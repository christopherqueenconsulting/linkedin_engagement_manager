import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

// One fake posthog-js shared by every import of the module under test.
const ph = {
  init: vi.fn(),
  capture: vi.fn(),
  identify: vi.fn(),
  reset: vi.fn(),
  get_session_id: vi.fn(() => 'sess-123'),
}
vi.mock('posthog-js', () => ({ posthog: ph }))

// The key is read at module scope, so each case stubs it and re-imports.
async function loadAnalytics(key?: string) {
  vi.resetModules()
  if (key) vi.stubEnv('VITE_POSTHOG_KEY', key)
  else vi.stubEnv('VITE_POSTHOG_KEY', '')
  return import('./analytics')
}

beforeEach(() => {
  Object.values(ph).forEach((fn) => fn.mockClear())
  delete (window as unknown as { posthog?: unknown }).posthog
})

afterEach(() => {
  vi.unstubAllEnvs()
})

describe('with no VITE_POSTHOG_KEY', () => {
  it('reports itself disabled and never loads posthog-js', async () => {
    const a = await loadAnalytics()
    expect(a.analyticsEnabled()).toBe(false)
    await expect(a.initAnalytics()).resolves.toBeNull()
    expect(ph.init).not.toHaveBeenCalled()
  })

  it('swallows every capture, identify and reset', async () => {
    const a = await loadAnalytics()
    a.capture('post_approved', { post_id: 1 })
    a.capturePageview()
    a.identifyUser({ userId: 5 })
    a.resetAnalytics()
    await Promise.resolve()
    expect(ph.capture).not.toHaveBeenCalled()
    expect(ph.identify).not.toHaveBeenCalled()
    expect(ph.reset).not.toHaveBeenCalled()
  })

  it('still resolves a session id from a script-tag posthog', async () => {
    const a = await loadAnalytics()
    ;(window as unknown as { posthog?: unknown }).posthog = { get_session_id: () => 'legacy-1' }
    expect(a.analyticsSessionId()).toBe('legacy-1')
  })

  it('reports no session id when nothing is loaded', async () => {
    const a = await loadAnalytics()
    expect(a.analyticsSessionId()).toBeUndefined()
  })
})

describe('with a key configured', () => {
  it('inits with the SPA privacy defaults and router-owned pageviews', async () => {
    const a = await loadAnalytics('phc_test')
    expect(a.analyticsEnabled()).toBe(true)
    await a.initAnalytics()
    expect(ph.init).toHaveBeenCalledTimes(1)
    const [key, config] = ph.init.mock.calls[0]
    expect(key).toBe('phc_test')
    expect(config.capture_pageview).toBe(false)
    expect(config.autocapture).toBe(true)
    expect(config.mask_all_element_attributes).toBe(true)
    expect(config.capture_performance).toEqual({ web_vitals: true })
    expect(config.session_recording.maskAllInputs).toBe(true)
    expect(config.session_recording.maskTextSelector).toBe('[data-ph-mask]')
  })

  it('inits only once no matter how many callers ask', async () => {
    const a = await loadAnalytics('phc_test')
    await Promise.all([a.initAnalytics(), a.initAnalytics(), a.initAnalytics()])
    expect(ph.init).toHaveBeenCalledTimes(1)
  })

  it('identifies on the backend distinct_id convention with plan facts', async () => {
    const a = await loadAnalytics('phc_test')
    a.identifyUser({
      userId: 42,
      email: 'me@example.com',
      plan: 'premium',
      planStatus: 'active',
      timezone: 'America/Chicago',
      createdAt: '2026-01-02T03:04:05Z',
    })
    await vi.waitFor(() => expect(ph.identify).toHaveBeenCalled())
    expect(ph.identify).toHaveBeenCalledWith(
      '42',
      {
        email: 'me@example.com',
        plan: 'premium',
        plan_status: 'active',
        timezone: 'America/Chicago',
      },
      { created_at: '2026-01-02T03:04:05Z' }
    )
  })

  it('omits person properties it was not given', async () => {
    const a = await loadAnalytics('phc_test')
    a.identifyUser({ userId: 42 })
    await vi.waitFor(() => expect(ph.identify).toHaveBeenCalled())
    expect(ph.identify).toHaveBeenCalledWith('42', {}, undefined)
  })

  it('captures product events and router pageviews', async () => {
    const a = await loadAnalytics('phc_test')
    a.capture(a.EVENTS.postApproved, { post_type: 'text', archetype: 'build_receipt' })
    a.capturePageview()
    await vi.waitFor(() => expect(ph.capture).toHaveBeenCalledTimes(2))
    expect(ph.capture).toHaveBeenCalledWith('post_approved', {
      post_type: 'text', archetype: 'build_receipt',
    })
    expect(ph.capture.mock.calls[1][0]).toBe('$pageview')
  })

  it('resets the person on logout', async () => {
    const a = await loadAnalytics('phc_test')
    a.resetAnalytics()
    await vi.waitFor(() => expect(ph.reset).toHaveBeenCalled())
  })

  it('exposes the client for the feedback widget session id', async () => {
    const a = await loadAnalytics('phc_test')
    await a.initAnalytics()
    expect(a.analyticsSessionId()).toBe('sess-123')
  })

  it('never lets a posthog error reach the caller', async () => {
    const a = await loadAnalytics('phc_test')
    ph.capture.mockImplementationOnce(() => { throw new Error('blocked') })
    expect(() => a.capture('post_rejected')).not.toThrow()
    ph.get_session_id.mockImplementationOnce(() => { throw new Error('blocked') })
    await a.initAnalytics()
    expect(a.analyticsSessionId()).toBeUndefined()
  })
})

describe('maskProps', () => {
  it('marks an editor for both autocapture opt-out and replay masking', async () => {
    const a = await loadAnalytics()
    expect(a.maskProps('w-full text-sm')).toEqual({
      className: 'w-full text-sm ph-no-capture',
      'data-ph-mask': 'true',
    })
  })

  it('works with no base class', async () => {
    const a = await loadAnalytics()
    expect(a.maskProps().className).toBe('ph-no-capture')
  })
})
