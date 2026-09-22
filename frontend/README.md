# ZORY Agent Console

A production-grade **React + TypeScript + Vite** console for exercising and
demoing the ZORY AI Commerce Agent's `POST /v1/chat` endpoint. It renders every
branch of the response contract — assistant prose, follow-up questions, product
cards, comparison tables, whole-room bundles (with a budget bar), clarifications,
and typed errors — each with a collapsible **raw response** for debugging.

## Quick start

```bash
cd frontend
npm install
npm run dev        # → http://localhost:3000
```

Then set **store_id** in the sidebar (the bundled catalog is store **50**) and
chat. The console auto-generates a `session_id`; "New session" starts a fresh
conversation.

## How it talks to the backend (no CORS needed)

The FastAPI service only enables CORS when `ZORY_API__CORS_ORIGINS` is set, and
the working `.env` leaves it empty. Rather than require a backend change, Vite
**proxies** `/v1` and `/health` to the service server-side, so the browser makes
same-origin calls and CORS never applies.

- Default proxy target is `http://localhost:8000`.
- Override without editing code: `VITE_PROXY_TARGET=http://host:port npm run dev`
  (or copy `.env.example` → `.env.local`).
- Leave the sidebar **Connection** field blank to use the proxy. Enter an
  absolute URL there only to call a backend directly (which then needs CORS).

## What you'll see depends on the backend

The UI faithfully renders whatever `/v1/chat` returns. For real products and
bundles the service must be answerable: a catalog database for the store, Redis,
an LLM key, and the customer-agent models configured. If any of that is missing
the agent replies with a normal message (e.g. *"the customer agent is not
configured"*) — the console shows it cleanly. Use the sidebar **health** panel
to confirm PostgreSQL/Redis are reachable first.

## Scripts

| Command | Purpose |
| --- | --- |
| `npm run dev` | Vite dev server with API proxy + HMR |
| `npm run build` | Type-check (`tsc -b`) and production build to `dist/` |
| `npm run preview` | Serve the production build locally |
| `npm run typecheck` | Type-check only |

## Structure

```
src/
  api/       types.ts (wire contract) · client.ts (typed fetch, discriminated results)
  hooks/     useChat · useConfig (localStorage) · useHealth
  lib/       format.ts (Decimal-as-string money/dims) · quickReplies.ts · session.ts
  components/
    ui/            Pill (outlined status/stat pills)
    presentation/  ProductCard · ProductGrid · ComparisonTable · RoomBundle
    icons.tsx      one line-icon set (24px grid, 1.75 stroke, currentColor)
    TopNav · SettingsPopover · HealthBadge   (chrome + connection/session config)
    ChatPanel · MessageBubble · Composer · QuickReplies · EmptyState · …
```

## Design system (NORA-inspired)

Warm off-white canvas, terracotta accent, dark warm-neutral text, thin neutral
borders, very soft shadows, restrained motion. All tokens are centralised in
`src/index.css` under `@theme` (Tailwind CSS v4 via `@tailwindcss/vite`) and
generate their own utilities — re-point a value there and the whole app re-skins:

| token | value | token | value |
| --- | --- | --- | --- |
| `--color-canvas` | `#f8f5f0` | `--color-clay` (primary) | `#ad5637` |
| `--color-surface` | `#ffffff` | `--color-clay-hover` | `#98472e` |
| `--color-surface-muted` | `#f2ece6` | `--color-clay-soft` | `#ead8d0` |
| `--color-ink` | `#26221f` | `--color-line` | `#ded8d2` |
| `--color-muted` | `#716b66` | `--color-line-strong` | `#c8bfb7` |

Notes for maintainers:

- Monetary and dimensional values arrive as JSON **strings** (Pydantic v2
  serialises `Decimal`), so `lib/format.ts` parses them only for display.
- The exact/​widened badge is derived from `relaxation_depth` (0 = exact),
  because the backend's `matched_exactly` is a computed property and not on the
  wire.
- Quick-reply chips are derived client-side from the agent's free-text question
  (`lib/quickReplies.ts`), since the backend returns no structured options.
