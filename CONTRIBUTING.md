# Contributing to Fenris

## The short version

1. Branch, change, test, commit.
2. `make test` must be green before every commit. `make lint` too.
3. Conventional commits, described below.
4. Never weaken a correctness test to make a feature test pass.

## Correctness rules (non-negotiable)

These invariants are the product. A PR that breaks any of them is rejected regardless of what it improves:

- **The cursor never advances past uncommitted index state.** Sync page tokens commit in the same transaction as that page's index writes.
- **One active representation per file.** Enforced by partial unique index; a revision bump retires the old representation atomically.
- **Retrieved text is data, never instructions.** Evidence is sandboxed in the prompt; the model cannot extend its own permissions through a document.
- **Citations resolve deterministically.** Model-invented evidence IDs are dropped and logged; they never reach the user.
- **No unauthorized or stale serving.** Tombstoned chunks must not surface in KNN or BM25; the tests pin this.
- **Network work happens outside transactions.** DB commits are milliseconds, never held across API calls.

## Commits

Conventional format with scope:

```
feat(retrieve): fuse BM25 and KNN with rank-bias-safe scoring
fix(sync): tombstone trashed files, not just removed ones
test(ingest): pin zero-overlap chunk boundaries
docs(readme): state the PDF limitation plainly
```

- Subject ≤ 72 characters, imperative mood.
- Body explains **what changed, why, and the evidence** (test counts, verified facts). A body that only restates the subject is noise.
- One commit per green change. Don't batch unrelated work.
- No internal milestone codes in subjects — a reader with no project context should understand every subject line.

## Testing

- All tests run offline. The Drive surface is exercised through `FakeDriveClient`, which raises the real library's `HttpError` types; generation through `FakeGenerator`.
- New feature → new tests in the same commit. Bug fix → regression test that fails without the fix.
- Hand-computed expected values for anything mathematical (nDCG, RRF fusion, adaptive-k elbows). A metric tested only against itself proves nothing.
- Metrics report per slice. If you added a blended number, remove it.

## Dependencies

The venv is deliberately CPU-slim: Docling's deep-learning stack (torch + CUDA wheels, ~4.5 GB) is uninstalled by `make setup`. If your change needs it back (e.g., PDF layout), say so in the PR and expect scrutiny — storage and install weight are user-facing costs.

## What's intentionally absent

No multi-tenant auth, no learned routers, no ANN graphs, no speculative generation. These are documented decisions, not gaps; see `docs/architecture.md` for the reasoning and the measured-gates philosophy behind deferral.

## Repo layout

```
src/fenris/
  schema.py, db.py      # the projection: DDL, triggers, vec0, connection
  config.py             # pinned toolchain identity (parser/chunker/embedder)
  drive/                # OAuth, DriveClient, quota, error taxonomy, fakes
  ingest/               # MIME routing, Docling parse, chunking, embeddings
  sync/                 # change-feed drain, tombstones, resync, staleness
  retrieve/             # routing, BM25, KNN, RRF, rerank, adaptive-k, evidence
  generate/             # verify, state ladder, prompts, agentic retry, render
  eval/                 # corpus, cases, metrics, runner
tests/                  # mirrors the slices; all offline
eval/                   # golden corpus fixtures + 120 graded cases
```
