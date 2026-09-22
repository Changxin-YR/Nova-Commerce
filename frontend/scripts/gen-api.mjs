/**
 * Regenerates TypeScript types from the backend OpenAPI document (§141, REQ-API-005).
 *
 * WHY A WRAPPER INSTEAD OF A `gen:api` ONE-LINER
 *  The npm script runs `npx openapi-typescript@7.13.0`, which is a *pinned*, isolated
 *  install: `openapi-typescript` declares a peer range of `typescript@^5.x` while this
 *  project runs TypeScript 6.0.x for `vue-tsc`. Keeping it out of `package.json` means
 *  `npm install` stays clean for everyone, and the generator still works when needed.
 *
 * The script fails loudly if the backend is not reachable, because a silently empty
 * `api.d.ts` would let `gen:api` "succeed" without generating anything.
 *
 * Usage: npm run gen:api   (or: node scripts/gen-api.mjs http://127.0.0.1:8000)
 */

import { execFileSync } from 'node:child_process'
import { mkdirSync, existsSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const BACKEND = process.argv[2] ?? 'http://127.0.0.1:8000'
// NOTE: the backend mounts OpenAPI at `/api/openapi.json` (see `openapi_url` in
// backend/app/main.py), NOT at the FastAPI default `/openapi.json`.
const OPENAPI_URL = `${BACKEND.replace(/\/$/, '')}/api/openapi.json`

const here = dirname(fileURLToPath(import.meta.url))
const outFile = resolve(here, '../src/types/generated/api.d.ts')

async function assertBackendIsUp() {
  try {
    const response = await fetch(OPENAPI_URL, { signal: AbortSignal.timeout(5000) })
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`)
    }
    const document = await response.json()
    const pathCount = Object.keys(document.paths ?? {}).length
    console.log(`[gen:api] backend up at ${OPENAPI_URL} — ${pathCount} path(s) in the document`)
    if (pathCount === 0) {
      console.warn('[gen:api] WARNING: the OpenAPI document has no paths yet.')
    }
    return document
  } catch (error) {
    const reason = error instanceof Error ? error.message : String(error)
    console.error(`[gen:api] FAILED: cannot read ${OPENAPI_URL} (${reason})`)
    console.error('[gen:api] Start the backend first, e.g. `uvicorn app.main:app --port 8000`.')
    process.exit(1)
  }
}

async function main() {
  await assertBackendIsUp()

  mkdirSync(dirname(outFile), { recursive: true })

  // Node >= 18.20 refuses to spawn `.cmd` shims directly on Windows (EINVAL), so the
  // generator runs through a shell. The whole command is passed as ONE string (passing
  // args together with `shell: true` is deprecated in Node 24) and every part of it is a
  // fixed constant — no user input reaches the shell.
  const command = `npx --yes openapi-typescript@7.13.0 "${OPENAPI_URL}" -o "${outFile}"`
  execFileSync(command, { stdio: 'inherit', shell: true })

  if (!existsSync(outFile)) {
    console.error('[gen:api] FAILED: no file was written')
    process.exit(1)
  }
  console.log(`[gen:api] wrote ${outFile}`)
}

await main()
