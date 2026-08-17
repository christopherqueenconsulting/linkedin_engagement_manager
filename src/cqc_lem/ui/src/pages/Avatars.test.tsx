import { describe, expect, it, vi, afterEach, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import Avatars from './Avatars'

// Issue #1598: an avatar that declares no likeness attributes leaves `subject_clause()` empty, so
// the likeness probe reports every frame unchecked and #744's clause contributes nothing to the
// image prompt — a state #1430 measured 152 times in production and that the SPA never mentioned.

const get = vi.fn()
const post = vi.fn()
const put = vi.fn()

vi.mock('../api/client', () => ({
  default: {
    get: (...args: unknown[]) => get(...args),
    post: (...args: unknown[]) => post(...args),
    put: (...args: unknown[]) => put(...args),
  },
}))

vi.mock('../contexts/useAuth', () => ({
  useAuth: () => ({ user: { email: 'test@example.com', userId: 1 }, sessionToken: 'tok' }),
}))

function harness(ui: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>)
}

function training(overrides: Record<string, unknown> = {}) {
  return {
    id: 4,
    training_id: 'trn_1',
    model_ref: 'owner/lora:v1',
    trigger_word: 'LEMAVTR1',
    status: 'succeeded',
    is_active: true,
    gender_presentation: null,
    age_band: null,
    attributes_confirmed_at: null,
    approval_status: 'approved',
    approved_at: '2026-08-01T12:00:00Z',
    sample_paths: [{ label: 'headshot', path: '/a.png' }],
    samples_generated_at: '2026-08-01T12:00:00Z',
    sample_regen_count: 0,
    created_at: '2026-08-01T11:00:00Z',
    ...overrides,
  }
}

/** Route every page query by URL so one avatar row can be varied per test. */
function mockApi(row: Record<string, unknown>) {
  get.mockImplementation((url: string) => {
    if (url === '/avatar/credits') {
      return Promise.resolve({ data: { detail: { balance: 1, active_avatar: row } } })
    }
    if (url === '/avatar/trainings') {
      return Promise.resolve({ data: { detail: [row] } })
    }
    if (url === '/avatar/preferences') {
      return Promise.resolve({ data: { detail: {
        avatar_disabled: false, avatar_use_post_image: true, avatar_use_carousel: false,
        avatar_use_video: true, avatar_use_newsletter: false, avatar_caption_overlay: false,
      } } })
    }
    if (String(url).endsWith('/samples')) {
      return Promise.resolve({ data: { detail: {
        avatar_id: row.id, approval_status: 'approved',
        samples: [{ label: 'headshot', url: '/a.png' }],
        samples_generated_at: '2026-08-01T12:00:00Z', sample_regen_count: 0,
        sample_regen_remaining: 2,
        gender_presentation: row.gender_presentation, age_band: row.age_band,
      } } })
    }
    return Promise.resolve({ data: { detail: null } })
  })
}

const undeclared = () => screen.queryByTestId('avatar-attributes-undeclared-4')
const ageOnly = () => screen.queryByTestId('avatar-attributes-age-only-4')

beforeEach(() => {
  get.mockReset()
  post.mockReset()
  put.mockReset()
})
afterEach(cleanup)

describe('Avatars — undeclared likeness attributes (issue #1598)', () => {
  it('names both consequences when neither attribute is declared', async () => {
    mockApi(training())
    harness(<Avatars />)
    await waitFor(() => expect(undeclared()).not.toBeNull())
    const text = undeclared()?.textContent ?? ''
    expect(text).toMatch(/likeness check/i)
    expect(text).toMatch(/unchecked/i)
    expect(text).toMatch(/image prompts/i)
    expect(ageOnly()).toBeNull()
  })

  it('says nothing once a value is declared', async () => {
    mockApi(training({ gender_presentation: 'woman', age_band: null }))
    harness(<Avatars />)
    await waitFor(() => expect(screen.getByTestId('avatar-card-4')).toBeTruthy())
    await waitFor(() => expect(screen.getByTestId('avatar-gender-4')).toBeTruthy())
    expect(undeclared()).toBeNull()
    expect(ageOnly()).toBeNull()
  })

  it('says nothing when only the age band is declared', async () => {
    mockApi(training({ gender_presentation: null, age_band: '40s' }))
    harness(<Avatars />)
    await waitFor(() => expect(screen.getByTestId('avatar-age-4')).toBeTruthy())
    expect(undeclared()).toBeNull()
    expect(ageOnly()).toBeNull()
  })

  it('never re-asks a declination — "prefer not to say" gets the age-band prompt, not the undeclared one', async () => {
    // `prefer-not-to-say` maps to ("", "") in GENDER_PRESENTATIONS, so on its own it still yields
    // an empty subject clause. Silent here would leave that account permanently inert.
    mockApi(training({ gender_presentation: 'prefer-not-to-say', age_band: null }))
    harness(<Avatars />)
    await waitFor(() => expect(ageOnly()).not.toBeNull())
    expect(undeclared()).toBeNull()
    const text = ageOnly()?.textContent ?? ''
    expect(text).toMatch(/age band/i)
    expect(text).toMatch(/likeness check/i)
  })

  it('says nothing when a declination is paired with an age band', async () => {
    mockApi(training({ gender_presentation: 'prefer-not-to-say', age_band: '50s' }))
    harness(<Avatars />)
    await waitFor(() => expect(screen.getByTestId('avatar-age-4')).toBeTruthy())
    expect(undeclared()).toBeNull()
    expect(ageOnly()).toBeNull()
  })

  it('treats a stored value the Python side would not recognise as undeclared', async () => {
    // `normalize_gender_presentation` returns None for an unknown key, so the clause is empty and
    // the probe is inert — the prompt has to read the row the same way.
    mockApi(training({ gender_presentation: 'robot', age_band: 'ancient' }))
    harness(<Avatars />)
    await waitFor(() => expect(undeclared()).not.toBeNull())
  })

  it('does not prompt on an avatar whose training has not succeeded', async () => {
    mockApi(training({ status: 'processing' }))
    harness(<Avatars />)
    await waitFor(() => expect(screen.getByTestId('avatar-card-4')).toBeTruthy())
    expect(undeclared()).toBeNull()
    expect(ageOnly()).toBeNull()
  })
})
