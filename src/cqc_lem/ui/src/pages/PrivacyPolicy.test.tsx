import { describe, expect, it, vi, afterEach } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import PrivacyPolicy from './PrivacyPolicy'

vi.mock('../utils/analytics', () => ({ capturePageview: vi.fn() }))

function harness() {
  return render(
    <MemoryRouter>
      <PrivacyPolicy />
    </MemoryRouter>
  )
}

afterEach(cleanup)

describe('PrivacyPolicy (issue #772)', () => {
  it('renders the page title and current year', () => {
    harness()
    expect(screen.getByRole('heading', { name: 'Privacy Policy' })).toBeTruthy()
    expect(screen.getByText(new RegExp(`Last updated: ${new Date().getFullYear()}-07-30`))).toBeTruthy()
  })

  it('links back to the home page', () => {
    harness()
    expect(screen.getByRole('link', { name: 'Back to home' })).toHaveAttribute('href', '/')
  })

  it('includes the LinkedIn integration section', () => {
    harness()
    expect(screen.getByRole('heading', { name: '4. LinkedIn integration' })).toBeTruthy()
  })
})
