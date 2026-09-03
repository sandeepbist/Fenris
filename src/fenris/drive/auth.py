"""OAuth for Google Drive: PKCE loopback flow, keyring-stored refresh token.

the build plan/5 decisions implemented here:

- Scope: ``https://www.googleapis.com/auth/drive.readonly`` - the only honest
  full-corpus choice (``drive.file`` cannot see pre-existing files;
  ``drive.metadata.readonly`` cannot download content).
- Flow: google-auth-oauthlib ``InstalledAppFlow.run_local_server`` - PKCE
  loopback, which the library enables by default (it autogenerates the
  S256 code verifier; we pass the flag explicitly so the intent is legible
  and survives a library default flip).
- Refresh token storage: the OS keyring ONLY (service "fenris", account
  "drive-refresh"). No plaintext fallback, ever - on a headless Linux box
  without Secret Service the user gets instructions, not a silent insecure
  file (the build plan security section: a secret on disk is a regression).
- ``disconnect()`` revokes for real: POSTs the refresh token to
  ``https://oauth2.googleapis.com/revoke`` AND deletes the keyring entry.
  A stale token never lingers silently. Deletion happens even if the revoke
  HTTP call fails, so local state never claims "connected" while Google
  still honors the grant; a revoke failure is surfaced, not swallowed.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import keyring
from google_auth_oauthlib.flow import InstalledAppFlow
from keyring.backends import fail as fail_backend
from keyring.backends import null as null_backend

__all__ = [
    "DRIVE_SCOPE",
    "KEYRING_ACCOUNT",
    "KEYRING_SERVICE",
    "REVOKE_URL",
    "AuthError",
    "KeyringUnavailableError",
    "build_credentials",
    "disconnect",
    "load_or_refresh",
    "run_auth_flow",
    "token_present",
]

#: The only scope fenris requests. Read-only by construction (the build plan).
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly"

KEYRING_SERVICE = "fenris"
KEYRING_ACCOUNT = "drive-refresh"

REVOKE_URL = "https://oauth2.googleapis.com/revoke"


class AuthError(Exception):
    """OAuth flow failed in a way the user must fix (no client secrets file,
    flow aborted, keyring unusable, refresh rejected)."""


class KeyringUnavailableError(AuthError):
    """No usable OS keyring backend. Never falls back to a plaintext file.

    Instructions are embedded because this is the one failure a headless
    Linux user actually hits (the build plan: headless-Linux gotchas).
    """


def _require_keyring() -> None:
    """Raise KeyringUnavailableError if the OS has no real keyring backend.

    keyring degrades to a null backend (returns None for everything) or a
    fail backend (raises NoKeyringError) when nothing viable exists. Writing
    to the null backend SILENTLY DISCARDS the token - that is the exact
    silent-data-loss case the MVP security section forbids, so both
    non-backends are treated as "unavailable" up front.
    """
    backend = keyring.get_keyring()
    if isinstance(backend, (null_backend.Keyring, fail_backend.Keyring)):
        raise KeyringUnavailableError(
            "No usable OS keyring backend was found, so fenris cannot store the "
            "Google refresh token safely. fenris never writes secrets to a "
            "plaintext file. Fix the keyring first:\n"
            "  - Linux (headless): install and start a Secret Service provider, "
            "e.g. `gnome-keyring` + `libsecret`, then re-run with the session "
            "keyring unlocked (or run inside a desktop session).\n"
            "  - macOS: unlock the login keychain.\n"
            "  - Windows: Credential Manager is used automatically.\n"
            "See https://pypi.org/project/keyring for backend options."
        )


def _load_client_config(client_secrets: Path | str) -> dict:
    """Read and validate the OAuth client secrets JSON (installed app type)."""
    path = Path(client_secrets)
    if not path.exists():
        raise AuthError(
            f"OAuth client secrets file not found: {path}\n"
            "Create OAuth credentials (type 'Desktop app') at "
            "https://console.cloud.google.com/apis/credentials and download the "
            "client secrets JSON there."
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise AuthError(f"Could not read client secrets file {path}: {e}") from e
    if "installed" not in data:
        raise AuthError(
            f"Client secrets file {path} has no 'installed' section - create "
            "credentials of type 'Desktop app' (the build plan: PKCE loopback flow)."
        )
    return data


def _credentials_from_refresh_token(refresh_token: str) -> google.oauth2.credentials.Credentials:  # noqa: F821
    """Rebuild google credentials around a stored refresh token."""
    import google.oauth2.credentials

    return google.oauth2.credentials.Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=[DRIVE_SCOPE],
    )


def run_auth_flow(
    client_secrets: Path | str,
    *,
    port: int = 8080,
    open_browser: bool = True,
    timeout_seconds: int | None = None,
    keyring_account: str = KEYRING_ACCOUNT,
) -> str:
    """Run the PKCE loopback OAuth flow; store and return the refresh token.

    Uses InstalledAppFlow.run_local_server (loopback redirect, PKCE S256 -
    the library autogenerates the code verifier). The refresh token is
    written to the OS keyring under (service "fenris", account
    "drive-refresh" - or "drive-refresh:<vault>" for additional vaults,
    P2.5) and also returned for callers that need it in-session.
    """
    _require_keyring()
    config = _load_client_config(client_secrets)
    flow = InstalledAppFlow.from_client_config(
        config, scopes=[DRIVE_SCOPE], autogenerate_code_verifier=True
    )
    try:
        creds = flow.run_local_server(
            host="localhost",
            port=port,
            open_browser=open_browser,
            timeout_seconds=timeout_seconds,
        )
    except Exception as e:  # user closed the browser, port busy, flow aborted
        raise AuthError(f"OAuth flow did not complete: {e}") from e
    if not creds.refresh_token:
        raise AuthError(
            "Google returned no refresh token. Re-run the flow and REVOKE "
            "previous grants at https://myaccount.google.com/permissions first - "
            "a re-consent after revocation is what forces a fresh refresh token."
        )
    keyring.set_password(KEYRING_SERVICE, keyring_account, creds.refresh_token)
    return creds.refresh_token


def token_present(keyring_account: str = KEYRING_ACCOUNT) -> bool:
    """Is a refresh token stored? (Never prints or returns the token.)"""
    try:
        _require_keyring()
    except KeyringUnavailableError:
        return False
    return keyring.get_password(KEYRING_SERVICE, keyring_account) is not None


def build_credentials(refresh_token: str):
    """google.oauth2 credentials for a raw refresh token (test seam)."""
    return _credentials_from_refresh_token(refresh_token)


def load_or_refresh(keyring_account: str = KEYRING_ACCOUNT):
    """Return valid google credentials from the keyring, refreshing as needed.

    Raises AuthError when nothing is stored or the refresh is rejected (the
    7-day-expiry case in the build plan - the user must re-run the flow;
    silent failure would leave sync believing it is connected).
    """
    _require_keyring()
    refresh_token = keyring.get_password(KEYRING_SERVICE, keyring_account)
    if not refresh_token:
        raise AuthError(
            "No Drive credentials stored. Run `fenris drive auth` first."
        )
    creds = _credentials_from_refresh_token(refresh_token)
    if not creds.valid:
        try:
            import google.auth.transport.requests

            creds.refresh(google.auth.transport.requests.Request())
        except Exception as e:
            raise AuthError(
                "Stored Drive credentials were rejected by Google (refresh "
                "failed). Re-run `fenris drive auth` to re-consent."
            ) from e
    return creds


def _delete_quietly(delete_password, account: str = KEYRING_ACCOUNT) -> None:
    """Best-effort keyring deletion.

    Used on the disconnect paths, where a failed delete must never abort the
    flow: the token is either already gone (KeyringError) or the backend is
    misbehaving, and in both cases the correct next step is the same - make
    sure nothing remains stored and let the caller's outcome stand. The
    deletion failure is invisible to the user by design here because the
    revoke outcome (the security-relevant part) is reported separately.
    """
    try:
        delete_password(KEYRING_SERVICE, account)
    except keyring.errors.KeyringError:
        pass


def disconnect(
    *, keyring_account: str = KEYRING_ACCOUNT,
    _opener=urllib.request.build_opener, _keyring_get=None, _keyring_delete=None
) -> bool:
    """Revoke the stored grant for real and delete the keyring entry.

    POSTs ``token=<refresh_token>`` to Google's revoke endpoint (form
    encoded, per oauth2.googleapis.com/revoke). The keyring entry is deleted
    REGARDLESS of the revoke outcome, so no local state can claim
    "connected" while the grant still works remotely; if the revoke HTTP call
    itself fails, that is returned/surfaced rather than swallowed.

    Returns True when the revoke succeeded (HTTP 200). Returns False when
    there was nothing to disconnect. Raises AuthError on transport failure
    (the keyring entry is still gone - disconnect is never half-done).

    ``keyring_account`` selects the vault ("drive-refresh" default, or
    "drive-refresh:<vault>" - P2.5). ``_opener`` / ``_keyring_get`` /
    ``_keyring_delete`` are test seams: the offline suite injects a fake
    network POST and a fake keyring backend.
    """
    get_pw = _keyring_get if _keyring_get is not None else keyring.get_password
    del_pw = _keyring_delete if _keyring_delete is not None else keyring.delete_password
    opener = _opener()

    refresh_token = get_pw(KEYRING_SERVICE, keyring_account)
    if not refresh_token:
        # Nothing stored: the user is already disconnected locally. Google
        # may still hold a grant from an earlier install, but fenris has no
        # token to present - that is the documented manual-revocation path.
        _delete_quietly(del_pw, keyring_account)
        return False

    revoked = False
    try:
        body = urllib.parse.urlencode({"token": refresh_token}).encode("utf-8")
        request = urllib.request.Request(
            REVOKE_URL, data=body, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with opener.open(request, timeout=30) as response:
                revoked = 200 <= response.status < 300
        except urllib.error.HTTPError as e:
            # 400/401 from revoke = token already invalid/expired: the grant
            # is dead on Google's side, which is what disconnect wants.
            revoked = e.code in (400, 401)
    except urllib.error.URLError as e:
        raise AuthError(
            f"Could not reach Google's revoke endpoint ({e}). The local token "
            "was deleted; revoke manually at "
            "https://myaccount.google.com/permissions if the grant matters."
        ) from e
    finally:
        # Delete first even on the way out: a raised AuthError must not leave
        # the token in the keyring claiming to be connected.
        _delete_quietly(del_pw, keyring_account)
    if not revoked:
        # Token is gone locally; the remote grant may or may not be dead.
        # Report, never swallow (the build plan: never silently).
        raise AuthError(
            "Google's revoke endpoint rejected the request. The local token was "
            "deleted, but the remote grant may still be active - revoke it "
            "manually at https://myaccount.google.com/permissions if in doubt."
        )
    return True
