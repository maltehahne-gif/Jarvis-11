# JARVIS HUD

The visual HUD from Blueprint 3 - a web frontend for the Core's local API and
WebSocket. Currently three of the eight modes from 3.2 are built: **Idle**,
**Mission** and **System**. The rest (News, Coding, Smart Home, Research) are
later increments with their own data sources; see `docs/ARCHITECTURE.md` at
the repo root for the full picture.

## Running it

The Core must already be running (`python -m jarvis` from the repo root,
default `http://127.0.0.1:8765`). Then:

```bash
npm install
npm run dev
```

Opens on `http://localhost:5173`. `VITE_JARVIS_API` (see `.env.development`)
points at the Core; the Core's CORS policy only allows local origins, never a
grant for remote access (see `api/server.py`'s middleware comment).

## Why no Tauri yet

Blueprint 4.3 names Tauri 2 as the native desktop/mobile shell. It needs
system libraries (GTK/webkit2gtk on Linux) this container does not have, so
for now the HUD is a plain web app, verified the same way the debug dashboard
is - in a real browser via Playwright. Wrapping it in Tauri later is a thin
step once a build environment has those libraries: the web app underneath
does not change.

## Structure

```
src/
  api/        REST client + WebSocket client (with reconnect)
  store/      zustand store - the seam between the live stream and components
  hooks/      useAnimationFrame - a render loop that never touches the network
  theme/      design tokens (Blueprint 3.1) + shared layout CSS
  components/ CoreOrb, DebugOverlay, IdleMode, MissionMode + mission/*,
              SystemMode + system/*
  types/      TypeScript types mirroring the Core's `to_dict()` JSON exactly
```

## The one rule that matters

Blueprint 3.3: no animation, input, or scrolling may block on a Claude
request. `useAnimationFrame` drives every animation from
`requestAnimationFrame`, reading only already-fetched state - never awaiting
anything. The `DebugOverlay`'s FPS counter is built from that same hook, so if
the render loop were ever secretly blocked on a fetch, the counter would show
it by dropping, not lie by staying frozen at 60.

## Testing

```bash
npx tsc -b --noEmit   # types
npx oxlint             # lint
npx vitest run          # unit tests (state logic, not rendering)
npm run build            # production build
```

Vitest covers store logic worth pinning down - notably that an out-of-order
network response can never roll a mission's displayed progress backwards (see
`store/events.test.ts`). Visual verification is Playwright screenshots against
a running Core, the same discipline `docs/ARCHITECTURE.md` describes for the
debug dashboard.
