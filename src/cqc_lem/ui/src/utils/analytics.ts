// The SPA's ONE PostHog surface (issue #646). Everything browser-side — init, $identify,
// autocapture, pageviews, web vitals and the named product events — goes through here so there is
// a single place that knows the key, the privacy defaults and the distinct_id convention.
//
// distinct_id is String(user_id), exactly what the backend uses (observability.py), so a user's
// server events and browser events land on the SAME PostHog person.
//
// Disabled means DISABLED: with no VITE_POSTHOG_KEY the posthog-js chunk is never imported, so the
// build ships it as a separate lazy chunk the browser never fetches and no request is ever made.

import type { PostHog } from 'posthog-js'

const KEY = import.meta.env.VITE_POSTHOG_KEY as string | undefined
const HOST = (import.meta.env.VITE_POSTHOG_HOST as string | undefined) || 'https://us.i.posthog.com'

// Session replay (issue #649). Recordings are quota'd, so the SDK — not a project setting — owns
// WHO is recorded: a sampled slice of ordinary sessions, plus every session that produces an
// $exception. Local `sampleRate` takes precedence over the project's sampling setting, so the two
// can never multiply into a rate nobody intended.
const DEFAULT_REPLAY_SAMPLE = 0.1

function envFlag(value: string | undefined, fallback: boolean): boolean {
  const raw = (value ?? '').trim().toLowerCase()
  if (!raw) return fallback
  return !['0', 'false', 'no', 'off'].includes(raw)
}

function replaySampleRate(): number {
  const raw = (import.meta.env.VITE_POSTHOG_REPLAY_SAMPLE as string | undefined) ?? ''
  if (!raw.trim()) return DEFAULT_REPLAY_SAMPLE
  const value = Number(raw)
  if (!Number.isFinite(value)) return DEFAULT_REPLAY_SAMPLE
  return Math.min(1, Math.max(0, value))
}

const REPLAY_ENABLED = envFlag(import.meta.env.VITE_POSTHOG_REPLAY as string | undefined, true)
const REPLAY_SAMPLE = replaySampleRate()

// Any element carrying this attribute holds the user's own content (a DM, a story, a draft post).
// Its text is masked in replay (PH4) and it is opted out of autocapture entirely.
export const MASK_ATTRIBUTE = 'data-ph-mask'
export const MASK_SELECTOR = `[${MASK_ATTRIBUTE}]`
// posthog-js's own element opt-out class — autocapture skips an element (and its descendants)
// carrying it. Applied alongside MASK_ATTRIBUTE, which only replay understands.
export const MASK_CLASS = 'ph-no-capture'
// Spread onto a content editor to mask it in both surfaces: <textarea {...maskProps('w-full …')} />
export function maskProps(className = ''): { className: string } & Record<string, string> {
  return { className: `${className} ${MASK_CLASS}`.trim(), [MASK_ATTRIBUTE]: 'true' }
}

export function analyticsEnabled(): boolean {
  return !!KEY
}

export function replayEnabled(): boolean {
  return !!KEY && REPLAY_ENABLED
}

// The session id we have already forced a recording for, so a page throwing in a loop costs one
// override and not one full snapshot per exception. Deliberately NOT `sessionRecordingStarted()`:
// posthog attaches rrweb for every session and the sampling decision only governs whether the
// buffer is SENT, so that method reads `true` in exactly the sampled-OUT case this override exists
// for. A new session id is a new sampling decision, so it re-arms.
let forcedReplaySession: string | null = null

function forceSessionRecording(ph: PostHog): void {
  if (!REPLAY_ENABLED) return
  try {
    let sessionId = ''
    try {
      sessionId = ph.get_session_id?.() || ''
    } catch {
      // No resolvable session id — fall through and force once.
    }
    if (forcedReplaySession !== null && forcedReplaySession === sessionId) return
    forcedReplaySession = sessionId
    ph.startSessionRecording({ sampling: true })
  } catch {
    // A recording decision must never surface as a UI error.
  }
}

// Record THIS session even if sampling left it out. Called for the two moments a recording is worth
// more than the quota it spends: an exception, and a user opening the feedback widget — a report
// whose replay link 404s is worse than no link, and only ~REPLAY_SAMPLE of sessions are recorded
// otherwise. Recording starts HERE; the lead-up exists only for the sampled slice.
export function ensureSessionRecorded(): void {
  withClient(forceSessionRecording)
}

// The recording rule that matters: a session that threw is the session someone will want to watch,
// so it is recorded even when sampling left it out. `eventCaptured` fires for BOTH posthog's own
// unhandled-error autocapture and captureException() below, so the rule lives in exactly one place.
function armErrorTriggeredReplay(ph: PostHog): void {
  if (!REPLAY_ENABLED) return
  try {
    ph.on('eventCaptured', (event: { event?: string } | undefined) => {
      if (event?.event !== '$exception') return
      forceSessionRecording(ph)
    })
  } catch {
    // Older/stubbed clients without the hook simply keep the sampled-only behavior.
  }
}

let client: PostHog | null = null
let loading: Promise<PostHog | null> | null = null

