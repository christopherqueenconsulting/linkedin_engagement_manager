import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

// The source half of the code-splitting proof (issue #1300). `ui-build.yml` runs `npm test` BEFORE
// `npm run build`, so no vitest test can ever look at `dist/` — the byte-level assertion lives in
// that workflow, after the build step. This one catches the far more likely regression anyway: a
// later PR adding `import Dashboard from './pages/Dashboard'` back to the top of App.tsx, which
// silently pulls the whole authenticated app back into the marketing page's entry chunk.

const APP = readFileSync(join(process.cwd(), 'src', 'App.tsx'), 'utf8')
const LAZY_ROUTES = ['Dashboard', 'Account', 'Avatars', 'ContentStudio', 'AdminFeedbackPage']

describe('the authenticated app is code-split (issue #1300)', () => {
  it('statically imports none of the authenticated screens', () => {
    for (const page of LAZY_ROUTES) {
      expect(
        new RegExp(`import\\s+${page}\\s+from\\s+'\\./pages/`).test(APP),
        `App.tsx statically imports ${page}`,
      ).toBe(false)
    }
  })

  it('loads each of them through the stale-chunk recovery helper', () => {
    for (const page of LAZY_ROUTES) {
      expect(APP).toContain(`const ${page} = lazyWithChunkRecovery(() => import('./pages/${page}'))`)
    }
  })

  // A lazy route with no boundary above it throws rather than suspending, and neither App.tsx nor
  // Layout.tsx had one before this change.
  it('puts a Suspense boundary above the lazy routes', () => {
    expect(APP).toContain('<Suspense')
    const layout = readFileSync(join(process.cwd(), 'src', 'components', 'Layout.tsx'), 'utf8')
    expect(layout).toContain('<Suspense')
  })

  it('still imports the marketing page eagerly — it is the first paint', () => {
    expect(APP).toMatch(/import Landing from '\.\/pages\/Landing'/)
  })
})
