# Fenris web UI

React 19 · TypeScript · Vite · Tailwind 4 · oxlint. The terminal-aesthetic
front end served by the engine's own API process (`make ui`), and bundled
into the desktop app.

## Layout

```
src/
├── components/   design-system primitives, citation cards, palette
├── lib/           API client, streaming helpers
├── pages/         dashboard · chat · sources · settings · search
└── main.tsx       router + app shell
```

## Commands

```bash
bun install          # once
bun run dev          # Vite dev server on :5173, proxying to the API on :8000
bun run build        # tsc + vite build -> dist/ (served by the engine)
bun run lint         # oxlint
```

The API process serves the production build from `dist/`, so `bun run build`
must run before `make ui` (the Makefile target chains it).

## Conventions

- Dark terminal aesthetic is the design system, not a theme toggle.
- Every answer state (ANSWERABLE / INSUFFICIENT / STALE / CONFLICTING /
  ERROR) must render honestly — never dress a non-answer as a guess.
- Citations resolve to evidence IDs from the API; the UI never invents
  source links.
