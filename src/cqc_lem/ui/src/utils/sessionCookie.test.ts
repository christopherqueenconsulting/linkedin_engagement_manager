import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  SESSION_COOKIE_NAME,
  clearFallbackSessionCookie,
  writeFallbackSessionCookie,
} from './sessionCookie'

// jsdom exposes only the resulting jar on a `document.cookie` READ, and the attributes are the whole
// point here — `Secure` in particular has to be ABSENT, because a refused `Secure` cookie is the
// failure this module exists for. So spy on the setter and assert what was asked for.
function captureWrites(): string[] {
  const written: string[] = []
  vi.spyOn(document, 'cookie', 'set').mockImplementation((value: string) => { written.push(value) })
  return written
}

afterEach(() => { vi.restoreAllMocks() })

describe('the cookie-less fallback credential (#1611)', () => {
  it('writes the real token under the name the server reads', () => {
    const written = captureWrites()

    writeFallbackSessionCookie('abc123')

    expect(written).toHaveLength(1)
    expect(written[0].startsWith(`${SESSION_COOKIE_NAME}=abc123;`)).toBe(true)
  })

  it('is first-party, site-wide and NOT Secure — the refused Secure cookie is the bug', () => {
    const written = captureWrites()

    writeFallbackSessionCookie('abc123')

    expect(written[0]).toContain('Path=/')
    expect(written[0]).toContain('SameSite=Lax')
    expect(written[0]).not.toContain('Secure')
  })

  it('outlives the browser session, or the next boot lands back on the 401', () => {
    const written = captureWrites()

    writeFallbackSessionCookie('abc123')

    // The server's own SESSION_ABSOLUTE_MAX_DAYS default.
    expect(written[0]).toContain(`Max-Age=${30 * 24 * 3600}`)
  })

  it('writes nothing without a token', () => {
    const written = captureWrites()

    writeFallbackSessionCookie('')

    expect(written).toHaveLength(0)
  })

  it('expires the cookie on the same path it was written to', () => {
    const written = captureWrites()

    clearFallbackSessionCookie()

    expect(written).toHaveLength(1)
    expect(written[0]).toContain(`${SESSION_COOKIE_NAME}=;`)
    expect(written[0]).toContain('Max-Age=0')
    expect(written[0]).toContain('Path=/')
  })
})
