// Records WHICH schema `src/api/schema.ts` was generated from (issue #1446).
//
// The generated types are only trustworthy while they track `openapi.json`, and the check that
// they do has to run somewhere. It cannot run in the Python unit lane — that lane has no node, so
// it cannot re-run the generator — and the UI lane is where node lives. So the generator records
// the hash of its input here, and `tests/unit/api/test_openapi_snapshot.py` compares that hash
// against the committed schema. Regenerating the snapshot without regenerating the types then
// fails the required lane, which is the mistake that actually happens.
//
// It is a stamp, not a checksum of the output: it proves the types were generated from THIS
// schema by the pinned generator, not that nobody hand-edited schema.ts afterwards. The
// component-name and "auto-generated" header assertions in that same test cover the hand-edit
// case, and `npm run check:api-types` re-runs the generator for an exact answer.

import { createHash } from 'node:crypto'
import { readFileSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const uiRoot = join(dirname(fileURLToPath(import.meta.url)), '..')
const schemaPath = join(uiRoot, 'openapi.json')
const stampPath = join(uiRoot, 'src', 'api', 'schema.stamp.json')

const pkg = JSON.parse(readFileSync(join(uiRoot, 'package.json'), 'utf8'))
const pinned = /openapi-typescript@[\d.]+/.exec(pkg.scripts['gen:api-types'])
if (!pinned) {
  console.error('gen:api-types no longer pins a generator version — refusing to stamp.')
  process.exit(1)
}

const stamp = {
  generator: pinned[0],
  source: 'openapi.json',
  source_sha256: createHash('sha256').update(readFileSync(schemaPath)).digest('hex'),
}
writeFileSync(stampPath, `${JSON.stringify(stamp, null, 2)}\n`)
console.log(`stamped src/api/schema.stamp.json (${stamp.generator}, ${stamp.source_sha256.slice(0, 12)}…)`)
