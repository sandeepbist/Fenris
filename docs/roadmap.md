# Fenris — Upgrade Roadmap

Date: 2026-09-02. Sources: full codebase scan (51 modules, 10.7k lines, 332 tests) + two research passes — the offline-RAG research notes (engine frontier, 122 citations) and the product-frontier research notes (product frontier, 64 citations). Every precedent cited there; this document is the synthesis and the build order.

The roadmap follows one rule inherited from the original plan: correctness and trust before novelty. Nothing below sacrifices the citation contract, the state ladder, or the local-first architecture.

---

## Phase 0 — polish what exists (days per item)

| # | Upgrade | Why it's first | Sketch |
|---|---|---|---|
| 0.1 | **Streaming answers (SSE)** | The most-felt gap: a 10–30s "thinking…" wait reads as broken by 2026 norms; both Gemini and Anthropic define SSE as the standard surface. `generate_stream` already exists in the generator — the API never wires it. | `POST /api/ask/stream` (SSE) → sentence-batched deltas; stop + regenerate buttons; the UI renders tokens as they arrive. |
| 0.2 | **Persisted conversations** | Every reload erases the chat. Every competitor persists sessions; this is the biggest anomaly in the current UI. | `conversations` + `turns` tables; a chats sidebar (rename, pin, delete); the chat page hydrates from DB. |
| 0.3 | **Jump-to-source citations** | Citations currently end at a quote card. The most-cited UX pattern in this space is: click a citation → see the document, scrolled to the cited span. The spans are already stored. | A document viewer (fetches source text from the projection), span highlighted, context above/below; evidence_id → (file, char span) already resolves. |
| 0.4 | **`fenris doctor`** | One command that checks keyring backend, API key, Drive auth, quota state, schema version, disk space — and prints what's wrong. Kills most support burden before it exists. | A diagnostics module + CLI command; the Settings page shows the same checklist. |
| 0.5 | **Keyboard-first pass** | A terminal-aesthetic app shipping zero shortcuts is a brand contradiction. AnythingLLM ships F1 cheat sheets; Raycast is a hotkey. | ⌘K command palette (navigate, ask, sync, resync), ⌘⇧? shortcut list, focus rings, tab order. Pure frontend. |
| 0.6 | **Onboarding wizard** | Today a non-technical user faces Makefile + venv + env vars. The first-run card exists; it needs to *walk* a user: key → auth → first sync with progress → sample question. | A first-run flow in the UI driven by `/api/health` states; nothing new engine-side. |

## Phase 1 — the majors (weeks per item)

| # | Upgrade | Why | Sketch |
|---|---|---|---|
| 1.1 | **Desktop app (Tauri v2 + PyInstaller sidecar)** | The current install path excludes non-technical users entirely. Tauri v2's sidecar docs name the exact pattern ("Python CLI apps bundled via pyinstaller"); minimal Tauri shells are <600KB — the Python sidecar dominates size, no Chromium. Updater plugin covers all three platforms with signing. Verified installer sizes: Electron-class competitors run 55–861MB; Fenris has no bundled model so it lands well below. | Tauri shell → spawns the PyInstaller-frozen Fenris engine → loads the web UI. `make ui` stays for developers. Sequencing: CLI → curl-installer → Tauri after onboarding (0.6) exists. |
| 1.2 | **Local-model fallback (offline mode)** | API-key dependence is the #1 fragility of a "local-first" promise. The models are already chosen (prior research): Qwen3-Embedding-0.6B via ONNX for offline retrieval, Qwen3-4B-Instruct-2507 Q4_K_M via llama.cpp for offline answers. Khoj ships exactly this toggle; AnythingLLM's router does it automatically with sticky routing. | An engine provider abstraction (Gemini | local), a Settings toggle, "answered by: local/API" badge on each turn; state ladder unchanged — INSUFFICIENT stays INSUFFICIENT offline. |
| 1.3 | **Search page (find-the-file)** | Chat is not the only retrieval verb; "where is that document" is half of what Drive users want. Engine already has BM25+KNN+diversity. | Debounced search-as-you-type; results grouped by file with snippet + match highlight; filter chips (mime, folder, date); Enter → jump-to-source (0.3). |
| 1.4 | **Corpus scoping + strictness toggle** | AnythingLLM's Query-vs-Chat mode and Khoj's `/notes` scoping make "only my files, strictly" a visible user control. For Fenris it's a natural extension of the honest-answers ladder. | Composer controls: scope-to-folder/doc-set; strict (evidence-only) vs standard toggle, reflected in the answer-state badge. |
| 1.5 | **Connector framework** | The sync layer's Drive adapter is clean and narrow; Notion/local-folder connectors slot in as new adapters. The architecture rule (connectors sync *into* the index, never queried live) is already settled by the local-first research. | `sync/` gains an adapter protocol (list, changes, fetch, export-as); Drive becomes the first implementation; local-folder second (it's free — no OAuth); Notion third. Limits verified: Notion 3 req/s; Jira per-tenant hourly quotas from March 2026. |
| 1.6 | **Chat export + share** | Answers over Drive docs are work products. Export thread → Markdown; a self-contained HTML share view with citations intact. | Pure UI + a serializer; citations render as links to spans. |

