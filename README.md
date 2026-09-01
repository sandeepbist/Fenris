# Fenris

Search and question-answering over your own Google Drive, on your machine.

Fenris connects to your Drive with a read-only token, builds a local index of your documents, and answers questions about them with citations to the exact text it used — or a plain statement that the evidence isn't there. Your files stay on your Drive. The index stays on your disk. Only your questions go anywhere, to the Gemini API, and that's the whole outbound story.

```
$ fenris ask "what did the Q3 report say about marketing spend?"
```

## Why you might want this

- **Your documents stay yours.** No uploads to a third-party corpus, no vector database billed by the gigabyte. Fenris is a read-only window over storage you already pay for.
- **Citations you can check.** Every answer links to the file, page, and quoted span it came from. A citation that doesn't resolve to retrieved text is dropped and logged, never shown.
- **Honest non-answers.** A question resolves to ANSWERABLE, INSUFFICIENT, STALE, CONFLICTING, or ERROR — the system says what's missing rather than writing fluent guesses.
- **It runs on a laptop.** SQLite with FTS5 and sqlite-vec, brute-force search over personal-scale corpora, a venv under 1.5 GB. No GPU, no server.

## What it does

1. **Syncs** from your Drive through the official change feed — renames and moves cost a metadata write; edits trigger re-ingestion of the changed file; deletions and revoked shares stop serving immediately. The sync cursor commits in the same transaction as the index writes, so a crash replays rather than skips.
2. **Parses and indexes** docx, xlsx, pptx, text, markdown, HTML, EPUB, and Google Docs/Sheets/Slides (via export) with Docling; chunks at 512 tokens with no overlap; keeps tables as structured records with cell-level references.
3. **Answers** through hybrid BM25 + vector search, RRF fusion, CPU reranking, and adaptive result counts — with every retrieved chunk sandboxed as untrusted data, prompt-injection defenses on by default.
4. **Serves a web UI** — dashboard, chat with citation cards, sources ledger, settings — from a single local process, or works entirely from the terminal.

## Requirements

- Python 3.12+, and [bun](https://bun.sh) if you want the web UI
- A Google account whose Drive you own, plus a one-time OAuth consent
- A Gemini API key ([get one free](https://aistudio.google.com/apikey)) for embeddings and answers

## Quickstart

Terminal only:

```bash
make setup
export GOOGLE_API_KEY=...          # from aistudio.google.com
.venv/bin/fenris drive auth        # one-time; browser opens, approve read-only access
.venv/bin/fenris sync run           # first run indexes; later runs take seconds
.venv/bin/fenris ask "what's in my 2024 tax folder?"
```

With the web UI — dashboard, chat, sources, settings at `http://127.0.0.1:8000`:

```bash
make ui
```

Development hot-reload: `make ui-dev` (frontend on :5173) alongside `make api`.

## Honest limitations

- **PDFs and images are skipped** in the default CPU-slim build. Docling's layout models pull a ~1.2 GB PyTorch install; if you want them indexed, install torch and re-run sync. Skipped files are recorded with reasons, visible in the Sources page.
- **First sync takes real time.** Downloads are paced against Drive's quota (~600/min). A few hundred files is minutes; tens of thousands is hours, by design — quota violations break syncs.
- **Single user.** One Drive, one machine, one person. There is no multi-tenant anything.
- **Answers are only as fresh as the last sync.** Fenris tells you when the index is stale rather than pretending.

## How it works

See [docs/architecture.md](docs/architecture.md) for the full design: revision-aware identity (content hashes, not Drive's version numbers), the crash-safe sync loop, the answer-state ladder, and the evaluation suite with its 120 graded questions over a synthetic personal corpus.

For the legal side — terms of use, privacy (what data goes where, precisely), and security disclosures — see [docs/legal/](docs/legal/).

## Contributing

The repo runs on conventional commits and a correctness-first bar: six invariants (cursor/transaction discipline, one active representation per file, retrieved-text-is-data, deterministic citations, no stale or unauthorized serving, network outside transactions) are enforced by tests and described in [CONTRIBUTING.md](CONTRIBUTING.md). All 313 tests run offline.

## License

MIT — see [LICENSE](LICENSE). Legal templates adapted from [General Legal](https://github.com/General-Legal/legal-templates) (CC0).
