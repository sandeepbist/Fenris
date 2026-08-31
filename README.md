# Fenris

RAG over your own Google Drive. Your Drive stays the source of truth; Fenris builds a local, revision-aware search index over it and answers questions with cited evidence — or tells you honestly when the evidence isn't there.

```
fenris drive auth          # connect your Drive (read-only, OAuth PKCE)
fenris sync run            # enumerate + index; later runs drain only changes
fenris ask "Q3 revenue?"   # cited answer, or an explicit answer state
```

## Why

Hosted vector databases charge you to keep your own documents in RAM. Fenris keeps Drive as the authority and holds only a local read projection: chunk text, embeddings, and a lexical index in SQLite. Storage you already own stores the documents; a laptop-scale database stores the index. Nothing of yours is uploaded anywhere by Fenris.

The design bets on a few principles:

- **Correctness before performance.** A stale answer is worse than a slow one, and a silently-wrong citation is worse than no citation. The index is keyed by content revision, not file identity; deleted or unshared files stop serving immediately; every answer carries verifiable spans or an explicit state.
- **Honest non-answers.** Queries resolve to one of ANSWERABLE, INSUFFICIENT, STALE, CONFLICTING, or ERROR. The system would rather say what's missing than synthesize fluency.
- **Local-first.** SQLite (WAL) + FTS5 + sqlite-vec for hybrid BM25/dense retrieval; Docling for parsing; Gemini for embeddings and generation; FlashRank for CPU reranking. Everything except the two API-backed steps runs offline.

## Install

Requires Python 3.12+.

```bash
make setup          # venv + deps (auto-trims GPU wheels Docling would drag in)
make test           # 308 tests, all offline
```

Set `GOOGLE_API_KEY` (or `GEMINI_API_KEY`) for embeddings and generation. Drive access needs a one-time `fenris drive auth` (PKCE loopback; the refresh token lives in your OS keyring, never on disk).

## The pipeline

1. **Sync** enumerates Drive (or drains its change feed) with the page token committed in the same transaction as the index writes — a crash replays, never skips.
2. **Ingest** parses via Docling (docx, xlsx, pptx, text, markdown, HTML, EPUB; Google Docs/Sheets/Slides via export), splits at 512 tokens with no overlap, builds deterministic contextual headers, embeds at 1536 dims, and records tables as typed records with row-block chunks.
3. **Retrieve** routes the query deterministically, runs BM25 and filtered vector KNN in parallel, fuses with RRF, reranks on CPU, and picks k by score-shape rather than a fixed count.
4. **Answer** verifies cited sources, applies the state ladder, assembles a position-aware prompt with retrieved text sandboxed as data, and renders citations deterministically — an invented citation is dropped and logged, never shown.
5. **Evaluate** runs the golden suite (120 graded cases over a synthetic personal corpus) with per-slice metrics; slices are never blended into one number.

See [docs/architecture.md](docs/architecture.md) for the design in depth.

## Limitations, stated plainly

- **PDFs and images are skipped by default.** Docling's layout models pull a ~1.2 GB PyTorch dependency; this repo ships CPU-slim by default and records such files as `skipped` with the reason. Install `torch` if you want them indexed.
- **Single user.** Authorization is your own Drive's; multi-tenant isolation is not a goal of this codebase.
- **Google-native quota applies.** Enumerating and ingesting spends Drive API quota units; sync is paced and incremental, but the first full ingest of a large Drive takes real time by design.
- **Eval gates are calibration-pending.** The suite measures; production thresholds are deliberately conservative until calibrated on real corpora.

## Development

```bash
make test      # full suite
make lint      # ruff
```

Conventional commits, one commit per green change, tests before every commit. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE).
