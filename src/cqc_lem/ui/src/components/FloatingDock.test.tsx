import { describe, expect, it, vi, afterEach } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import FeedbackWidget from './FeedbackWidget'
import FloatingDock, { FLOATING_DOCK_ID } from './FloatingDock'
import {
  SettingsSaveProvider, SaveAllBar, useRegisterSaveSection,
} from '../pages/account/SettingsSaveContext'

vi.mock('../utils/analytics', () => ({
  capture: vi.fn(),
  ensureSessionRecorded: vi.fn(),
  replayEnabled: () => false,
  analyticsSessionId: () => undefined,
  EVENTS: { prefsSaved: 'prefs_saved', feedbackOpened: 'feedback_opened' },
}))
vi.mock('../contexts/AuthContext', () => ({
  useAuth: () => ({ user: { userId: 1 }, sessionToken: 't' }),
}))
vi.mock('../hooks/useAppInfo', () => ({ useAppInfo: () => ({ data: null }) }))
vi.mock('../api/client', () => ({ default: { post: vi.fn() } }))

function DirtySection() {
  useRegisterSaveSection('voice', 'Voice', true, () => Promise.resolve(true))
  return null
}

// Mirrors Layout: the settings page (with its Save All bar) renders first, the dock last.
function harness() {
  return render(
    <MemoryRouter>
      <SettingsSaveProvider>
        <DirtySection />
        <SaveAllBar />
      </SettingsSaveProvider>
      <FloatingDock>
        <FeedbackWidget />
      </FloatingDock>
    </MemoryRouter>
  )
}

afterEach(cleanup)

describe('FloatingDock (issue #596)', () => {
  it('stacks Save All above the feedback launcher instead of behind it', () => {
    const { container } = harness()
    const dock = container.querySelector(`#${FLOATING_DOCK_ID}`) as HTMLElement
    const saveAll = screen.getByRole('button', { name: /Save All/ })
    const feedback = screen.getByRole('button', { name: 'Feedback / Report a bug' })

    expect(dock).toBeTruthy()
    expect(dock.contains(saveAll)).toBe(true)
    expect(dock.contains(feedback)).toBe(true)
    // column-reverse: the FIRST child sits at the bottom, so the launcher must precede the
    // portaled Save All bar for the bar to land above it.
    expect(dock.className).toContain('flex-col-reverse')
    const order = [...dock.children]
    expect(order.findIndex((el) => el.contains(feedback)))
      .toBeLessThan(order.findIndex((el) => el.contains(saveAll)))
  })

  it('leaves the corner to the dock — neither control pins itself there any more', () => {
    const { container } = harness()
    const dock = container.querySelector(`#${FLOATING_DOCK_ID}`) as HTMLElement
    const pinned = [...container.querySelectorAll('.fixed.bottom-4.right-4')]
    expect(pinned).toEqual([dock])
  })

  it('keeps Save All reachable once the feedback panel is open', () => {
    const { container } = harness()
    fireEvent.click(screen.getByRole('button', { name: 'Feedback / Report a bug' }))
    const dock = container.querySelector(`#${FLOATING_DOCK_ID}`) as HTMLElement
    const panel = screen.getByRole('button', { name: 'Close feedback' }).parentElement as HTMLElement
    const saveAll = screen.getByRole('button', { name: /Save All/ })

    expect(dock.contains(saveAll)).toBe(true)
    // The open panel is a sibling in the stack, not an overlay drawn on top of the bar.
    expect(panel.contains(saveAll)).toBe(false)
    expect(panel.className).not.toContain('fixed')
  })
})