## Phase 2 — frontier (benchmark-gated, novel)

| # | Upgrade | The case |
|---|---|---|
| 2.1 | **Versioned-document QA** — "what changed between v1 and v3 of this contract?" | **No shipped precedent found** (UNVERIFIED that anyone does it) — and Fenris already stores retired revisions with exact identity. The corpus keeps the raw material; the missing piece is a diff-QA answer state + UI affordance. Genuinely novel differentiation. |
| 2.2 | **Citations during streaming** — evidence IDs resolved per sentence as tokens arrive | No mainstream product anchors *verified* citations mid-stream. The published path exists (CAGE's attribution-map-before-generation, arXiv:2607.24236; Attribute-First-then-Generate, arXiv:2403.17104) and Fenris's post-hoc renderer is already the enforcement point. |
| 2.3 | **Related-files panel** — from any document, "what else relates" | Smart Connections' core loop; Obsidian's whole graph premise. Vector-similar + co-cited; the engine has both signals. |
| 2.4 | **Scheduled digests** — "every Monday 8:00: what changed in my Drive this week" — ✅ shipped | Converts pull into push. AnythingLLM's Scheduled Jobs proved the shape. Shipped as: a projection-only digest engine (no network, no LLM — windows over `sources.created_at` with chained anchors so nothing double-reports, honest new/updated/removed/problems/skipped buckets), a cron-subset scheduler daemon inside the API process (`fenris digest` CLI + `/api/digests` for parity; external cron gets the identical stateless `run-due`). The desktop app (1.1) will make the host literally always-on. |
| 2.5 | **Multi-vault (personal + work Drive)** — ✅ shipped | Namespacing already existed in the schema (`drive_id`); the work was second OAuth identity + a vault picker. Shipped as: a vault registry (lazy `vaults` table; `"mydrive"` keeps its existing keyring account so existing installs migrate transparently), `--vault` on drive auth/disconnect/status + sync + ask, `fenris vault ls/rm`, per-vault keyring accounts (`drive-refresh:<id>`), sync threading with an explicit `api_drive_id` boundary (each vault syncs its own account's My Drive), retrieval vault filter post-fusion (vault-restricted miss = honest INSUFFICIENT; one vault's evidence can never leak into another's answer), `/api/vaults` CRUD with live token presence, and vault chips in the chat composer (rendered only when a second vault exists). |

## Deliberately not on the roadmap

- **Audio overviews, mind maps, flashcards** — sticky for study products, off-mission for a citation-enforced Drive QA tool.
- **Notes-editor features** — Reor (archived 2026-03-07) is the cautionary tale for drifting into the notes market. Differentiate on Drive sync + citation enforcement.
- **In-app model marketplace/downloads** — multi-GB model management is its own product; local models arrive via the offline mode (1.2), not a browser inside Settings.
- **Rust rewrite** — settled by research: extract hot paths only if profiling demands; every Python→Rust success was an extraction, not a rewrite.
- **Agent workflows/no-code flows** — listed for completeness; low fit with the honest-answers positioning.

## Sequencing logic

Phase 0 first because each item is days and compounds: streaming (0.1) is half of 2.2; conversations (0.2) is half of 1.6 and 2.4; jump-to-source (0.3) is half of 1.3. Phase 1 items are independent of each other and can interleave. The desktop app (1.1) gates the global-hotkey overlay and scheduled digests. Everything in Phase 2 ships behind the same eval gates the engine already enforces: a technique that raises recall but breaks citations or freshness is a regression, not an upgrade.
