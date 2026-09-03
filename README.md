<div align="center">

<!-- logo -->
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="frontend/public/favicon.svg">
  <img src="frontend/public/favicon.svg" width="110" alt="Fenris">
</picture>

# Fenris

**RAG over your own Google Drive — local-first, citation-enforced, honest by design.**

[![License: MIT](https://img.shields.io/badge/license-MIT-7aa2f7.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-306998.svg)](pyproject.toml)
[![Tests](https://img.shields.io/badge/tests-416%20passing-4ade80.svg)](tests/)
[![Scope](https://img.shields.io/badge/Drive%20scope-read--only-f97316.svg)](docs/legal/security-disclosure.md)
[![Offline](https://img.shields.io/badge/retrieval-100%25%20local-7aa2f7.svg)](docs/architecture.md)

Fenris connects to your Drive with a read-only token, builds a local search index,
and answers questions about your files with citations to the exact text it used —
or a plain statement that the evidence isn't there.

[Key ideas](#key-ideas) · [Quickstart](#quickstart) · [How it works](#how-it-works) · [Commands](#commands) · [Limitations](#honest-limitations) · [Security](#security)

</div>

---

## Key ideas

Most RAG tools ask you to upload your documents to someone else's corpus.
Fenris starts from the opposite position: your files stay where they are,
on storage you already pay for, and everything Fenris needs to search them
lives on your disk.

| Idea | What it means in practice |
| :--- | :--- |
| **Local-first** | Google Drive is the authoritative copy. Fenris keeps a local, rebuildable index — a projection, not a warehouse. Queries never touch the network. |
| **Citation-enforced** | Every answer links to the file, page, and quoted span it came from. A citation that doesn't resolve to retrieved text is dropped and logged. Model-invented sources cannot reach the output. |
| **Honest non-answers** | A question resolves to `ANSWERABLE`, `INSUFFICIENT`, `STALE`, `CONFLICTING`, or `ERROR`. When the evidence isn't there, Fenris says what's missing instead of writing a fluent guess. |
| **Correctness before speed** | The sync cursor commits in the same transaction as the index writes, so a crash replays rather than skips. A file can never have two active representations. Deleted or unshared files stop serving immediately. |
| **Untrusted by default** | Retrieved text is fenced as data, never instructions. No shell, no URL fetcher, no Drive write path exists in the answer loop — a poisoned document has nothing to command. |

---

## Quickstart

Requirements: Python 3.12+, [bun](https://bun.sh) (web UI only), a Google account, and a [Gemini API key](https://aistudio.google.com/apikey).

**Terminal only:**

```bash
make setup                        # venv + deps (auto-trims GPU wheels)

export GOOGLE_API_KEY=...         # aistudio.google.com/apikey
.venv/bin/fenris drive auth      # one-time: browser opens, approve read-only access
.venv/bin/fenris sync run         # first run indexes; later runs take seconds
.venv/bin/fenris ask "what's in my 2024 tax folder?"
```

**With the web UI** — dashboard, chat with citation cards, sources ledger, settings:

```bash
make ui                           # build + serve everything at 127.0.0.1:8000
```

```bash
make test                         # the full suite, all offline, ~1min
make lint                         # ruff
make ui-dev                       # hot-reload frontend on :5173
```

<div align="center"><em>The whole product is one process: UI, API, and engine.</em></div>

---

## How it works

```
 Google Drive ──── change feed ────► Sync ──► Ingest ──► SQLite projection
 (authoritative)                     │        (Docling)   sources · chunks
                                     │                     FTS5 · vec0
                                     │                     tables · cursor
                                     ▼
        ┌──────────────── local only ────────────────┐
        │                                            │
        ▼                                            ▼
      BM25 ──────── RRF fusion ──────── KNN        Verify
        └──────────────┬───────────────┘            │
                       ▼                            ▼
                   Rerank ──► Adaptive-k ──► State ladder ──► Prompt
                                  │               │          (untrusted fence)
                                  ▼               ▼
                             Diversity      INSUFFICIENT │   │
                                              STALE ────┤   ▼
                                           CONFLICTING ─┤ Generate
                                                      └─► Citation render
                                                           │
                                                           ▼
                                                     Answer + state
```

**The answer path, in one paragraph.** The query is routed deterministically —
exact-match questions go BM25-first, table questions restrict to structured
records, everything else runs the hybrid path. BM25 and filtered vector search
run in parallel against active, untrashed chunks; results fuse via reciprocal
rank; a CPU reranker scores the fused set; adaptive-k picks how many results
the question actually deserves (score shape, not a fixed count). Cited sources
are verified against the index before generation; the model sees evidence
sandboxed as data and must cite evidence IDs; the renderer resolves those IDs
deterministically. An answer that survives all of that is shown with its
citations. Anything less gets an explicit state — never a dressed-up guess.

The full design — revision-aware identity, the crash-safe sync loop, the
evaluation suite — is in **[docs/architecture.md](docs/architecture.md)**.

---

## Commands

| Command | What it does |
| :--- | :--- |
| `fenris drive auth` | PKCE loopback OAuth; refresh token goes to the OS keyring, never a file |
| `fenris drive auth --vault work` | Connect a second Google account as its own vault (P2.5) |
| `fenris drive disconnect` | Revokes the Google grant for real and deletes the stored token |
| `fenris drive status` | Connection state and this session's quota counters |
| `fenris vault ls` | Every registered vault + live keyring connection state |
| `fenris sync run [--vault work]` | Drains the Drive change feed; renames cost metadata only, edits re-ingest |
| `fenris sync status` | Cursor state, index counts, staleness verdict |
| `fenris sync resync [--vault work]` | Full re-enumeration escape hatch with diff reconciliation |
| `fenris digest now` | Compute + print one "what changed in my Drive" digest |
| `fenris digest schedule add "Weekly" --at 08:00 --weekly --dow mon` | Monday-8am digest schedule; the API process runs due ones |
| `fenris ask "..." [--vault work]` | One question, one cited answer (or an explicit state) |
| `fenris eval --suite golden` | 120 graded cases over a reproducible synthetic corpus |
| `fenris init` | Create the data directory and apply the schema |

---

## What gets indexed

| Format | Path |
| :--- | :--- |
| DOCX · XLSX · PPTX · TXT · MD · HTML · EPUB | Parsed locally with Docling |
| Google Docs · Sheets · Slides | Exported, hash-verified, then parsed |
| Tables (in any format) | Kept as structured records with cell-level references |
| PDF · images | Skipped in the CPU-slim build — see [limitations](#honest-limitations) |
| Audio · video · archives | Skipped with the reason recorded, visible in the UI |

---

## Honest limitations

Stated plainly, because a tool that hides its gaps invites you to trust it
anyway.

- **PDFs and images are skipped by default.** Docling's layout models pull a
  ~1.2 GB PyTorch install; this repo ships CPU-slim instead. Install `torch`
  and re-run sync to enable them. Skipped files are recorded with reasons.
- **First sync takes real time.** Downloads are paced against Drive's quota
  (~600/min). Hundreds of files is minutes; tens of thousands is hours — by
  design, because quota violations break syncs.
- **Single user, multiple vaults.** One machine, one person — but up to
  several Google accounts (`fenris drive auth --vault work`), each with its
  own keyring token, its own sync cursor, and per-question vault chips in
  the chat. No multi-tenant anything: a vault is yours on this machine,
  full stop.
- **Answers are only as fresh as the last sync.** Fenris marks staleness when
  it detects it rather than pretending freshness.
- **The local index is not encrypted.** The OS login is the boundary; a
  fake passphrase checkbox would be theater. See the
  [security disclosure](docs/legal/security-disclosure.md) for the full
  threat model — what's defended, and what deliberately isn't.

---

## Security

The engine survived an offensive review: five attack batteries (SQL/FTS5
injection, citation forgery, API abuse, token-path tracing, ingest bombs),
four real vulnerabilities found and fixed, each locked by a regression test
in [tests/test_security.py](tests/test_security.py).

- OAuth scope is `drive.readonly` — Fenris cannot write to your Drive, period.
- The refresh token lives in the OS keyring; `disconnect` revokes at Google
  even if the network call fails.
- All SQL is parameterized; control characters and metadata bombs are
  neutralized at ingest; questions are capped at validation.
- The web UI binds `127.0.0.1` only and has no auth — it was never meant to
  face anything but your own browser. Do not port-forward it.

Full disclosure: **[security & data handling](docs/legal/security-disclosure.md)** ·
[terms of service](docs/legal/terms-of-service.md) ·
[privacy policy](docs/legal/privacy-policy.md)

---

## Project layout

```
src/fenris/
├── drive/        OAuth client, quota accounting, error taxonomy, fakes
├── sync/         change-feed drain, tombstones, resync, staleness
├── ingest/       MIME routing, Docling parse, chunking, embeddings, guards
├── retrieve/     routing, BM25, KNN, RRF, rerank, adaptive-k, evidence
├── generate/     verification, state ladder, prompts, agentic retry, render
├── eval/         corpus, 120 graded cases, metrics, runner
└── api.py        local HTTP surface + one-process web UI

frontend/         React 19 · Tailwind 4 · dark terminal aesthetic
tests/            the full suite, all offline
docs/             architecture · legal · research notes
```

---

## Contributing

Six correctness invariants are enforced by tests and never traded for
features — the cursor never advances past uncommitted index state; one
active representation per file; retrieved text is data; citations resolve
deterministically; nothing stale or unauthorized serves; network never runs
inside transactions. Read them in **[CONTRIBUTING.md](CONTRIBUTING.md)**
before opening a PR.

## License

[MIT](LICENSE) — code. Legal documents adapted from
[General Legal](https://github.com/General-Legal/legal-templates) (CC0).
The project is not legal advice.
