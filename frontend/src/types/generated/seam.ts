/**
 * The generated-types seam (§141, REQ-API-005).
 *
 * `npm run gen:api` writes `api.d.ts` into this directory from the backend OpenAPI
 * document (`/api/openapi.json`, see `scripts/gen-api.mjs`). That file is NOT committed —
 * see `.gitignore` — so it is absent in a fresh checkout, and this module must therefore
 * stay compilable with or without it. That is why it does not import `./api`; it
 * documents the exact recipe for using it.
 *
 * WHAT OPENAPI-TYPESCRIPT 7 ACTUALLY EMITS (verified by running it against the backend)
 *   export interface paths { ... }
 *   export interface components { schemas: { ApiEnvelope: { code: number; ... } } }
 *   export interface operations { ... }
 * i.e. NAMED interfaces — not one `ApiDefinition` object type.
 *
 * HOW TO MIGRATE A DTO (incremental, never a big-bang rewrite)
 *   1. `npm run gen:api`
 *   2. In `src/types/api-contract.ts`, add:
 *        import type { components } from '@/types/generated/api'
 *   3. Replace a hand-written interface with an alias, KEEPING THE NAME so no store or
 *      component has to change:
 *        export type ApiEnvelope<T> =
 *          Omit<components['schemas']['ApiEnvelope'], 'data'> & { data: T }
 *   4. `npm run typecheck`. Anything that relied on a field the generator does not emit
 *      now fails the BUILD instead of failing at runtime.
 *
 * WHY THE FROZEN ENUMS ARE NOT GENERATED
 *  `src/types/domain.ts` mirrors status enums the spec freezes (§105). Generating them
 *  would let a backend rename silently change the frontend's meaning, so they are
 *  ASSERTED instead: `src/types/__tests__/domain-mirror.spec.ts` parses the backend Python
 *  source and fails on drift in business codes, statuses or agent event names.
 *  Generation covers request/response DTO SHAPES; assertion covers MEANING.
 */

export {}
