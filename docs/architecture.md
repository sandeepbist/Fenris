# Fenris architecture

How the system is built and why each decision was made. Written to be read standalone — no internal planning documents required.

## The model

Fenris is a local read projection over a Google Drive you own.

```
Google Drive  ──(change feed, downloads/exports)──►  Sync
                                                        │
                                                        ▼
                     SQLite projection ◄────────── Ingest (parse/chunk/embed)
                     sources · chunks · FTS5 · vec0
                     tables · drive_files · cursor
                        │                    │
                        ▼ (BM25)             ▼ (KNN)
                     Retrieve ── RRF ── Rerank ── Adaptive-k ── Diversity
                        │
                        ▼
                     Verify sources ── State ladder ── Prompt ── Generate
                        │
                                                        ▼
                                          Citation render ── Answer + state
```

Drive is authoritative. The database is rebuildable from Drive plus code at any time, which shapes everything: identity is content revision, not file identity; nothing Fenris writes to Drive is load-bearing; deleting the local DB loses no documents, only re-ingest time.

## Identity and revision

Every indexed representation is keyed by a tuple of (drive, file, **content revision**, representation MIME, parser version, chunker version, embedding model). Drive's `version` field is a change-detection hint, never identity — a rename bumps `version` with unchanged bytes, and re-embedding an unchanged file would be pure waste.

- Binary files: revision = `md5Checksum`.
- Google Workspace files (Docs/Sheets/Slides): no checksum exists; the export bytes are hashed (`exp:sha256`), and the hash is the identity.
- Consequence: a rename or move costs one metadata upsert. A content edit produces a new representation and retires the old one in the same transaction. A file can never have two active representations (partial unique index enforces it).

## Sync: the correctness loop

The change feed (`changes.list`) is the truth; a webhook would only be a wake-up signal, so none is used.

1. `getStartPageToken` is persisted **before** enumeration, so changes landing during a multi-hour scan are replayed, never lost.
2. Each change page is classified without network: removed → tombstone (covers deletion and loss-of-access); trashed → retire; version-bump-with-same-md5 → metadata-only upsert; checksum change → re-ingest.
3. Network work (download/export) happens outside any transaction. One short transaction per page applies writes and advances the token together. A crash between pages replays the page idempotently — every write is keyed.
4. `full_resync` is the escape hatch: fresh token, re-enumeration, and a reconciliation diff that tombstones anything locally active but gone from Drive.

Permission denials are isolated: one forbidden file marks itself unreadable and the drain continues.

## Ingest

- **Routing** decides per MIME: parse directly (docx/xlsx/pptx/text/markdown/HTML/EPUB), export-then-parse (Workspace formats), or skip with a recorded reason (audio, video, archives… — tracked so renames stay visible, never indexed).
- **Parsing** is Docling, bounded (pages, bytes, seconds), never raising — failures become `status='failed'` rows and the loop continues.
- **Chunking** splits at 512 tokens, zero overlap (measured to add indexing cost without retrieval benefit), with parent chunks linked but only children embedded. Tables never go through the text splitter: each becomes a typed record plus row-block chunks (~8 rows, header prepended), so a hit cites an exact region.
- **Headers** are deterministic (`title / path / section`) rather than LLM-generated — not mainly to save cost, but because reproducible headers keep the representation key stable across rebuilds.
- **Embeddings**: gemini-embedding-001 at 1536 dims, L2-normalized in code, batched, checkpointed. Identical chunk hashes reuse stored embeddings — re-ingesting an unchanged corpus costs nothing.

## Retrieval

- **Routing** is a deterministic policy table: EXACT (quoted phrase / rare term / filename) goes BM25-first and skips the reranker; TABLE restricts to table chunks; everything else runs the hybrid path; AGENTIC is triggered by post-rerank score floors, not guessed in advance.
- **Hybrid search**: FTS5 (porter + unicode61, bm25) and sqlite-vec KNN run against active, untrashed, current-epoch chunks — the filters are vec0 metadata columns applied during the scan, not post-hoc. RRF (`1/(60+rank)`) fuses them without score-scale distortion.
- **Adaptive-k** selects the final count from the reranked 30 by score shape (elbow on normalized scores, or cumulative mass), clamped [1, 25] — instead of a fixed k that either pads simple questions or truncates hard ones.
- **Diversity** caps per-file contribution so one large document cannot crowd out the evidence set.

## The answer layer

- **Verification before generation**: cited sources are checked against the projection; vanished items are dropped silently (revealing the existence of inaccessible content is itself a leak), stale ones trigger one refresh then a STALE verdict.
- **State ladder** (strict order): ERROR wraps everything; STALE persists after one refresh; INSUFFICIENT on empty evidence or below a score floor; CONFLICTING when two strong, rare-term-overlapping, revision-differing candidates disagree — both are then presented, never majority-voted; else ANSWERABLE.
- **Prompt layout** puts the strongest evidence at the top and bottom (position-biased attention) and wraps every evidence block in an explicit untrusted-data fence. The system prompt states that retrieved text is data, never instructions.
- **Citations** are resolved by a deterministic renderer: the model emits evidence IDs only, and any ID that doesn't resolve to retrieved evidence is dropped and logged as an unverifiable claim. Invented file names, pages, or quotes cannot reach the output.
- **Agentic retry** is bounded (4 rounds, 32 opens, 20 s) and deterministic — it reformulates from the missing-evidence signal rather than calling the model per round; the model sees only the final assembled prompt.

## Evaluation

The golden suite is 120 graded questions over a seeded, byte-reproducible synthetic corpus (invoices, meetings, versioned project docs, near-duplicate hard negatives, an expenses table). Metrics (ir-measures for recall/MRR/nDCG with graded relevance; hand-rolled span-recall and state-correctness) are reported **per slice** — exact, temporal, multi-hop, paraphrase, hard-negative, unanswerable, table — because an average hides exactly the failure modes that matter. The runner takes an injected retrieval callable, so it measures the real stack without importing it.

A deliberately naive baseline run proves the harness can't be gamed: keyword retrieval scores recall@5 ≈ 0.84 while span-recall is 0.0 and every unanswerable case fails.

## What is deliberately absent

| Absent | Why |
|---|---|
| ANN graph indexes (HNSW, DiskANN-style) | Brute-force KNN over a personal corpus (~tens of thousands of chunks) is sub-100 ms; the complexity buys nothing until scale measurements say otherwise |
| Quantization funnels, io_uring, WASM | Same reason: measured gates before machinery |
| Learned routers/cache eviction | Deterministic policies first; learned ones need traces that only exist after real usage |
| Multi-vector/visual retrieval | Storage cost is severe and the text path covers the formats in scope; revisit with measurements |
| Webhooks | A desktop process has no public endpoint, and the change feed is authoritative anyway |
| Local DB encryption | A single-user desktop DB guarded by the OS user is theater; OS-level protection is the honest layer |

Each of these is a deferral with a reopen condition, not a rejection.
