import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react'
import type { ReactNode } from 'react'
import App from '../App'
import { SHOWN_PLANS } from '../constants/plans'

// The front page had no test at all before issue #1300 — 59 test files under `ui/src` and zero on
// the one surface a prospect sees before signing up. These render the WHOLE logged-out route tree
// (App, not Landing on its own) because the defects being guarded are structural: the duplicate nav
// and footer only existed once Landing was placed inside the app's Layout.

const auth = {
  user: null as { userId: number; email: string } | null,
  isLoading: false,
  isAdmin: false,
  enrollmentRequired: false,
  isLoginModalOpen: false,
  openLoginModal: vi.fn(),
  closeLoginModal: vi.fn(),
  logout: vi.fn(),
  sessionToken: null,
}

vi.mock('../contexts/AuthContext', () => ({
  AuthProvider: ({ children }: { children: ReactNode }) => <>{children}</>,
}))
vi.mock('../contexts/useAuth', () => ({ useAuth: () => auth }))

const captured: { event: string; properties?: Record<string, unknown> }[] = []
vi.mock('../utils/analytics', async () => {
  const actual = await vi.importActual<typeof import('../utils/analytics')>('../utils/analytics')
  return {
    ...actual,
    capture: (event: string, properties?: Record<string, unknown>) =>
      captured.push({ event, properties }),
    capturePageview: vi.fn(),
  }
})

vi.mock('../api/client', () => ({
  default: { get: vi.fn(() => Promise.resolve({ data: {} })), post: vi.fn() },
}))
vi.mock('../hooks/useAppInfo', () => ({ useAppInfo: () => ({ data: undefined }) }))
vi.mock('../hooks/useFeatureFlags', () => ({
  FLAGS: { tutorialVideos: 'tutorial-videos-enabled', brandShowcase: 'brand-showcase-enabled' },
  useFeatureFlag: () => false,
}))
vi.mock('../components/NewVersionNotice', () => ({ default: () => null }))
vi.mock('../components/LoginModal', () => ({ default: () => <div data-testid="login-modal" /> }))
// The authenticated screens are lazy now; loading them for real would drag their whole data layer
// into a marketing-page test.
vi.mock('../pages/Dashboard', () => ({ default: () => <div data-testid="dashboard" /> }))
vi.mock('../pages/Account', () => ({ default: () => null }))
vi.mock('../pages/Avatars', () => ({ default: () => null }))
vi.mock('../pages/ContentStudio', () => ({ default: () => null }))
vi.mock('../pages/AdminFeedbackPage', () => ({ default: () => null }))

// jsdom has no IntersectionObserver. The section-viewed hook skips silently without one, so the
// tests that assert the event install a stub that reports every observed section as visible.
class ImmediateIntersectionObserver {
  private callback: IntersectionObserverCallback
  constructor(callback: IntersectionObserverCallback) {
    this.callback = callback
  }
  observe(target: Element) {
    this.callback(
      [{ isIntersecting: true, target } as unknown as IntersectionObserverEntry],
      this as unknown as IntersectionObserver,
    )
  }
  unobserve() {}
  disconnect() {}
  takeRecords(): IntersectionObserverEntry[] {
    return []
  }
}

function useImmediateObserver() {
  vi.stubGlobal('IntersectionObserver', ImmediateIntersectionObserver)
}

