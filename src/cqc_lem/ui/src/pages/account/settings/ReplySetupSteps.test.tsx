import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import type { EngPrefs, GmailForwardConfirmation } from '../types'
import ReplySetupSteps from './ReplySetupSteps'

const state: { eng: Partial<EngPrefs> | null } = { eng: null }

vi.mock('./EngagementPrefsContext', () => ({
  useEngagementPrefs: () => state,
}))

afterEach(cleanup)

function show(confirmation: GmailForwardConfirmation | null | undefined) {
  state.eng = {
    reply_inbound_address: 'reply+tok9@parse.example.com',
    gmail_forward_confirmation: confirmation,
  } as Partial<EngPrefs>
  render(<ReplySetupSteps />)
  return screen.getByTestId('forwarding-status')
}

describe('ReplySetupSteps forwarding status', () => {
  it('reads "not confirmed" only when setup has never started', () => {
    expect(show(null).textContent).toContain('not confirmed')
  })

  // Issue #813: our server-side click of Gmail's verify link often fails, so a user who added the
  // address and finished by hand sat on confirmed=false. "Not confirmed" read as "setup failed".
  it('reads as waiting once Gmail has asked us to verify the address', () => {
    const chip = show({ confirmed: false, code: '123456789', url_found: true })
    expect(chip.textContent).toContain('Waiting for your first forwarded email')
    expect(chip.textContent).not.toContain('not confirmed')
  })

  it('confirms and says why when a forwarded LinkedIn email arrived', () => {
    expect(show({ confirmed: true, source: 'forwarded_email' }).textContent).toContain('Forwarding confirmed')
    expect(screen.getByText(/reached your forwarding address/)).toBeTruthy()
  })

  it('confirms without the delivery note when the auto-click did it', () => {
    expect(show({ confirmed: true, source: 'auto_click' }).textContent).toContain('Forwarding confirmed')
    expect(screen.queryByText(/reached your forwarding address/)).toBeNull()
  })

  it('offers the manual code only while confirmation is still outstanding', () => {
    show({ confirmed: false, code: '123456789' })
    expect(screen.getByText('123456789')).toBeTruthy()
    cleanup()
    show({ confirmed: true, code: '123456789', source: 'auto_click' })
    expect(screen.queryByText('123456789')).toBeNull()
  })
})
