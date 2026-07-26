import { describe, expect, it, afterEach } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { Advanced, ConflictNotice, Field } from './Field'
import { SETTING_BY_KEY } from './registry'
import type { Finding } from './conflicts'

afterEach(cleanup)

describe('Field', () => {
  it('always shows the label and what the setting does', () => {
    render(<Field settingKey="min_reactions"><input aria-label="min reactions" /></Field>)
    expect(screen.getByText(SETTING_BY_KEY.min_reactions.label)).toBeTruthy()
    expect(screen.getByText(SETTING_BY_KEY.min_reactions.what)).toBeTruthy()
  })

  it('keeps "why it matters" and "recommended" behind the ? so dense sections stay scannable', () => {
    render(<Field settingKey="min_reactions"><input aria-label="min reactions" /></Field>)
    expect(screen.queryByText(/Why it matters/)).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: /Why .* matters/ }))
    expect(screen.getByText(/Why it matters/)).toBeTruthy()
    expect(screen.getByText(/Recommended/)).toBeTruthy()
  })

  it('renders an unknown key as a plain passthrough rather than blowing up', () => {
    render(<Field settingKey="not_a_setting"><input aria-label="raw" /></Field>)
    expect(screen.getByLabelText('raw')).toBeTruthy()
  })
})

describe('ConflictNotice', () => {
  const finding: Finding = {
    id: 'C2', severity: 'warn', section: 'targeting', anchor: 'feed_fallback_when_empty',
    message: 'Nothing would be commented on.',
    fix: { label: 'Turn the fallback on', patch: { feed_fallback_when_empty: true } },
  }

  it('shows the message and its one-click fix', () => {
    render(<ConflictNotice finding={finding} />)
    expect(screen.getByTestId('conflict-C2').textContent).toContain('Nothing would be commented on.')
    expect(screen.getByRole('button', { name: 'Turn the fallback on' })).toBeTruthy()
  })

  it('omits the fix button when there is no obvious fix', () => {
    render(<ConflictNotice finding={{ ...finding, fix: undefined }} />)
    expect(screen.queryByRole('button')).toBeNull()
  })
})

describe('Advanced', () => {
  it('starts collapsed and opens on click', () => {
    render(<Advanced><p>hidden knob</p></Advanced>)
    expect(screen.queryByText('hidden knob')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: /Advanced/ }))
    expect(screen.getByText('hidden knob')).toBeTruthy()
  })
})
