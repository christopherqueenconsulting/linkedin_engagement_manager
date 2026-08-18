import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

// One fake posthog-js shared by every import of the module under test.
type CapturedEvent = { event?: string }
let onEventCaptured: ((event: CapturedEvent) => void) | null = null

const ph = {
  init: vi.fn(),
  capture: vi.fn(),
  captureException: vi.fn(),
  identify: vi.fn(),
  reset: vi.fn(),
  get_session_id: vi.fn(() => 'sess-123'),
  on: vi.fn((event: string, cb: (e: CapturedEvent) => void) => {
    if (event === 'eventCaptured') onEventCaptured = cb
    return () => undefined
  }),
  startSessionRecording: vi.fn(),
  sessionRecordingStarted: vi.fn(() => false),
  setPersonProperties: vi.fn(),
}
vi.mock('posthog-js', () => ({ posthog: ph }))

// The key, the host and the replay settings are read at module scope, so each case stubs them and
// re-imports.
async function loadAnalytics(key?: string, env: Record<string, string> = {}) {
  vi.resetModules()
  onEventCaptured = null
  vi.stubEnv('VITE_POSTHOG_KEY', key || '')
  for (const [name, value] of Object.entries(env)) vi.stubEnv(name, value)
  return import('./analytics')
}

beforeEach(() => {
  Object.values(ph).forEach((fn) => fn.mockClear())
  delete (window as unknown as { posthog?: unknown }).posthog
  window.sessionStorage.clear()
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
    a.captureException(new Error('nope'))
    a.identifyUser({ userId: 5 })
    a.resetAnalytics()
    await Promise.resolve()
    expect(ph.capture).not.toHaveBeenCalled()
    expect(ph.captureException).not.toHaveBeenCalled()
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
    expect(config.capture_performance).toEqual({ web_vitals: true, network_timing: true })
    expect(config.session_recording.maskAllInputs).toBe(true)
    expect(config.session_recording.maskTextSelector).toBe('[data-ph-mask]')
  })

  it('addresses captures at the reverse proxy, with the app host kept separate', async () => {
    // Issue #1677. A direct `us.i.posthog.com` is blocked BY NAME by tracking protection, and the
    // resulting loss is indistinguishable from a quiet day — so the default must be our own domain.
    // `ui_host` is not decoration: without it posthog-js builds toolbar and replay links off
    // api_host, which is a proxy with no PostHog UI behind it.
    const a = await loadAnalytics('phc_test', { VITE_POSTHOG_HOST: '' })
    await a.initAnalytics()
    const [, config] = ph.init.mock.calls[0]
    expect(config.api_host).toBe('https://lemt.christopherqueenconsulting.com')
    expect(config.ui_host).toBe('https://us.posthog.com')
  })

  it('lets VITE_POSTHOG_HOST move ingestion without moving the app host', async () => {
    const a = await loadAnalytics('phc_test', { VITE_POSTHOG_HOST: 'https://ph.example.test' })
    await a.initAnalytics()
    const [, config] = ph.init.mock.calls[0]
    expect(config.api_host).toBe('https://ph.example.test')
    expect(config.ui_host).toBe('https://us.posthog.com')
  })

  it('autocaptures unhandled exceptions but never console.error', async () => {
    const a = await loadAnalytics('phc_test')
    await a.initAnalytics()
    const [, config] = ph.init.mock.calls[0]
    expect(config.capture_exceptions).toEqual({
      capture_unhandled_errors: true,
      capture_unhandled_rejections: true,
      capture_console_errors: false,
    })
  })

  it('captures a caught error through captureException, not capture()', async () => {
    const a = await loadAnalytics('phc_test')
    const err = new Error('save failed')
    a.captureException(err, { surface: 'account' })
    await vi.waitFor(() => expect(ph.captureException).toHaveBeenCalled())
    expect(ph.captureException).toHaveBeenCalledWith(err, { surface: 'account' })
    expect(ph.capture).not.toHaveBeenCalled()
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

describe('session replay', () => {
  it('records a sampled slice with content masked and no payloads', async () => {
    const a = await loadAnalytics('phc_test')
    expect(a.replayEnabled()).toBe(true)
    await a.initAnalytics()
    const [, config] = ph.init.mock.calls[0]
    expect(config.disable_session_recording).toBe(false)
    expect(config.enable_recording_console_log).toBe(true)
    expect(config.session_recording).toEqual({
      maskAllInputs: true,
      maskTextSelector: '[data-ph-mask]',
      sampleRate: 0.1,
      strictMinimumDuration: true,
      recordHeaders: false,
      recordBody: false,
      captureCanvas: { recordCanvas: false },
    })
  })

  it('takes the sample rate from the build env and clamps nonsense', async () => {
    let a = await loadAnalytics('phc_test', { VITE_POSTHOG_REPLAY_SAMPLE: '0.25' })
    await a.initAnalytics()
    expect(ph.init.mock.calls[0][1].session_recording.sampleRate).toBe(0.25)

    ph.init.mockClear()
    a = await loadAnalytics('phc_test', { VITE_POSTHOG_REPLAY_SAMPLE: '7' })
    await a.initAnalytics()
    expect(ph.init.mock.calls[0][1].session_recording.sampleRate).toBe(1)

    ph.init.mockClear()
    a = await loadAnalytics('phc_test', { VITE_POSTHOG_REPLAY_SAMPLE: 'lots' })
    await a.initAnalytics()
    expect(ph.init.mock.calls[0][1].session_recording.sampleRate).toBe(0.1)
  })

  it('records every session that throws, overriding the sample', async () => {
    const a = await loadAnalytics('phc_test')
    await a.initAnalytics()
    expect(onEventCaptured).toBeTypeOf('function')
    onEventCaptured!({ event: '$exception' })
    expect(ph.startSessionRecording).toHaveBeenCalledWith({ sampling: true })
  })

  // posthog attaches rrweb for EVERY session — sampling only decides whether the buffer is sent —
  // so sessionRecordingStarted() reads true for a sampled-OUT session and must never gate this.
  it('still overrides when rrweb is already attached but the session was sampled out', async () => {
    const a = await loadAnalytics('phc_test')
    await a.initAnalytics()
    ph.sessionRecordingStarted.mockReturnValueOnce(true)
    onEventCaptured!({ event: '$exception' })
    expect(ph.startSessionRecording).toHaveBeenCalledWith({ sampling: true })
  })

  it('ignores other events and overrides at most once per session', async () => {
    const a = await loadAnalytics('phc_test')
    await a.initAnalytics()
    onEventCaptured!({ event: '$pageview' })
    expect(ph.startSessionRecording).not.toHaveBeenCalled()

    onEventCaptured!({ event: '$exception' })
    onEventCaptured!({ event: '$exception' })
    expect(ph.startSessionRecording).toHaveBeenCalledTimes(1)

    // A new session id is a fresh sampling decision, so the trigger re-arms.
    ph.get_session_id.mockReturnValueOnce('sess-456')
    onEventCaptured!({ event: '$exception' })
    expect(ph.startSessionRecording).toHaveBeenCalledTimes(2)
  })

  it('never lets a recording decision throw into the page', async () => {
    const a = await loadAnalytics('phc_test')
    await a.initAnalytics()
    ph.startSessionRecording.mockImplementationOnce(() => { throw new Error('blocked') })
    expect(() => onEventCaptured!({ event: '$exception' })).not.toThrow()
  })

  it('ships no recording at all when replay is turned off', async () => {
    const a = await loadAnalytics('phc_test', { VITE_POSTHOG_REPLAY: 'false' })
    expect(a.replayEnabled()).toBe(false)
    await a.initAnalytics()
    const [, config] = ph.init.mock.calls[0]
    expect(config.disable_session_recording).toBe(true)
    expect(config.enable_recording_console_log).toBe(false)
    expect(config.capture_performance).toEqual({ web_vitals: true, network_timing: false })
    expect(ph.on).not.toHaveBeenCalled()
  })

  it('is off with analytics itself off', async () => {
    const a = await loadAnalytics()
    expect(a.replayEnabled()).toBe(false)
  })

  it('records on demand so a feedback report always has a replay to link', async () => {
    const a = await loadAnalytics('phc_test')
    await a.initAnalytics()
    a.ensureSessionRecorded()
    await Promise.resolve()
    expect(ph.startSessionRecording).toHaveBeenCalledWith({ sampling: true })

    // Shares the once-per-session budget with the error trigger.
    onEventCaptured!({ event: '$exception' })
    expect(ph.startSessionRecording).toHaveBeenCalledTimes(1)
  })

  it('records nothing on demand when replay is off', async () => {
    const a = await loadAnalytics('phc_test', { VITE_POSTHOG_REPLAY: 'false' })
    await a.initAnalytics()
    a.ensureSessionRecorded()
    await Promise.resolve()
    expect(ph.startSessionRecording).not.toHaveBeenCalled()
  })
})

// Issue #658: marketing attribution. The first touch is captured into sessionStorage by
// utils/attribution.ts; these cases prove it reaches the PostHog person and the signup event, which
// is what makes a signup readable as a CHANNEL conversion rather than as direct traffic.
describe('first-touch attribution', () => {
  const FIRST_TOUCH = {
    utm_source: 'linkedin',
    utm_medium: 'social',
    utm_campaign: 'post-12',
    ref: '7',
    landing_page: '/',
  }

  function storeFirstTouch() {
    window.sessionStorage.setItem('lem_attribution', JSON.stringify(FIRST_TOUCH))
  }

  it('rides onto the person as $set_once keys matching the backend convention', async () => {
    storeFirstTouch()
    const a = await loadAnalytics('phc_test')
    a.identifyUser({ userId: 42, createdAt: '2026-01-02T03:04:05Z' })
    await vi.waitFor(() => expect(ph.identify).toHaveBeenCalled())
    expect(ph.identify.mock.calls[0][2]).toEqual({
      initial_utm_source: 'linkedin',
      initial_utm_medium: 'social',
      initial_utm_campaign: 'post-12',
      initial_ref: '7',
      initial_landing_page: '/',
      created_at: '2026-01-02T03:04:05Z',
    })
  })

  it('sends no set-once payload at all for a direct visit', async () => {
    const a = await loadAnalytics('phc_test')
    a.identifyUser({ userId: 42 })
    await vi.waitFor(() => expect(ph.identify).toHaveBeenCalled())
    expect(ph.identify).toHaveBeenCalledWith('42', {}, undefined)
  })

  it('records a signup with its attribution, on a distinct event name from the API\'s', async () => {
    storeFirstTouch()
    const a = await loadAnalytics('phc_test')
    a.recordSignup({ method: 'email_pin', pin_bypassed: false })
    await vi.waitFor(() => expect(ph.capture).toHaveBeenCalled())
    expect(a.EVENTS.signupCompletedWeb).toBe('signup_completed_web')
    expect(ph.capture).toHaveBeenCalledWith('signup_completed_web', {
      ...FIRST_TOUCH,
      method: 'email_pin',
      pin_bypassed: false,
    })
    // Written on the still-anonymous person so the identify that follows merges it in.
    expect(ph.setPersonProperties).toHaveBeenCalledWith(undefined, {
      initial_utm_source: 'linkedin',
      initial_utm_medium: 'social',
      initial_utm_campaign: 'post-12',
      initial_ref: '7',
      initial_landing_page: '/',
    })
  })

  it('captures the signup even when there is no attribution to carry', async () => {
    const a = await loadAnalytics('phc_test')
    a.recordSignup()
    await vi.waitFor(() => expect(ph.capture).toHaveBeenCalled())
    expect(ph.capture).toHaveBeenCalledWith('signup_completed_web', {})
    expect(ph.setPersonProperties).not.toHaveBeenCalled()
  })

  it('is a no-op with analytics disabled', async () => {
    storeFirstTouch()
    const a = await loadAnalytics()
    a.recordSignup()
    await Promise.resolve()
    expect(ph.capture).not.toHaveBeenCalled()
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
