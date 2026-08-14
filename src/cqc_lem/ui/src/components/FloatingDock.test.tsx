import { describe, expect, it, vi, afterEach } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import FeedbackWidget from './FeedbackWidget'
import FloatingDock, { FLOATING_DOCK_ID } from './FloatingDock'
import { SettingsSaveProvider, SaveAllBar } from '../pages/account/SettingsSaveContext'
import { useRegisterSaveSection } from '../pages/account/settingsSave'

vi.mock('../utils/analytics', () => ({
  capture: vi.fn(),
  ensureSessionRecorded: vi.fn(),
  replayEnabled: () => false,
  analyticsSessionId: () => undefined,
  EVENTS: { prefsSaved: 'prefs_saved', feedbackOpened: 'feedback_opened' },
}))
vi.mock('../contexts/useAuth', () => ({
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

// The dock id marks the portal SLOT; the visual stack is its parent.
function slotAndStack(container: HTMLElement) {
  const slot = container.querySelector(`#${FLOATING_DOCK_ID}`) as HTMLElement
  return { slot, stack: slot.parentElement as HTMLElement }
}

// Normal flex column: earlier in the DOM renders HIGHER on screen.
function isAbove(stack: HTMLElement, upper: HTMLElement, lower: HTMLElement) {
  const order = [...stack.children]
  return order.findIndex((el) => el.contains(upper)) < order.findIndex((el) => el.contains(lower))
}

describe('FloatingDock (issue #596)', () => {
  it('stacks Save All above the feedback launcher instead of behind it', () => {
    const { container } = harness()
    const { slot, stack } = slotAndStack(container)
    const saveAll = screen.getByRole('button', { name: /Save All/ })
    const feedback = screen.getByRole('button', { name: 'Feedback / Report a bug' })

    expect(slot).toBeTruthy()
    expect(slot.contains(saveAll)).toBe(true)
    expect(stack.contains(feedback)).toBe(true)
    expect(isAbove(stack, saveAll, feedback)).toBe(true)
  })

  // React re-appends a host node it re-renders to the END of its parent, so a portaled bar sharing
  // one container with the launcher swapped places with it on the first open/close. The slot keeps
  // the order structural — assert it survives a full toggle, not just the initial mount.
  it('keeps the stacking order after the feedback panel opens and closes', () => {
    const { container } = harness()
    const { stack } = slotAndStack(container)
    const saveAll = screen.getByRole('button', { name: /Save All/ })

    fireEvent.click(screen.getByRole('button', { name: 'Feedback / Report a bug' }))
    const panel = screen.getByRole('button', { name: 'Close feedback' })
      .parentElement as HTMLElement
    expect(isAbove(stack, saveAll, panel)).toBe(true)

    fireEvent.click(screen.getByRole('button', { name: 'Close feedback' }))
    const launcher = screen.getByRole('button', { name: 'Feedback / Report a bug' })
    expect(isAbove(stack, saveAll, launcher)).toBe(true)
  })

  it('leaves the corner to the dock — neither control pins itself there any more', () => {
    const { container } = harness()
    const { stack } = slotAndStack(container)
    const pinned = [...container.querySelectorAll('.fixed.bottom-4.right-4')]
    expect(pinned).toEqual([stack])
  })

  // The dock is pinned to the BOTTOM, so an open panel grows upwards — off the top of a landscape
  // phone, taking the "Send feedback" button with it. A width clamp does not cover that; the panel
  // needs the same height cap the modals wear (issue #894).
  it('caps the open feedback panel to the viewport and scrolls its own content', () => {
    render(
      <MemoryRouter>
        <FloatingDock><FeedbackWidget /></FloatingDock>
      </MemoryRouter>
    )
    fireEvent.click(screen.getByRole('button', { name: 'Feedback / Report a bug' }))
    const panel = screen.getByRole('button', { name: 'Close feedback' }).parentElement as HTMLElement
    expect(panel.contains(screen.getByRole('button', { name: 'Send feedback' }))).toBe(true)
    expect(panel.className).toContain('max-h-viewport')
    expect(panel.className).toContain('overflow-y-auto')
  })

  it('keeps Save All reachable once the feedback panel is open', () => {
    const { container } = harness()
    fireEvent.click(screen.getByRole('button', { name: 'Feedback / Report a bug' }))
    const { slot } = slotAndStack(container)
    const panel = screen.getByRole('button', { name: 'Close feedback' }).parentElement as HTMLElement
    const saveAll = screen.getByRole('button', { name: /Save All/ })

    expect(slot.contains(saveAll)).toBe(true)
    // The open panel is a sibling in the stack, not an overlay drawn on top of the bar.
    expect(panel.contains(saveAll)).toBe(false)
    expect(panel.className).not.toContain('fixed')
  })
})
