# Nexora Commerce — Frontend (Vue 3 + TypeScript)

One Vue 3 application serving **two shells**: the consumer store (`/`) and the merchant
console (`/console`), plus the AI workspace. Composition API with
`<script setup lang="ts">` everywhere.

## Quick start

```bash
npm install
cp .env.example .env.local     # optional; the defaults work for local dev
npm run dev                    # http://127.0.0.1:5173
```

In development Vite proxies `/api` to `http://127.0.0.1:8000` so the SPA and the API
share an origin exactly as they do in production (Nginx, one origin on `:18000`).

## Scripts

| Script | What it does |
| --- | --- |
| `npm run dev` | Vite dev server on 5173 (`--strictPort`, so a port clash fails loudly). |
| `npm run build` | `vue-tsc --noEmit` **then** `vite build`. Type errors fail the build. |
| `npm run typecheck` | `vue-tsc --noEmit` alone. |
| `npm run test` | Vitest unit suite. |
| `npm run test:e2e` | Playwright (`e2e/`), starts the dev server if needed. |
| `npm run lint` | ESLint flat config over `src/`, `tests/`, `e2e/` and the config files. |
| `npm run gen:api` | Regenerates `src/types/generated/api.d.ts` from the backend OpenAPI document. |

`gen:api` needs a running backend on `127.0.0.1:8000`; it is intentionally NOT part of
`build`, so a build never depends on a live server.

## Architecture

```
src/
  api/          THE single HTTP client + one module per backend bounded context
  agent/        the SSE consumer and the 12 frozen agent events
  charts/       ChartSpec validation + ChartSpec -> ECharts options
  components/   ui/ (state, chips, cards), charts/, agent/ (blocks, approval card)
  composables/  useAsyncState — the bridge between an API call and the §108 states
  config/       constants and app-level metadata
  directives/   v-permission / v-can (UX only, see below)
  layouts/      StoreLayout (consumer) and ConsoleLayout (merchant)
  router/       both shells in one route table
  stores/       exactly six Pinia stores
  styles/       design tokens (CSS custom properties) + shared primitives
  types/        frozen domain enums, transport contract, ChartSpec
  utils/        money, trace-id
```

### Rules this codebase does not bend

1. **One HTTP client.** Every request goes through `src/api/client.ts`. Components never
   import axios. The client centrally owns the base URL, timeout, auth header,
   `X-Trace-Id` propagation, and the 401 → refresh retry.
2. **Single-flight refresh.** Concurrent 401s share ONE refresh. The backend rotates
   refresh tokens and treats reuse as a security event, so a refresh stampede would log
   the user out for real. Covered by `src/api/__tests__/client.refresh.spec.ts`.
3. **Money is integer minor units** (cents) everywhere — types, stores, API payloads.
   Conversion to major units happens only in `src/utils/money.ts` and in the chart
   value formatter. `formatMoney` uses integer arithmetic, never `amount / 100`.
4. **The server is authoritative.** The UI never computes a payable amount, never decides
   whether a transition is legal, and never marks an order paid. It renders what the
   server returned and reacts to business error codes.
5. **Permission is UX only.** Route guards, menu filtering and `v-permission` decide what
   to DISPLAY. They are not a security boundary — the backend re-checks every request.
   Each usage site carries a comment saying so.
6. **Exactly six Pinia stores** (`auth`, `permission`, `cart`, `app`, `aiThread`,
   `notification`). Page-local state stays in the page.
7. **The agent never emits code.** A chart arrives as a declarative `ChartSpec`, is
   validated by `sanitizeChartSpec`, and only then becomes ECharts options. There is no
   `eval`, no `new Function`, and no `v-html` anywhere in the chart or message path. An
   invalid spec renders as an Error block.
8. **No chain-of-thought.** Only the 12 frozen agent events exist, none of them carries
   reasoning, and the UI has nothing to display even if one did.

### UI state contract

`src/components/ui/StateView.vue` implements every state in one place, so a page cannot
forget one:

* core pages — Loading, Success, Empty, Error, PermissionDenied
* agent surfaces — Streaming, WaitingApproval, Failed, Cancelled
* knowledge surfaces — Processing, Failed

`useAsyncState` derives `permission_denied` from the mapped error, so a 403 is handled by
the same code path as any other failure rather than being special-cased per page.

## Toolchain notes (read before bumping versions)

The pinned set is deliberately exact; `npm install` must not float it.

* **TypeScript is pinned to 6.0.3, not 7.0.2.** `vue-tsc` 3.3.11 resolves
  `typescript/lib/tsc`, which TypeScript 7 no longer exports
  (`ERR_PACKAGE_PATH_NOT_EXPORTED`), so the type check cannot run on TS 7 at all.
* **`baseUrl` is not used** in `tsconfig.json`: it is deprecated in TypeScript 6 and
  removed in TypeScript 7. Path aliases are relative to the tsconfig instead.
* **Vite 8 builds with Rolldown.** The object form of `output.manualChunks` no longer
  exists; chunking uses `build.rolldownOptions.output.codeSplitting.groups`.
* **`openapi-typescript` runs via `npx`**, not as a dependency: its peer range is
  `typescript@^5.x` and this project is on 6.x, so keeping it out of `package.json` keeps
  `npm install` clean while `npm run gen:api` still works.
* **ESLint has no type-aware rules.** `@typescript-eslint/parser` declares
  `typescript >=4.8.4 <6.1.0`; `vue-tsc` is the authoritative type check, so lint focuses
  on syntax and correctness rules that need no type information.

## Backend contract

Types in `src/types/` are a hand-written mirror of the backend. The mirror cannot silently
rot: `src/types/__tests__/domain-mirror.spec.ts` parses
`backend/app/core/errors.py` and fails if any business code name or value differs. It also
asserts the frozen status enums and the 12 agent event names.
