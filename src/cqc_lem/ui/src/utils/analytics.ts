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
        capture_performance: { web_vitals: true },
        // Replay is issue #650 (PH4). Session ids still resolve with it off, which is all the
        // feedback widget needs.
        disable_session_recording: true,
        session_recording: { maskAllInputs: true, maskTextSelector: MASK_SELECTOR },
      })
      // The feedback widget and any legacy script-tag reader look for window.posthog.
      ;(window as unknown as { posthog?: PostHog }).posthog = posthog
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
