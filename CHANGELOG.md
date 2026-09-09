# Changelog

Notable changes to Fenris. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[semver](docs/release-channels.md) with the answer contract counted as API.
Dates are YYYY-MM-DD.

## [1.0.0] — 2026-09-10

First public release. Everything below shipped before the repo opened; the
entries summarize the state at open, not individual commits.

### Added

- **Drive sync engine** — OAuth PKCE (read-only scope, token in the OS
  keyring), crash-safe change-feed sync with tombstones and a resync escape
  hatch, quota-paced downloads (~600/min), full error taxonomy with real
  token revocation on disconnect.
- **Ingestion** — MIME routing with a tiered parser: slim tier parses
  DOCX/XLSX/PPTX/TXT/MD/HTML/EPUB locally via Docling; full tier
  (`make install-full`) adds PDF layout models and image OCR. Google-native
  formats export, hash-verify, then parse. Revision-aware identity — a
  file never has two active representations.
- **Retrieval** — deterministic routing (exact / table / normal), hybrid
  BM25 + sqlite-vec KNN fused by RRF, CPU rerankers (FlashRank MiniLM
  default; ModernBERT via the pinned-weights registry with
  `fenris models ls/fetch`), adaptive-k, per-file diversity cap, and
  body-hash dedup on the fused list (default on, kill switch
  `FENRIS_DEDUP_BODIES=0`).
- **Answer layer** — the five-state ladder (ANSWERABLE, INSUFFICIENT,
  STALE, CONFLICTING, ERROR), deterministic citation resolution (forged
  evidence IDs dropped and logged), a bounded agentic retry loop, NLI
  groundedness gating (dark until calibration), and contextual-retrieval
  mode (dark until calibration).
- **Local-model fallback** — offline answers via local embedding +
  generation models with an honest "answered by" badge; state ladder
  unchanged offline.
- **Multi-vault** — several Google accounts as separate vaults, per-vault
  keyring tokens and sync cursors, vault-restricted retrieval (one vault's
  evidence never leaks into another's answer).
- **Web UI** — dashboard, cited-answer chat with SSE streaming and live
  citations, sources ledger with per-file skip reasons, jump-to-source
  viewer, find-the-file search, conversations persisted across reloads,
  command palette (⌘K), onboarding walk-through, digest history.
- **Desktop app** — Tauri v2 shell wrapping a PyInstaller engine sidecar.
- **Scheduled digests** — "what changed in my Drive" digests with a
  cron-subset scheduler inside the API process; `fenris digest` CLI parity.
- **CLI** — `fenris init/ask/eval/doctor/version`, `drive auth/status/
  disconnect`, `sync run/status/resync`, `vault ls/rm`, `digest`, `models`.
- **Eval harness** — 120 graded golden cases over a seeded synthetic
  corpus, ir-measures metrics, offline runner (`fenris eval --suite golden`).

### Security

- Five offensive batteries (SQL/FTS5 injection, citation forgery, API
  abuse, token-path tracing, ingest bombs); four real vulnerabilities
  found and fixed, each locked by a regression test.
- Web UI binds 127.0.0.1 only; no network inside transactions; retrieved
  text fenced as data; all SQL parameterized.

### Known limitations

See [the README's honest limitations](README.md#honest-limitations) and
the [security disclosure](docs/legal/security-disclosure.md): slim-tier
PDF/image skips, first-sync latency by design, unencrypted local index
(OS login is the boundary).

## [Unreleased]

- Golden-suite calibration sweeps for the dark features (reranker floors,
  contextual retrieval, groundedness thresholds) — eval procedures, gated
  on a user-run window.
- Desktop updater signing keys (see [release channels](docs/release-channels.md)).

[1.0.0]: https://github.com/sandeepbist/Fenris/releases/tag/v1.0.0
