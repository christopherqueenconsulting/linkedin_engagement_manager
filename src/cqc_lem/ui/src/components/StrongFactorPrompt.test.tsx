import { describe, expect, it, vi, afterEach, beforeEach } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import StrongFactorPrompt from './StrongFactorPrompt'

const auth = {
  strongFactorPrompt: true,
  strongFactorDeadline: '2099-01-01T00:00:00Z',
  enrollmentRequired: false,
}
vi.mock('../contexts/AuthContext', () => ({ useAuth: () => auth }))

beforeEach(() => {
  localStorage.clear()
  auth.strongFactorPrompt = true
  auth.strongFactorDeadline = '2099-01-01T00:00:00Z'
  auth.enrollmentRequired = false
})
afterEach(cleanup)

function harness() {
  return render(
    <MemoryRouter>
      <StrongFactorPrompt />
    </MemoryRouter>,
  )
}

// Mandatory enrolment is a date, and a date nobody was warned about is a support ticket
// (issue #905, design §7 Stage 2).
describe('StrongFactorPrompt', () => {
  it('warns as soon as a deadline is scheduled, and names it', () => {
    harness()
    const prompt = screen.getByTestId('strong-factor-prompt')
    expect(prompt.textContent).toContain('Add two-factor sign-in')
    expect(prompt.textContent).toContain(new Date(auth.strongFactorDeadline!).toLocaleDateString())
  })

  it('stays hidden when the server is not asking for a factor', () => {
    auth.strongFactorPrompt = false
    harness()
    expect(screen.queryByTestId('strong-factor-prompt')).toBeNull()
  })

  it('stands down once the session is actually held — the gate says it better', () => {
    auth.enrollmentRequired = true
    harness()
    expect(screen.queryByTestId('strong-factor-prompt')).toBeNull()
  })

  it('is dismissible, and stays dismissed on the next render', () => {
    const { unmount } = harness()
    fireEvent.click(screen.getByRole('button', { name: 'Not now' }))
    expect(screen.queryByTestId('strong-factor-prompt')).toBeNull()
    unmount()
    harness()
    expect(screen.queryByTestId('strong-factor-prompt')).toBeNull()
  })

  it('a dismissal does not swallow a deadline the operator MOVED', () => {
    // Bringing the date forward is the dangerous direction: a "Not now" from weeks ago must not be
    // what stops someone hearing that the cutover is now sooner than they were told.
    harness()
    fireEvent.click(screen.getByRole('button', { name: 'Not now' }))
    cleanup()
    auth.strongFactorDeadline = '2098-06-01T00:00:00Z'
    harness()
    expect(screen.getByTestId('strong-factor-prompt').textContent).toContain(
      new Date(auth.strongFactorDeadline!).toLocaleDateString(),
    )
  })

  it('a dismissal does not swallow the deadline actually arriving', () => {
    // "From <date>" and "from your next sign-in" are different notices, and the second one is the
    // urgent one. Dismissing the first must not be what hides it.
    harness()
    fireEvent.click(screen.getByRole('button', { name: 'Not now' }))
    cleanup()
    auth.strongFactorDeadline = '2020-01-01T00:00:00Z'
    harness()
    expect(screen.getByTestId('strong-factor-prompt').textContent).toContain('From your next sign-in')
  })

  it('a user who ENROLS never sees it again, dismissal or not', () => {
    // The server stops sending strong_factor_prompt the moment a factor exists, which is why the
    // dismissal is only ever browser state — it can hide the nudge, never the deadline.
    auth.strongFactorPrompt = false
    localStorage.clear()
    harness()
    expect(screen.queryByTestId('strong-factor-prompt')).toBeNull()
  })

  it('changes its wording once the deadline has already passed', () => {
    auth.strongFactorDeadline = '2020-01-01T00:00:00Z'
    harness()
    expect(screen.getByTestId('strong-factor-prompt').textContent).toContain(
      'From your next sign-in',
    )
  })

  it('links to the page that makes it go away', () => {
    harness()
    expect(screen.getByRole('link', { name: 'Set it up' }).getAttribute('href')).toBe('/account')
  })
})
