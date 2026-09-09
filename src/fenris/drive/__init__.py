"""Drive connection slice: client wrapper, quota, typed errors, auth, fake, CLI.

Public surface for the integrator:

- ``fenris.drive.client.DriveClient`` - the real-API client (list_files,
  get_metadata, download_media, export, get_start_page_token, changes_list).
- ``fenris.drive.fake.FakeDriveClient`` - same contract, in-memory, for
  offline tests and eval.
- ``fenris.drive.auth`` - run_auth_flow / load_or_refresh / disconnect /
  token_present (keyring-backed, read-only scope).
- ``fenris.drive.cli.drive_app`` - Typer sub-app to wire into the main CLI.
"""

from fenris.drive import auth, client, errors, fake, quota

__all__ = ["auth", "client", "errors", "fake", "quota"]
