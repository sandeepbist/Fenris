"""Typed errors for the Drive client surface.

The single most load-bearing distinction (the build plan): 403 handling
splits by *reason*, not by status code.

- rate-limit/quota (``rateLimitExceeded``, ``dailyLimitExceeded``,
  ``tooManyRequests`` 429, ``userRateLimitExceeded``) -> exponential backoff
  + jitter, capped, then surface as a stale/failed sync - retryable.
- genuine permission denial (``forbidden``, ``insufficientFilePermissions``)
  -> **no retry ever**, the item is unreadable and routes to the
  unauthorized path.

Everything else that looks transient (5xx, transport noise) is retryable as
TransientError. Nothing in this module ever swallows an error silently.
"""

from __future__ import annotations

import json
import random
import time
from collections.abc import Callable
from typing import Any

# googleapiclient is an installed dependency; imported lazily nowhere - errors.py
# is imported by fake.py and tests, both of which run in the project venv.
from googleapiclient.errors import HttpError

__all__ = [
    "BACKOFF_MAX_RETRIES",
    "PERMISSION_DENIED_REASONS",
    "RATE_LIMIT_REASONS",
    "FenrisDriveError",
    "PermissionDeniedError",
    "RateLimitedError",
    "TransientError",
    "backoff",
    "classify_http_error",
]

# config.py owns BACKOFF_MAX_RETRIES = 5 but the drive slice must not import
# across module boundaries; the value is identical (the build plan: cap 5).
BACKOFF_MAX_RETRIES = 5

RATE_LIMIT_REASONS = frozenset(
    {
        "rateLimitExceeded",
        "dailyLimitExceeded",
        "userRateLimitExceeded",
        "tooManyRequests",
        "quotaExceeded",
    }
)

PERMISSION_DENIED_REASONS = frozenset(
    {
        "forbidden",
        "insufficientFilePermissions",
        "insufficientPermissions",
    }
)

# Decorrelated-jitter bounds (seconds). Base ~ the "1s, doubling" family used
# against per-minute Drive quotas; decorrelated jitter keeps bursts from
# synchronizing (AWS architecture blog formulation).
_BACKOFF_BASE = 1.0
_BACKOFF_CAP = 60.0


class FenrisDriveError(Exception):
    """Base for all errors the Drive client surface raises deliberately.

    Callers above this layer should never see a raw
    googleapiclient/httplib2 error leak out of fenris.drive.
    """

    #: Whether a retry with backoff is meaningful for this error class.
    retryable: bool = False
    #: Rough bucket for logging/telemetry: 'rate_limit' | 'permission' | 'transient'
    kind: str = "error"

    def __init__(self, message: str, *, reason: str = "", status_code: int | None = None,
                 retry_after: float | None = None):
        super().__init__(message)
        self.reason = reason
        self.status_code = status_code
        self.retry_after = retry_after


class RateLimitedError(FenrisDriveError):
    """Drive returned a quota/rate response. Retryable with backoff."""

    retryable = True
    kind = "rate_limit"


class PermissionDeniedError(FenrisDriveError):
    """Genuine access denial. NEVER retried - a retry would only re-deny."""

    retryable = False
    kind = "permission"


class TransientError(FenrisDriveError):
    """Server-side/transient failure (5xx, transport). Retryable with backoff."""

    retryable = True
    kind = "transient"


