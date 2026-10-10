import { describe, expect, it, vi, afterEach } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import DemoBadge, { DEMO_BADGE_TEST_ID } from './DemoBadge'

const appInfo = vi.hoisted(() => ({ data: undefined as unknown }))
vi.mock('../hooks/useAppInfo', () => ({ useAppInfo: () => appInfo }))

afterEach(() => {
  cleanup()
  appInfo.data = undefined
})

describe('DemoBadge (issue #2372)', () => {
  it('renders "Demo data" when the server reports demo mode on', () => {
    appInfo.data = { version: '1.0.0', show_version: false, demo_mode: true }
    render(<DemoBadge />)
    const badge = screen.getByTestId(DEMO_BADGE_TEST_ID)
    expect(badge.textContent).toBe('Demo data')
    // A fixed corner the recording can crop, never in the way of a click.
    expect(badge.className).toContain('fixed')
    expect(badge.className).toContain('bottom-3')
    expect(badge.className).toContain('left-3')
    expect(badge.className).toContain('pointer-events-none')
  })

  it('renders nothing when demo mode is off', () => {
    appInfo.data = { version: '1.0.0', show_version: false, demo_mode: false }
    render(<DemoBadge />)
    expect(screen.queryByTestId(DEMO_BADGE_TEST_ID)).toBeNull()
  })

  it('renders nothing when the server does not report the field (off by default)', () => {
    appInfo.data = { version: '1.0.0', show_version: false }
    render(<DemoBadge />)
    expect(screen.queryByTestId(DEMO_BADGE_TEST_ID)).toBeNull()
  })

  it('renders nothing while app-info is loading or failed', () => {
    appInfo.data = undefined
    render(<DemoBadge />)
    expect(screen.queryByTestId(DEMO_BADGE_TEST_ID)).toBeNull()
  })
})
