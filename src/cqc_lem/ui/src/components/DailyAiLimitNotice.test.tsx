import { describe, expect, it } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import DailyAiLimitNotice from './DailyAiLimitNotice'
import { announceDailyAiLimit, DAILY_AI_LIMIT_FALLBACK } from '../utils/dailyAiLimit'

describe('DailyAiLimitNotice', () => {
  it('renders nothing until the limit is announced', () => {
    render(<DailyAiLimitNotice />)
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it("shows the server's wording, and can be dismissed", () => {
    render(<DailyAiLimitNotice />)
    act(() => announceDailyAiLimit('Daily AI limit reached for your free trial. AI generation resumes at 00:00 UTC.'))
    expect(screen.getByRole('alert').textContent).toContain('resumes at 00:00 UTC')
    fireEvent.click(screen.getByText('Dismiss'))
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('falls back to its own wording when the response carried none', () => {
    render(<DailyAiLimitNotice />)
    act(() => announceDailyAiLimit(undefined))
    expect(screen.getByRole('alert').textContent).toContain(DAILY_AI_LIMIT_FALLBACK)
  })
})