def _reasons_from_error(err: HttpError) -> list[str]:
    """Pull every machine-readable reason string out of an HttpError.

    HttpError.reason is the human *message* (e.g. "Rate limit exceeded"), not
    the machine reason; the per-item reasons live in the JSON body under
    error.errors[i].reason (mirrored in error_details as a list of dicts).
    We match on both surfaces so a malformed body can't hide the reason.
    """
    reasons: list[str] = []
    # 1) error_details: list of per-item dicts with a "reason" key
    details = getattr(err, "error_details", "")
    if isinstance(details, list):
        for item in details:
            if isinstance(item, dict) and isinstance(item.get("reason"), str):
                reasons.append(item["reason"])
    # 2) raw JSON body (works even when _get_reason fell back to non-JSON parse)
    content = getattr(err, "content", b"")
    if isinstance(content, (bytes, bytearray)):
        try:
            data = json.loads(bytes(content).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            data = None
        if isinstance(data, dict):
            inner = data.get("error")
            if isinstance(inner, dict) and isinstance(inner.get("errors"), list):
                for item in inner["errors"]:
                    if isinstance(item, dict) and isinstance(item.get("reason"), str):
                        reasons.append(item["reason"])
    # 3) HttpError.reason - the message. Google sometimes puts the bare reason
    #    string here (e.g. plain-text or protobuf-less bodies).
    msg = getattr(err, "reason", "") or ""
    for token in msg.replace(",", " ").split():
        if token in RATE_LIMIT_REASONS or token in PERMISSION_DENIED_REASONS:
            reasons.append(token)
    # 4) the documented reason classification also rides on the HTTP layer:
    #    resp.reason for e.g. "Too Many Requests" on 429s.
    resp = getattr(err, "resp", None)
    if resp is not None:
        r = resp.get("reason") if hasattr(resp, "get") else None
        if isinstance(r, str) and r:
            reasons.append(r)
    return reasons


def _status_code(err: HttpError) -> int | None:
    resp = getattr(err, "resp", None)
    try:
        return resp.status  # type: ignore[attr-defined]
    except AttributeError:
        return None


def _retry_after(err: HttpError) -> float | None:
    """Parse Retry-After (seconds form only; HTTP-date form is not used by Drive)."""
    resp = getattr(err, "resp", None)
    try:
        raw = resp.get("retry-after") or resp.get("Retry-After")  # type: ignore[union-attr]
    except AttributeError:
        return None
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def classify_http_error(err: HttpError) -> FenrisDriveError:
    """Convert an HttpError into exactly one typed fenris error.

    Order matters: reason strings win over status codes, because a 403 with
    ``rateLimitExceeded`` is retryable while a 403 with ``forbidden`` never is.
    Only if no recognized reason is found does the status code decide:
    429/403 fall back to RateLimitedError (Google uses 403 for quota), other
    5xx become TransientError, and anything else is surfaced as a
    PermissionDeniedError-shaped dead end is wrong - unknown 4xx that are
    neither rate limit nor permission (e.g. 400 bad request) map to a
    non-retryable FenrisDriveError via TransientError's sibling: we keep the
    class set minimal and use PermissionDeniedError only for real denials, so
    unknown client errors become plain FenrisDriveError (never retried).
    """
    reasons = _reasons_from_error(err)
    status = _status_code(err)
    retry_after = _retry_after(err)
    joined = ", ".join(dict.fromkeys(reasons)) or (getattr(err, "reason", "") or "")

    for r in reasons:
        if r in RATE_LIMIT_REASONS:
            return RateLimitedError(
                f"Drive rate limit ({r}): {getattr(err, 'reason', '')}",
                reason=r, status_code=status, retry_after=retry_after,
            )
    for r in reasons:
        if r in PERMISSION_DENIED_REASONS:
            return PermissionDeniedError(
                f"Drive permission denied ({r}): {getattr(err, 'reason', '')}",
                reason=r, status_code=status,
            )

    # Status-code fallback when the body gave no recognized reason.
    if status == 429:
        return RateLimitedError(
            f"Drive rate limit (HTTP 429): {getattr(err, 'reason', '')}",
            reason="tooManyRequests", status_code=status, retry_after=retry_after,
        )
    if status == 403:
        # Google issues 403 for quota problems when no reason token is present.
        return RateLimitedError(
            f"Drive rate limit (HTTP 403, unrecognized reason: {joined or 'none'}): "
            f"{getattr(err, 'reason', '')}",
            reason="rateLimitExceeded", status_code=status, retry_after=retry_after,
        )
    if status is not None and 500 <= status <= 599:
        return TransientError(
            f"Drive transient failure (HTTP {status}): {getattr(err, 'reason', '')}",
            reason=f"serverError{status}", status_code=status,
        )
    # Unknown 4xx (and anything else): not retryable, not a permission claim.
    return FenrisDriveError(
        f"Drive request failed (HTTP {status}): {getattr(err, 'reason', '')}",
        reason=joined or "unknown", status_code=status,
    )


def backoff(
    attempt: int,  # 0-based; the sleep BEFORE the (attempt+1)-th retry
    *,
    sleep: Callable[[float], None] = time.sleep,
    rng: Callable[[], float] | None = None,
    previous: float | None = None,
) -> float:
    """Exponential backoff with decorrelated jitter; returns the delay used.

    Decorrelated jitter (AWS formulation): ``sleep = min(cap, rand(base,
    previous * 3))`` with previous starting at base - delays decorrelate
    across callers instead of synchronizing.

    This helper computes and *performs* the sleep (injectable for tests so
    the suite stays fast); callers pass the returned value as ``previous``
    on the next round so the random walk carries state.
    """
    rand = rng if rng is not None else random.random
    upper = min(_BACKOFF_CAP, _BACKOFF_BASE * (2 ** attempt))
    prev = previous if previous is not None else _BACKOFF_BASE
    delay = min(_BACKOFF_CAP, _BACKOFF_BASE + rand() * (prev * 3 - _BACKOFF_BASE))
    delay = min(delay, upper)  # keep exponential shape under jitter; never explode past cap
    sleep(delay)
    return delay


def run_with_backoff(
    op: Callable[[], Any],
    *,
    is_retriable: Callable[[BaseException], bool],
    max_retries: int = BACKOFF_MAX_RETRIES,
    on_retry: Callable[[int, BaseException, float], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    rng: Callable[[], float] | None = None,
) -> Any:
    """Run ``op`` retrying retryable failures per the build plan.

    - Retries only errors for which ``is_retriable`` is True. In the Drive
      client that means RateLimitedError and TransientError ONLY -
      PermissionDeniedError propagates immediately, on the first failure,
      because a genuine denial never becomes a grant by waiting.
    - Honors a Retry-After header when the error carries one: that delay is
      used verbatim (still capped) and beats the jittered estimate.
    - Max 5 retries; the final failure re-raises.
    - ``on_retry(attempt_1_based, error, delay)`` lets callers/tests count
      retries without touching the sleep clock.
    """
    previous: float | None = None
    attempt = 0
    while True:
        try:
            return op()
        except BaseException as e:
            if not is_retriable(e):
                raise
            if attempt >= max_retries:
                raise
            retry_after = getattr(e, "retry_after", None)
            if retry_after is not None and retry_after >= 0:
                delay = min(float(retry_after), _BACKOFF_CAP)
                sleep(delay)
            else:
                delay = backoff(attempt, sleep=sleep, rng=rng, previous=previous)
            previous = delay
            attempt += 1
            if on_retry is not None:
                on_retry(attempt, e, delay)
