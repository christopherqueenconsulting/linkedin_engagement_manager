import { describe, expect, it, vi, afterEach, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import VoiceExemplarsCard, { MAX_EXPORT_BYTES } from './VoiceExemplarsCard'
import { summarize } from './voiceExemplarsSummary'

const post = vi.fn()
vi.mock('../../api/client', () => ({ default: { post: (...args: unknown[]) => post(...args) } }))
vi.mock('../../contexts/useAuth', () => ({ useAuth: () => ({ sessionToken: 'tok' }) }))

function harness(ui: ReactNode) {
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>)
}

const csv = (name = 'Comments.csv', size?: number) => {
  const f = new File(['Date,Message\n'], name, { type: 'text/csv' })
  if (size !== undefined) Object.defineProperty(f, 'size', { value: size })
  return f
}
const choose = (f: File) =>
  fireEvent.change(screen.getByTestId('voice-exemplars-file'), { target: { files: [f] } })
const button = () => screen.getByTestId('voice-exemplars-upload') as HTMLButtonElement
const result = (over = {}) => ({
  data: {
    detail: { received: 10, stored: 8, duplicate: 1, too_short_or_long: 1, over_cap: 0, embed_failed: 0, ...over },
  },
})

beforeEach(() => post.mockReset())
afterEach(cleanup)

describe('VoiceExemplarsCard (issue #2627, Phase 1b)', () => {
  it('keeps Upload disabled until a file is chosen', () => {
    harness(<VoiceExemplarsCard />)
    expect(button().disabled).toBe(true)
  })

  it('posts the chosen kind and file as multipart and reports the counts', async () => {
    post.mockResolvedValue(result())
    harness(<VoiceExemplarsCard />)
    fireEvent.click(screen.getByLabelText(/Shares\.csv/))
    choose(csv('Shares.csv'))
    fireEvent.click(button())
    await waitFor(() => expect(post).toHaveBeenCalled())
    const [url, form] = post.mock.calls[0] as [string, FormData]
    expect(url).toBe('/user/voice-exemplars')
    expect(form.get('kind')).toBe('shares')
    expect(form.get('session_token')).toBe('tok')
    expect((form.get('file') as File).name).toBe('Shares.csv')
    await waitFor(() =>
      expect(screen.getByRole('status').textContent).toMatch(/Saved 8 of 10 rows.*1 already saved.*1 too short/))
  })

  it('refuses an oversize file without calling the server', () => {
    harness(<VoiceExemplarsCard />)
    choose(csv('Comments.csv', MAX_EXPORT_BYTES + 1))
    expect(screen.getByRole('status').textContent).toMatch(/larger than 5 MB/)
    expect(button().disabled).toBe(true)
    expect(post).not.toHaveBeenCalled()
  })

  it('surfaces the server’s row error on a 400', async () => {
    post.mockRejectedValueOnce(Object.assign(new Error('bad'), {
      isAxiosError: true, response: { status: 400, data: { detail: 'No text column found in that file' } },
    }))
    harness(<VoiceExemplarsCard />)
    choose(csv())
    fireEvent.click(button())
    await waitFor(() => expect(screen.getByRole('status').textContent).toMatch(/No text column found/))
  })

  it('reads a 404 as not enabled', async () => {
    post.mockRejectedValueOnce(Object.assign(new Error('nf'), {
      isAxiosError: true, response: { status: 404 },
    }))
    harness(<VoiceExemplarsCard />)
    choose(csv())
    fireEvent.click(button())
    await waitFor(() => expect(screen.getByRole('status').textContent).toMatch(/not enabled/))
  })

  it('says so when the saved examples could not be read', () => {
    expect(summarize({ received: 3, stored: 0, duplicate: 0, too_short_or_long: 0, over_cap: 0,
      embed_failed: 0, unavailable: true })).toMatch(/Could not read/)
  })
})
