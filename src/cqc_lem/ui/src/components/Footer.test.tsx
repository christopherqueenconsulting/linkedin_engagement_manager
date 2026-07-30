import { describe, expect, it, vi, afterEach } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import Footer from './Footer'

vi.mock('../hooks/useAppInfo', () => ({ useAppInfo: () => ({ data: null }) }))

function harness() {
  return render(
    <MemoryRouter>
      <Footer />
    </MemoryRouter>
  )
}

afterEach(cleanup)

describe('Footer (issue #772)', () => {
  it('renders the copyright notice', () => {
    harness()
    expect(screen.getByText(/Christopher Queen Consulting\. All rights reserved\./)).toBeTruthy()
  })

  it('links to the privacy policy', () => {
    harness()
    const link = screen.getByRole('link', { name: 'Privacy Policy' })
    expect(link.getAttribute('href')).toBe('/privacy-policy')
  })

  it('links to the terms and conditions', () => {
    harness()
    const link = screen.getByRole('link', { name: 'Terms and Conditions' })
    expect(link.getAttribute('href')).toBe('/terms-and-conditions')
  })
})
