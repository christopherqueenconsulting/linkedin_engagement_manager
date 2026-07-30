import { describe, expect, it, vi, afterEach } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import TermsAndConditions from './TermsAndConditions'

vi.mock('../utils/analytics', () => ({ capturePageview: vi.fn() }))

function harness() {
  return render(
    <MemoryRouter>
      <TermsAndConditions />
    </MemoryRouter>
  )
}

afterEach(cleanup)

describe('TermsAndConditions (issue #772)', () => {
  it('renders the page title and current year', () => {
    harness()
    expect(screen.getByRole('heading', { name: 'Terms and Conditions' })).toBeTruthy()
    expect(screen.getByText(new RegExp(`Last updated: ${new Date().getFullYear()}-07-30`))).toBeTruthy()
  })

  it('links back to the home page', () => {
    harness()
    expect(screen.getByRole('link', { name: 'Back to home' })).toHaveAttribute('href', '/')
  })

  it('includes the LinkedIn terms section', () => {
    harness()
    expect(screen.getByRole('heading', { name: '3. LinkedIn terms and your responsibilities' })).toBeTruthy()
  })
})