export function initAnalytics(): Promise<PostHog | null> {
  if (!KEY) return Promise.resolve(null)
  if (loading) return loading
  loading = import('posthog-js')
    .then(({ posthog }) => {
      posthog.init(KEY, {
        api_host: HOST,
        // The router owns pageviews — posthog's own listener fires once per full page load and
        // would miss every in-app navigation.
        capture_pageview: false,
        capture_pageleave: true,
        autocapture: true,
        // Attribute values can carry content (a post title in aria-label, a draft in value).
        mask_all_element_attributes: true,
        // network_timing rides with replay: PerformanceObserver entries (url, status, duration) so a
        // recording shows WHICH call failed. Never the request/response bodies — see below.
        capture_performance: { web_vitals: true, network_timing: REPLAY_ENABLED },
        // Error tracking (issue #648). Unhandled errors and rejections become grouped $exception
        // issues; console.error stays OFF — it is noisy and routinely carries user content in the
        // message, which the rest of this module goes out of its way to keep out of PostHog.
        capture_exceptions: {
          capture_unhandled_errors: true,
          capture_unhandled_rejections: true,
          capture_console_errors: false,
        },
        // Replay (issue #649). Session ids resolve either way, which is all the feedback widget
        // needs — VITE_POSTHOG_REPLAY=false turns the recording itself off and nothing else.
        disable_session_recording: !REPLAY_ENABLED,
        // Console lines land on the replay timeline. Unlike console.error->$exception (deliberately
        // off), this stays inside the recording, which is already masked and access-controlled.
        enable_recording_console_log: REPLAY_ENABLED,
        session_recording: {
          maskAllInputs: true,
          maskTextSelector: MASK_SELECTOR,
          sampleRate: REPLAY_SAMPLE,
          // Measure the minimum duration against the recorded buffer, not wall-clock session age,
          // so a bounce spread over two page loads is still dropped as a bounce.
          strictMinimumDuration: true,
          // A body is the user's own LinkedIn content and a header is their session token; timing
          // (above) is what debugging actually needs.
          recordHeaders: false,
          recordBody: false,
          // Canvas is the carousel/slide preview — expensive to record and never the bug.
          captureCanvas: { recordCanvas: false },
        },
      })
      // The feedback widget and any legacy script-tag reader look for window.posthog.
      ;(window as unknown as { posthog?: PostHog }).posthog = posthog
      armErrorTriggeredReplay(posthog)
      client = posthog
      return posthog
    })
    .catch(() => {
      // Blocked by an ad blocker or a bad key — analytics must never take the app down with it.
      return null
    })
  return loading
}

function withClient(fn: (ph: PostHog) => void): void {
  if (!KEY) return
  ;(loading ?? initAnalytics()).then((ph) => {
    if (!ph) return
    try {
      fn(ph)
    } catch {
      // Never let an analytics call surface as a UI error.
    }
  })
}

export type AnalyticsIdentity = {
  userId: number
  email?: string | null
  plan?: string | null
  planStatus?: string | null
  timezone?: string | null
  createdAt?: string | null
}

// Unify the anonymous browser person with the backend's str(user_id) person. Plan facts are set on
// every call (they change); the signup date is $set_once so a later call can't rewrite history.
// Deliberately no LinkedIn credentials, cookies or profile data — email is the only PII.
export function identifyUser(identity: AnalyticsIdentity): void {
  const props: Record<string, unknown> = {}
  if (identity.email) props.email = identity.email
  if (identity.plan) props.plan = identity.plan
  if (identity.planStatus) props.plan_status = identity.planStatus
  if (identity.timezone) props.timezone = identity.timezone
  const setOnce = identity.createdAt ? { created_at: identity.createdAt } : undefined
  withClient((ph) => ph.identify(String(identity.userId), props, setOnce))
}

// Logout must break the link between this browser and the person, or the next user on the same
// machine inherits their events.
export function resetAnalytics(): void {
  withClient((ph) => ph.reset())
}

export function capture(event: string, properties?: Record<string, unknown>): void {
  withClient((ph) => ph.capture(event, properties))
}

// A caught error worth an error-tracking issue (an API failure the user was shown, a render the
// boundary rescued). Autocapture only sees what reaches window, so anything we catch needs this.
// Never `capture('$exception', …)` — only captureException produces a groupable stack trace.
export function captureException(error: unknown, properties?: Record<string, unknown>): void {
  withClient((ph) => ph.captureException(error, properties))
}

export function capturePageview(): void {
  withClient((ph) =>
    ph.capture('$pageview', {
      $current_url: window.location.href,
      path: window.location.pathname,
    })
  )
}

// Synchronous on purpose — the feedback widget attaches it to a report at submit time. Falls back
// to a window-global posthog so a script-tag install still resolves.
export function analyticsSessionId(): string | undefined {
  const ph = client ?? (window as unknown as { posthog?: PostHog }).posthog
  try {
    return ph?.get_session_id?.() || undefined
  } catch {
    return undefined
  }
}

// The moments autocapture cannot name. Keep this vocabulary stable — PostHog insights key off it.
export const EVENTS = {
  postApproved: 'post_approved',
  postRejected: 'post_rejected',
  prefsSaved: 'prefs_saved',
  dmTemplateSaved: 'dm_template_saved',
  storyBankEntryAdded: 'story_bank_entry_added',
  contentPlanGenerated: 'content_plan_generated',
  feedbackOpened: 'feedback_opened',
} as const
