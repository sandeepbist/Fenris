"""Injection-pattern scanner (the build plan, "Security in MVP").

A deterministic regex pass over chunk text at ingest: patterns like
"ignore previous instructions", exfiltration phrasing, and base64
payloads mark the chunk ``flagged=1``. The flag is surfaced to the prompt
layer and to the user - it NEVER blocks ingestion and is never treated
as proof of safety. The prompt layer's data-not-instructions rule is
the actual mitigation; this is the annotation that makes it auditable.

Patterns come from config.INJECTION_PATTERNS (the security policy lives
there); this module owns the matching semantics.
"""

from __future__ import annotations

import base64
import binascii
import re

SCANNER_VERSION = "regex-v1"

# Compiled once at import: case-insensitive, whitespace-tolerant.
# A phrase pattern like "ignore previous instructions" must also match
# "ignore the previous instructions", so runs of whitespace inside
# the pattern become ``\s+``. Note: re.escape() escapes a literal space
# as backslash-space ("\ "), and Python 3.14 keeps that behavior, so the
# whitespace-run substitution targets both the raw space and the escaped
# form (portable across the escape behaviors of supported versions).
_WS_RUN = re.compile(r"(?:\\ |\s)+")


def _compile_pattern(pattern: str) -> re.Pattern[str]:
    """Compile one config pattern into a whitespace-tolerant regex.

 A callable replacement keeps the output raw: a plain replacement
 string would re-escape the backslash and yield a literal ``\\s+``.
 """
    escaped = re.escape(pattern)
    return re.compile(_WS_RUN.sub(lambda _m: r"\s+", escaped), re.IGNORECASE)


def _compile_patterns(patterns) -> dict[str, re.Pattern[str]]:
    return {
        pattern: _compile_pattern(pattern)
        for pattern in patterns
        if pattern and pattern.strip()
    }


def _default_patterns() -> dict[str, re.Pattern[str]]:
    """config.INJECTION_PATTERNS without importing at module scope (keeps
 this module importable even if config moves)."""
    from fenris.config import INJECTION_PATTERNS

    return _compile_patterns(INJECTION_PATTERNS)


# Base64 blob heuristic: a long run of base64 alphabet characters. Most
# base64 in legitimate prose is short (inline padding); payloads are long.
_BASE64_RE = re.compile(rb"[A-Za-z0-9+/]{80,}={0,2}")
_BASE64_MIN_DECODED = 40  # decode must yield real bytes (payload, not junk)
_BASE64_PRINTABLE_RATIO = 0.85

# Compiled config patterns; rebuilt lazily so tests can swap the policy.
_PATTERNS: dict[str, re.Pattern[str]] | None = None


def _patterns() -> dict[str, re.Pattern[str]]:
    global _PATTERNS
    if _PATTERNS is None:
        _PATTERNS = _default_patterns()
    return _PATTERNS


def set_patterns(patterns) -> None:
    """Swap the pattern set (tests and future policy versions only)."""
    global _PATTERNS
    _PATTERNS = _compile_patterns(patterns)


def scan_text(text: str) -> tuple[bool, list[str]]:
    """Scan one chunk of text.

 Args:
 text: the chunk body (header context is not scanned - injection
 lives in content, and headers are deterministic anyway).

 Returns:
 (flagged, hits) where hits lists the patterns that matched (the
 stable config strings), plus ``b64_blob`` if a decodable base64
 payload was found.
 """
    if not text:
        return False, []
    hits: list[str] = []
    for pattern, rx in _patterns().items():
        if rx.search(text):
            hits.append(pattern)
    if _looks_like_base64_payload(text):
        hits.append("b64_blob")
    return bool(hits), hits


def _looks_like_base64_payload(text: str) -> bool:
    """True when the text embeds a long base64 run that decodes to mostly
 printable bytes - the signature of an encoded instruction payload.

 Deliberately conservative: a long run that fails to decode, or decodes
 to binary junk, is almost always a hash/ID/binary key, not an attack.
 """
    if len(text) < 80:
        return False
    for match in _BASE64_RE.finditer(text.encode("utf-8", errors="ignore")):
        blob = match.group(0)
        if len(blob) < 80:
            continue
        # pad to a multiple of 4 (trailing '=' may sit outside the run)
        padded = blob + b"=" * (-len(blob) % 4)
        try:
            decoded = base64.b64decode(padded, validate=False)
        except (binascii.Error, ValueError):
            continue
        if len(decoded) < _BASE64_MIN_DECODED:
            continue
        printable = sum(32 <= b < 127 or b in (9, 10, 13) for b in decoded)
        if printable / len(decoded) >= _BASE64_PRINTABLE_RATIO:
            return True
    return False


def scan_chunk(header_text: str, body_text: str) -> tuple[bool, list[str]]:
    """Scan a chunk for storage: body first (injection content), header
 included for completeness (a poisoned title is still a poisoned chunk).

 Returns:
 (flagged, hits) - stored as chunks.flagged=1 plus a debug summary.
 """
    flagged, hits = scan_text(body_text or "")
    if not flagged:
        flagged, header_hits = scan_text(header_text or "")
        hits.extend(header_hits)
    return flagged, hits