beforeEach(() => {
  auth.user = null
  auth.isLoading = false
  captured.length = 0
  window.history.pushState({}, '', '/')
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

function renderApp() {
  return render(<App />)
}

describe('logged-out front page structure (issue #1300)', () => {
  it('renders exactly one nav, one main and one footer', () => {
    const { container } = renderApp()
    expect(container.querySelectorAll('nav')).toHaveLength(1)
    expect(container.querySelectorAll('main')).toHaveLength(1)
    expect(container.querySelectorAll('footer')).toHaveLength(1)
  })

  it('has one sticky top-0 element, so nothing sits under two stacked bars', () => {
    const { container } = renderApp()
    const sticky = container.querySelectorAll('[class*="sticky"][class*="top-0"]')
    expect(sticky).toHaveLength(1)
    expect(sticky[0].tagName).toBe('NAV')
  })

  it('does not clip the page to the application measure', () => {
    const { container } = renderApp()
    // `max-w-5xl` is the app <main>'s box. A "full-bleed" hero rendered inside it was 1024px wide.
    expect(container.querySelectorAll('[class*="max-w-5xl"]')).toHaveLength(0)
  })

  it('keeps the anchors the tutorial capture navigates to', () => {
    const { container } = renderApp()
    expect(container.querySelector('#features')).not.toBeNull()
    // `#pricing` never existed, so TUTORIAL_FLOWS['getting-started'] was already failing at step 3.
    expect(container.querySelector('#pricing')).not.toBeNull()
  })

  it('has exactly one h1', () => {
    renderApp()
    expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1)
  })

  it('renders every section of the new information architecture', () => {
    const { container } = renderApp()
    const sections = [...container.querySelectorAll('[data-section]')].map((el) =>
      el.getAttribute('data-section'),
    )
    for (const name of [
      'hero',
      'proof',
      'problem',
      'how_it_works',
      'features',
      'safety',
      'comparison',
      'pricing',
      'final_cta',
    ]) {
      expect(sections, `${name} section is missing`).toContain(name)
    }
    // The FAQ is the existing server-driven section (issue #506), kept rather than replaced.
    expect(container.querySelector('#faq')).not.toBeNull()
  })

  it('offers a skip link ahead of the nav', () => {
    renderApp()
    const skip = screen.getByRole('link', { name: 'Skip to main content' })
    expect(skip.getAttribute('href')).toBe('#main')
  })

  it('links to both legal pages', () => {
    renderApp()
    expect(screen.getByRole('link', { name: 'Privacy Policy' }).getAttribute('href')).toBe(
      '/privacy-policy',
    )
    expect(screen.getByRole('link', { name: 'Terms and Conditions' }).getAttribute('href')).toBe(
      '/terms-and-conditions',
    )
  })
})

describe('honesty (issue #1300)', () => {
  it('publishes none of the fabricated proof or the features that do not exist', () => {
    const { container } = renderApp()
    const text = container.textContent ?? ''
    for (const claim of [
      '2,500+',
      '50K+',
      '85%',
      'Trusted by Professionals Worldwide',
      'Join thousands',
      '© 2024',
      'Custom AI training',
      'White-label',
      'API access',
      'Multi-team',
    ]) {
      expect(text.includes(claim), `the page still says "${claim}"`).toBe(false)
    }
  })

  it('states the limit of what automation can promise', () => {
    renderApp()
    expect(screen.getByText(/no tool can guarantee a LinkedIn account/i)).toBeTruthy()
  })

  // Issue #2366, free-trial scope: posts publish only after approval, engagement is suggestions the
  // user sends, and sign-in is LinkedIn OAuth. Scoped to the sections that copy change owns.
  // Pricing is deliberately absent: its capability list is shared with the account page and is
  // a separate change.
  const SCOPED_SECTIONS = [
    'hero',
    'proof',
    'problem',
    'how_it_works',
    'features',
    'safety',
    'comparison',
    'final_cta',
  ]

  function scopedText(container: HTMLElement): string {
    const parts = SCOPED_SECTIONS.map((name) => {
      const el = container.querySelector(`[data-section="${name}"]`)
      expect(el, `${name} section is missing`).not.toBeNull()
      return el?.textContent ?? ''
    })
    parts.push(screen.getByRole('contentinfo').textContent ?? '')
    // The FAQ renders its seeded fallback here (the mocked API returns no entries), answers
    // included — a collapsed answer is `hidden`, not absent, so it still counts as page text.
    const faq = container.querySelector('#faq')
    expect(faq, 'faq section is missing').not.toBeNull()
    parts.push(faq?.textContent ?? '')
    return parts.join('\n')
  }

  it('never claims LEM engages, or uses your session, on your behalf', () => {
    const { container } = renderApp()
    const text = scopedText(container)
    for (const claim of [
      /comments, replies and\s+follows up for you/i,
      /comments and replies for you/i,
      /your own (logged-in )?session/i,
      /running without you/i,
      /background job/i,
      /Nothing publishes until/i,
      /DMs land as drafts/i,
      /approval-gated/i,
      /engages for you/i,
      /engagement automation/i,
      /(connect|asks for) your session/i,
      /session cookies are stored/i,
      /run the automation/i,
    ]) {
      expect(claim.test(text), `the page still matches ${claim}`).toBe(false)
    }
  })

  it('states the approval, suggestion and sign-in posture in the hero', () => {
    const { container } = renderApp()
    const hero = container.querySelector('[data-section="hero"]')?.textContent ?? ''
    expect(hero).toMatch(/only after you approve it/i)
    expect(hero).toMatch(/which you review and send yourself/i)
    expect(hero).toMatch(/never asks for your LinkedIn password/i)
  })

  it('states the same posture in the safety section', () => {
    const { container } = renderApp()
    const safety = container.querySelector('[data-section="safety"]')?.textContent ?? ''
    expect(safety).toMatch(/never asks for your LinkedIn password or a session cookie/i)
    expect(safety).toMatch(/LEM does not send them for you/i)
    expect(safety).toMatch(/Only after you approve it is it published/i)
  })

  it('carries the trademark and non-affiliation notice', () => {
    const { container } = renderApp()
    expect(container.textContent).toContain('not affiliated with, endorsed by, or sponsored by')
  })
})

describe('accessibility (issue #1300)', () => {
  it('never skips a heading level', () => {
    const { container } = renderApp()
    const levels = [...container.querySelectorAll('h1,h2,h3,h4,h5,h6')].map((el) =>
      Number(el.tagName.slice(1)),
    )
    expect(levels[0]).toBe(1)
    for (let i = 1; i < levels.length; i += 1) {
      expect(levels[i] - levels[i - 1], `jump at heading ${i}: ${levels.join(',')}`).toBeLessThanOrEqual(1)
    }
  })

  it('gives every call to action a distinguishable accessible name', () => {
    renderApp()
    const names = screen
      .getAllByRole('button')
      .map((button) => button.getAttribute('aria-label') ?? button.textContent ?? '')
      .filter(Boolean)
    expect(names.length).toBeGreaterThan(5)
    expect(new Set(names).size, `duplicate control names: ${names.join(' | ')}`).toBe(names.length)
  })

  it('never leaves a ✓ or ✗ carrying meaning by shape alone', () => {
    const { container } = renderApp()
    const marks = container.querySelectorAll('svg[aria-hidden="true"] + .sr-only, .sr-only')
    const texts = [...marks].map((el) => el.textContent)
    expect(texts).toContain('included')
    // Every included/not-included icon is drawn by IncludedMark, which always emits the word.
    const included = texts.filter((t) => t === 'included' || t === 'not included').length
    expect(included).toBeGreaterThan(10)
  })

  it('labels the comparison table and wraps it in a keyboard-reachable scroll region', () => {
    renderApp()
    const region = screen.getByRole('region', {
      name: /How LEM compares with doing it by hand/i,
    })
    expect(region.getAttribute('tabindex')).toBe('0')
    expect(within(region).getByRole('table')).toBeTruthy()
  })

  it('swaps the comparison table for one block per capability below sm', () => {
    const { container } = renderApp()
    const region = screen.getByRole('region', { name: /How LEM compares with doing it by hand/i })
    expect(region.className).toContain('hidden')
    expect(region.className).toContain('sm:block')
    const stacked = container.querySelector('[data-section="comparison"] ul.sm\\:hidden') as HTMLElement
    expect(stacked).not.toBeNull()
    const rows = within(region).getAllByRole('row').length - 1
    expect(stacked.querySelectorAll(':scope > li')).toHaveLength(rows)
    expect(stacked.querySelectorAll('dt')).toHaveLength(rows * 3)
  })
})

// A phone browser lays a document out at its min-content width whenever that is wider than the
// screen, then zooms the whole thing down to fit — so on a 375px iPhone the front page rendered at
// ~610px with the hero, the copy and the buttons all running off the right edge (issue #1556).
// These assert the three structures that made the page ask for that room. Widths themselves are
// not assertable here: jsdom has no layout engine, so what is guarded is the markup that decides
// them.
describe('mobile viewport (issue #1556)', () => {
  it('hides the nav trial CTA below `sm` on the wrapper, where hiding actually works', () => {
    const { container } = renderApp()
    const cta = container.querySelector('nav [data-cta="nav_trial"]') as HTMLElement
    expect(cta).not.toBeNull()
    // The button's own base classes set `inline-flex`, and Tailwind emits that after `hidden`, so
    // a `hidden` on the button itself never applied.
    expect(cta.className).not.toContain('hidden')
    expect(cta.parentElement?.className).toContain('hidden')
    expect(cta.parentElement?.className).toContain('sm:block')
  })

  it('lets the hero columns shrink to the screen', () => {
    const { container } = renderApp()
    const grid = container.querySelector('[data-section="hero"] div[class*="grid"]') as HTMLElement
    expect(grid).not.toBeNull()
    for (const column of [...grid.children] as HTMLElement[]) {
      expect(column.className, `hero column "${column.className}" pins the page open`).toContain(
        'min-w-0',
      )
    }
  })

  it('lets every feature beat column shrink to the screen', () => {
    const { container } = renderApp()
    const beats = container.querySelectorAll('[data-testid^="feature-beat-"]')
    expect(beats.length).toBeGreaterThan(0)
    for (const beat of [...beats]) {
      for (const column of [...beat.children] as HTMLElement[]) {
        expect(column.className, `beat column "${column.className}" pins the page open`).toContain(
          'min-w-0',
        )
      }
    }
  })
})

describe('landing analytics (issue #1300)', () => {
  it('reports which section a CTA was clicked in', () => {
    renderApp()
    act(() => {
      screen.getByRole('button', { name: 'Start free trial from the hero' }).click()
    })
    expect(captured).toContainEqual({
      event: 'landing_cta_clicked',
      properties: { cta: 'hero_trial', section: 'hero' },
    })
    expect(auth.openLoginModal).toHaveBeenCalled()
  })

  it('reports which plan was chosen, and carries the intent into signup', () => {
    renderApp()
    act(() => {
      screen.getByRole('button', { name: 'Choose Professional and start the free trial' }).click()
    })
    expect(captured).toContainEqual({
      event: 'landing_plan_selected',
      properties: { tier: 'professional', price: '$79' },
    })
    expect(window.sessionStorage.getItem('lem:plan-intent')).toBe('professional')
  })

  it('names every pricing button with its visible text (WCAG 2.5.3)', () => {
    const { container } = renderApp()
    const buttons = [...container.querySelectorAll('[data-section="pricing"] button')]
    // One button per plan the pricing section shows (issue #2370 pricing test: trial + recommended).
    expect(buttons.length).toBe(SHOWN_PLANS.length)
    for (const button of buttons) {
      const visible = (button.textContent ?? '').trim()
      expect(button.getAttribute('aria-label') ?? '', visible).toContain(visible)
    }
  })

  it('reports the sections a visitor actually reached', () => {
    useImmediateObserver()
    renderApp()
    const sections = captured
      .filter((entry) => entry.event === 'landing_section_viewed')
      .map((entry) => entry.properties?.section)
    expect(sections).toContain('hero')
    expect(sections).toContain('pricing')
    expect(sections).toContain('safety')
  })
})

describe('the session round-trip (issue #1300)', () => {
  it('renders neither the marketing page nor the app while the session is resolving', () => {
    auth.isLoading = true
    const { container } = renderApp()
    // The hard-refresh flash guard: `user` is still null here, so without this a signed-in user
    // would see the whole marketing page and then have it swapped out from under them.
    expect(screen.queryByRole('heading', { level: 1 })).toBeNull()
    expect(screen.queryByTestId('dashboard')).toBeNull()
    expect(container.querySelector('[aria-busy="true"]')).not.toBeNull()
  })

  it('gives a signed-in visitor the application, not the marketing page', async () => {
    auth.user = { userId: 1, email: 'owner@example.com' }
    renderApp()
    expect(await screen.findByTestId('dashboard')).toBeTruthy()
    expect(screen.queryByRole('heading', { level: 1, name: /Your LinkedIn content/i })).toBeNull()
  })
})

// The legal pages ride inside the marketing chrome for a logged-out visitor (issue #1300 §1), so
// everything the front page's structure asserts has to hold there too — and the nav's in-page
// anchors have to still lead somewhere from a path that has no sections.
describe('the marketing chrome on the legal pages (issue #1300)', () => {
  it('keeps one nav, one main and one footer on /privacy-policy', () => {
    window.history.pushState({}, '', '/privacy-policy')
    const { container } = renderApp()
    expect(screen.getByRole('heading', { level: 1, name: 'Privacy Policy' })).toBeTruthy()
    expect(container.querySelectorAll('nav')).toHaveLength(1)
    expect(container.querySelectorAll('main')).toHaveLength(1)
    expect(container.querySelectorAll('footer')).toHaveLength(1)
    expect(container.querySelectorAll('[class*="sticky"][class*="top-0"]')).toHaveLength(1)
  })

  it('keeps one nav, one main and one footer on /terms-and-conditions', () => {
    window.history.pushState({}, '', '/terms-and-conditions')
    const { container } = renderApp()
    expect(screen.getByRole('heading', { level: 1, name: 'Terms and Conditions' })).toBeTruthy()
    expect(container.querySelectorAll('nav')).toHaveLength(1)
    expect(container.querySelectorAll('main')).toHaveLength(1)
    expect(container.querySelectorAll('footer')).toHaveLength(1)
  })

  it('points the in-page anchors at the front page rather than the current path', () => {
    window.history.pushState({}, '', '/privacy-policy')
    renderApp()
    // A bare href="#features" here resolves to /privacy-policy#features — a dead control in both
    // the nav and the footer, on both legal pages.
    for (const link of screen.getAllByRole('link', { name: 'Features' })) {
      expect(link.getAttribute('href')).toBe('/#features')
    }
  })

  it('routes back to the front page and scrolls to the section that was asked for', async () => {
    const scrollIntoView = vi.fn()
    Element.prototype.scrollIntoView = scrollIntoView
    window.history.pushState({}, '', '/privacy-policy')
    renderApp()

    act(() => {
      screen.getAllByRole('link', { name: 'Pricing' })[0].click()
    })

    expect(window.location.pathname).toBe('/')
    expect(screen.getByRole('heading', { level: 1, name: /Your LinkedIn content/i })).toBeTruthy()
    await waitFor(() => expect(scrollIntoView).toHaveBeenCalled())
  })

  it('leaves the anchors as plain hashes on the front page, where the browser handles them', () => {
    renderApp()
    for (const link of screen.getAllByRole('link', { name: 'Features' })) {
      expect(link.getAttribute('href')).toBe('#features')
    }
  })
})
