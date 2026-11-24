import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import ContentSection from './ContentSection'

vi.mock('@tanstack/react-query', () => ({ useQuery: () => ({ data: undefined }) }))
vi.mock('../../../api/client', () => ({ default: { get: vi.fn(), put: vi.fn() } }))
vi.mock('../../../contexts/useAuth', () => ({ useAuth: () => ({ sessionToken: 'tok' }) }))
vi.mock('./BrandKitCard', () => ({ default: () => <div /> }))
vi.mock('./userPrefsCtx', () => ({ useUserPrefs: () => ({ prefs: {}, setPrefs: vi.fn() }) }))

const state = vi.hoisted(() => ({ eng: {} as Record<string, unknown>, setEng: vi.fn() }))
vi.mock('./engagementPrefsCtx', () => ({
  useEngagementPrefs: () => ({ eng: state.eng, setEng: state.setEng }),
}))

afterEach(() => { cleanup(); state.setEng.mockClear() })

// The toggle sits behind the section's collapsed Advanced disclosure.
function renderOpen() {
  render(<ContentSection />)
  fireEvent.click(screen.getByRole('button', { name: /Advanced/ }))
}

function digestToggle() {
  return within(screen.getByTestId('field-review_digest_enabled')).getByRole('switch')
}

describe('session windows (#2767)', () => {
  const defaults = [{ start: '08:00', end: '08:45' }, { start: '12:30', end: '13:15' }]

  it('shows the defaults when nothing is stored', () => {
    state.eng = { session_windows: null, session_window_defaults: defaults }
    renderOpen()
    expect(screen.getByText(/Defaults: 08:00-08:45 and 12:30-13:15/)).toBeTruthy()
  })

  it('writes a window once both of its times are set', () => {
    state.eng = { session_windows: null, session_window_defaults: defaults }
    renderOpen()
    fireEvent.change(screen.getByLabelText('Window 1 start'), { target: { value: '09:00' } })
    expect(state.setEng).toHaveBeenLastCalledWith({ session_windows: [] })
    state.eng = { session_windows: [{ start: '09:00', end: '' }], session_window_defaults: defaults }
    cleanup()
    renderOpen()
    fireEvent.change(screen.getByLabelText('Window 1 end'), { target: { value: '10:00' } })
    expect(state.setEng).toHaveBeenLastCalledWith({ session_windows: [{ start: '09:00', end: '10:00' }] })
  })

  it('writes the timezone override, blank meaning the account zone', () => {
    state.eng = { session_timezone: 'Europe/London', session_window_defaults: defaults }
    renderOpen()
    fireEvent.change(screen.getByLabelText('Session timezone'), { target: { value: '' } })
    expect(state.setEng).toHaveBeenCalledWith({ session_timezone: '' })
  })
})

describe('review digest opt-out (#2669)', () => {
  it('reads as on when the pref was never saved', () => {
    state.eng = {}
    renderOpen()
    expect(digestToggle().getAttribute('aria-checked')).toBe('true')
  })

  it('turning it off writes review_digest_enabled=false', () => {
    state.eng = { review_digest_enabled: true }
    renderOpen()
    fireEvent.click(digestToggle())
    expect(state.setEng).toHaveBeenCalledWith({ review_digest_enabled: false })
  })

  it('turning it back on writes true', () => {
    state.eng = { review_digest_enabled: false }
    renderOpen()
    expect(digestToggle().getAttribute('aria-checked')).toBe('false')
    fireEvent.click(digestToggle())
    expect(state.setEng).toHaveBeenCalledWith({ review_digest_enabled: true })
  })
})
