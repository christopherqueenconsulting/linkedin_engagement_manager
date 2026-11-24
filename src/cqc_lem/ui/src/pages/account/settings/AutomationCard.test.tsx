import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import type { EngPrefs } from '../types'
import AutomationCard from './AutomationCard'
import { AUTOMATION_LANES } from './automationLanes'

const setEng = vi.fn()
const state: { eng: Partial<EngPrefs> | null; setEng: typeof setEng } = { eng: null, setEng }

vi.mock('./engagementPrefsCtx', () => ({ useEngagementPrefs: () => state }))

beforeEach(() => {
  setEng.mockClear()
  state.eng = {
    nurture_dm_mode: 'pre_review', artifact_dm_mode: 'pre_review', group_post_mode: 'auto_approve',
    followup_step_mode: 'pre_review', appreciation_dm_mode: 'pre_review',
  }
})
afterEach(cleanup)

describe('AutomationCard', () => {
  it('lists every lane with its saved mode', () => {
    render(<AutomationCard />)
    for (const lane of AUTOMATION_LANES) {
      const row = screen.getByTestId(`automation-${lane.key}`)
      const expected = lane.key === 'group_post_mode' ? 'auto_approve' : 'pre_review'
      expect((within(row).getByRole('combobox') as HTMLSelectElement).value).toBe(expected)
    }
  })

  it('renders nothing before preferences load', () => {
    state.eng = null
    const { container } = render(<AutomationCard />)
    expect(container.textContent).toBe('')
  })

  it.each(AUTOMATION_LANES)('round-trips $key through a named confirm step', (lane) => {
    state.eng = {
      nurture_dm_mode: 'pre_review', artifact_dm_mode: 'pre_review', group_post_mode: 'pre_review',
      followup_step_mode: 'pre_review', appreciation_dm_mode: 'pre_review',
    }
    render(<AutomationCard />)
    const row = screen.getByTestId(`automation-${lane.key}`)
    fireEvent.change(within(row).getByRole('combobox'), { target: { value: 'auto_approve' } })
    expect(setEng).not.toHaveBeenCalled()
    const dialog = screen.getByRole('alertdialog')
    expect(dialog.textContent).toContain(lane.label)
    fireEvent.click(within(dialog).getByText(`Yes, auto-approve ${lane.label}`))
    expect(setEng).toHaveBeenCalledWith({ [lane.key]: 'auto_approve' })
  })

  it('cancelling the confirm leaves the mode alone', () => {
    render(<AutomationCard />)
    const row = screen.getByTestId('automation-nurture_dm_mode')
    fireEvent.change(within(row).getByRole('combobox'), { target: { value: 'auto_approve' } })
    fireEvent.click(screen.getByText('Cancel'))
    expect(setEng).not.toHaveBeenCalled()
    expect(screen.queryByRole('alertdialog')).toBeNull()
  })

  it('going back to review needs no confirmation', () => {
    render(<AutomationCard />)
    const row = screen.getByTestId('automation-group_post_mode')
    fireEvent.change(within(row).getByRole('combobox'), { target: { value: 'pre_review' } })
    expect(setEng).toHaveBeenCalledWith({ group_post_mode: 'pre_review' })
  })
})
