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

function digestToggle() {
  return within(screen.getByTestId('field-review_digest_enabled')).getByRole('switch')
}

describe('review digest opt-out (#2669)', () => {
  it('reads as on when the pref was never saved', () => {
    state.eng = {}
    render(<ContentSection />)
    expect(digestToggle().getAttribute('aria-checked')).toBe('true')
  })

  it('turning it off writes review_digest_enabled=false', () => {
    state.eng = { review_digest_enabled: true }
    render(<ContentSection />)
    fireEvent.click(digestToggle())
    expect(state.setEng).toHaveBeenCalledWith({ review_digest_enabled: false })
  })

  it('turning it back on writes true', () => {
    state.eng = { review_digest_enabled: false }
    render(<ContentSection />)
    expect(digestToggle().getAttribute('aria-checked')).toBe('false')
    fireEvent.click(digestToggle())
    expect(state.setEng).toHaveBeenCalledWith({ review_digest_enabled: true })
  })
})
