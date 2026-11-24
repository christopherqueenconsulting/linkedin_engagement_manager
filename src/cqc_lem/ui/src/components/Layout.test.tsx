import { describe, expect, it, vi, afterEach } from 'vitest'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import type { ReactNode } from 'react'
import { MemoryRouter } from 'react-router-dom'
import Layout from './Layout'
import { OPEN_FEEDBACK_EVENT } from './FloatingDock'

const auth = {
  user: { userId: 1, email: 'a-fairly-long-address@example.com' },
  logout: vi.fn(),
  openLoginModal: vi.fn(),
  isAdmin: true,
}
vi.mock('../contexts/useAuth', () => ({ useAuth: () => auth }))
// The Agents entry is flag-gated (issue #2687). Off by default here so every geometry test below
// runs against the nav real users see today; the Agents block flips it.
const flags = { mcpServer: false }
vi.mock('../hooks/useFeatureFlags', () => ({
  FLAGS: { mcpServer: 'mcp-server-enabled' },
  useFeatureFlag: (key: string) => key === 'mcp-server-enabled' && flags.mcpServer,
}))
vi.mock('./AccountReadinessBanner', () => ({ default: () => null }))
vi.mock('./FeedbackWidget', () => ({ default: () => null }))
vi.mock('./FloatingDock', () => ({
  default: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  OPEN_FEEDBACK_EVENT: 'lem:open-feedback',
}))
vi.mock('./Footer', () => ({ default: () => null }))
vi.mock('./PostHogSurveyModal', () => ({ default: () => null }))
vi.mock('./ShippedNotice', () => ({ default: () => null }))
vi.mock('./SurveyModal', () => ({ default: () => null }))

afterEach(() => {
  cleanup()
  flags.mcpServer = false
})

function harness() {
  return render(
    <MemoryRouter>
      <Layout />
    </MemoryRouter>,
  )
}

// The nav is the one row every page carries, so it is also the one row that can push the whole
// site sideways on a phone: five links plus the account controls do not fit 375px (issue #894).
describe('Layout nav on a narrow screen (issue #894)', () => {
  it('lets the link row shrink and scroll instead of widening the page', () => {
    harness()
    const links = screen.getByTestId('nav-links')
    expect(links.className).toContain('overflow-x-auto')
    expect(links.className).toContain('min-w-0')
  })

  it('keeps the account controls out of the shrinking row so Log out stays reachable', () => {
    harness()
    const logout = screen.getByRole('button', { name: 'Log out' })
    const cluster = logout.parentElement as HTMLElement
    expect(cluster.className).toContain('shrink-0')
    expect(screen.getByTestId('nav-links').contains(logout)).toBe(false)
  })

  it('never breaks a nav label across lines — a wrapped label would blow out the 56px bar', () => {
    harness()
    const labels = ['Home', 'Account', 'Avatars', 'Content', 'Users', 'Feedback Triage']
    for (const label of labels) {
      expect(screen.getByRole('link', { name: label }).className).toContain('whitespace-nowrap')
    }
  })

  it('drops the signed-in email and the inline links below the md breakpoint', () => {
    harness()
    const email = screen.getByText(auth.user.email).parentElement as HTMLElement
    expect(email.className).toContain('hidden')
    expect(email.className).toContain('md:inline')
    expect(screen.getByTestId('nav-links').className).toContain('hidden md:flex')
  })
})

// Below md the wordmark leaves no room for the links, so they move behind one labelled button
// instead of scrolling out of sight (where a phone user could not tell they existed).
describe('Layout menu below md', () => {
  it('starts closed and names what it controls', () => {
    harness()
    const button = screen.getByRole('button', { name: 'Menu' })
    expect(button.getAttribute('aria-expanded')).toBe('false')
    expect(button.getAttribute('aria-controls')).toBe('app-menu')
    expect(button.className).toContain('md:hidden')
    expect(document.getElementById('app-menu')).toBeNull()
  })

  it('lists every destination, admin links included, plus Log out', () => {
    harness()
    fireEvent.click(screen.getByRole('button', { name: 'Menu' }))
    const menu = document.getElementById('app-menu') as HTMLElement
    for (const label of ['Home', 'Account', 'Avatars', 'Content', 'Users', 'Feedback Triage', 'Audit Log']) {
      within(menu).getByRole('link', { name: label })
    }
    within(menu).getByRole('button', { name: 'Log out' })
    expect(screen.getByRole('button', { name: 'Close menu' }).getAttribute('aria-expanded')).toBe('true')
  })

  it('carries the feedback launcher as a row that opens the panel and closes the menu', () => {
    harness()
    fireEvent.click(screen.getByRole('button', { name: 'Menu' }))
    const menu = document.getElementById('app-menu') as HTMLElement
    const opened = vi.fn()
    window.addEventListener(OPEN_FEEDBACK_EVENT, opened)
    fireEvent.click(within(menu).getByRole('button', { name: 'Feedback / Report a bug' }))
    window.removeEventListener(OPEN_FEEDBACK_EVENT, opened)
    expect(opened).toHaveBeenCalledTimes(1)
    expect(document.getElementById('app-menu')).toBeNull()
  })

  it('closes on Escape', () => {
    harness()
    fireEvent.click(screen.getByRole('button', { name: 'Menu' }))
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(document.getElementById('app-menu')).toBeNull()
  })
})

describe('Layout Agents entry (issue #2687)', () => {
  it('is absent while mcp-server-enabled is off, inline and in the menu', () => {
    harness()
    expect(screen.queryByRole('link', { name: 'Agents' })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Menu' }))
    const menu = document.getElementById('app-menu') as HTMLElement
    expect(within(menu).queryByRole('link', { name: 'Agents' })).toBeNull()
  })

  it('appears in both navs when the flag is on, with the same no-wrap geometry as its siblings', () => {
    flags.mcpServer = true
    harness()
    const inline = within(screen.getByTestId('nav-links')).getByRole('link', { name: 'Agents' })
    expect(inline.getAttribute('href')).toBe('/agents')
    expect(inline.className).toContain('whitespace-nowrap')
    fireEvent.click(screen.getByRole('button', { name: 'Menu' }))
    const menu = document.getElementById('app-menu') as HTMLElement
    expect(within(menu).getByRole('link', { name: 'Agents' }).getAttribute('href')).toBe('/agents')
  })
})

describe('Layout skip link', () => {
  it('is the first focusable element and targets the main region', () => {
    harness()
    const skip = screen.getByRole('link', { name: 'Skip to main content' })
    expect(skip.getAttribute('href')).toBe('#main')
    expect(document.querySelector('a, button')).toBe(skip)
    expect(screen.getByRole('main').getAttribute('id')).toBe('main')
  })
})
