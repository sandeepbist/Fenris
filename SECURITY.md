# Security Policy

Fenris connects to your Google Drive with a read-only token and keeps a
local index of your documents. Security is a product feature here, not a
process — treat every report as a product bug.

## The design posture

- **Drive scope is `drive.readonly`.** Fenris has no code path that writes
  to Drive; `fenris drive disconnect` revokes the grant at Google even if
  the network call fails.
- **The refresh token lives in the OS keyring**, never in a file.
- **The web UI binds `127.0.0.1` only and has no auth.** It was built for
  your own browser, nothing else. Do not port-forward it behind a proxy.
- **Retrieved text is fenced as data, never instructions** — no shell, no
  URL fetcher, no Drive write path exists inside the answer loop.
- **All SQL is parameterized**; ingest neutralizes control characters and
  metadata bombs; questions are capped at validation.

The full threat model — what is defended and what deliberately isn't — is
in [docs/legal/security-disclosure.md](docs/legal/security-disclosure.md).

## Supported versions

Only the latest release line receives fixes. Security patches land in
patch releases and are announced in the release notes.

## Reporting a vulnerability

Use GitHub's **private security advisory** page:

https://github.com/sandeepbist/Fenris/security/advisories

Please include the affected version (`fenris version`), a minimal
reproduction, and your assessment of reachability. Do not open a public
issue for anything exploitable — the advisory page keeps the details
private until a fix ships.

You can expect a response within a week. The project is maintained by
volunteers; disclosure timelines are best-effort (coordinated disclosure
once a patch is out). We credit reporters in the release notes and the
security disclosure doc, at your option.

## What we consider a vulnerability

- Any path where a **Drive document can influence execution**: prompt
  injection reaching a tool, shell, or network call (the answer loop has
  none — a regression here is critical).
- **Token or index data leaving the machine** through any Fenris process.
- **Citation forgery**: an answer citing text that was not retrieved.
- **Stale or unauthorized data served**: a revoked/trashed file still
  answering.
- SQL/FTS5 injection, or any crash-loop reachable from a crafted document.

Hard crashes reachable only from your own files and preferences are bugs,
not vulnerabilities — file them as issues.
