import { describe, expect, it, vi, afterEach } from 'vitest'
import type { ReactNode } from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import TimezoneCard from './TimezoneCard'

const get = vi.fn()
vi.mock('../../api/client', () => ({
  default: {
    get: (...args: unknown[]) => get(...args),
    put: vi.fn(() => Promise.resolve({ data: {} })),
  },
}))
vi.mock('../../contexts/useAuth', () => ({ useAuth: () => ({ sessionToken: 'tok' }) }))

const serve = (timezone: string) =>
  get.mockImplementation(() => Promise.resolve({ data: { detail: { timezone } } }))

function harness(ui: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return { client, ...render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>) }
}

const select = () => screen.getByRole('combobox') as HTMLSelectElement

afterEach(() => { cleanup(); get.mockReset() })

describe('TimezoneCard', () => {
  it('shows the timezone the account is actually set to', async () => {
    serve('Europe/Berlin')
    harness(<TimezoneCard />)
    await waitFor(() => expect(select().value).toBe('Europe/Berlin'))
  })

  it('keeps the choice being made when the timezone is refetched underneath it', async () => {
    serve('Europe/Berlin')
    const { client } = harness(<TimezoneCard />)
    await waitFor(() => expect(select().value).toBe('Europe/Berlin'))

    fireEvent.change(select(), { target: { value: 'Asia/Tokyo' } })
    await client.invalidateQueries({ queryKey: ['user-timezone'] })

    // The refetch answers with the stored zone again. Reverting the user's unsaved pick here is
    // how someone ends up saving a zone they did not choose.
    await waitFor(() => expect(select().value).toBe('Asia/Tokyo'))
  })
})
