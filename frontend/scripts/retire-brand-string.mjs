// Removes the last statements of the retired product name from the docs, so the
// captain's verification command returns EMPTY as required.
//
// The facts worth keeping are preserved, without the literal retiree:
//   * a storage-key rename happened, and it logs out existing sessions once;
//   * the rename covered package name, title, storage keys, log prefix, identifiers.
// Neither sentence needs the old brand string to be useful.
import { readFileSync, writeFileSync } from 'node:fs'

const jobs = [
  {
    file: 'README.md',
    from: '### Deploy note — the brand rename invalidated existing client sessions\n\nRenaming the product to Nova also renamed the `localStorage` keys (`nexora.*` → `nova.*`,\nsee `STORAGE_KEYS` in `src/config/constants.ts`).',
    to: '### Deploy note — the rename invalidated existing client sessions\n\nThe product rename also renamed the `localStorage` keys (see `STORAGE_KEYS` in\n`src/config/constants.ts`).',
  },
  {
    file: 'REDESIGN_STATUS.md',
    from: '## Brand rename (owner decision): Nexora → Nova\n\nDone in `frontend/` only. **68 occurrences across 15 source/config/doc files**, in two\ncase-sensitive passes (`Nexora`→`Nova`, `nexora`→`nova`): package name, `index.html` title,',
    to: '## Brand rename (owner decision)\n\nDone in `frontend/` only. **68 occurrences across 15 source/config/doc files**, in two\ncase-sensitive passes: package name, `index.html` title,',
  },
]

for (const job of jobs) {
  const text = readFileSync(job.file, 'utf8')
  if (!text.includes(job.from)) {
    console.error(`NOT FOUND in ${job.file} — aborting so nothing is silently skipped`)
    process.exit(1)
  }
  writeFileSync(job.file, text.replace(job.from, job.to), 'utf8')
  console.log(`rewrote ${job.file}`)
}

// REDESIGN_STATUS also has a mention in the "Deliberately NOT renamed" prose.
const status = 'REDESIGN_STATUS.md'
let s = readFileSync(status, 'utf8')
s = s.replace(
  '"Deploy consequence:" the `localStorage` key rename logs out any browser holding a token\nunder the old keys, once.',
  '**Deploy consequence:** the `localStorage` key rename logs out any browser holding a token\nunder the old keys, once.',
)
writeFileSync(status, s, 'utf8')
console.log('done')
