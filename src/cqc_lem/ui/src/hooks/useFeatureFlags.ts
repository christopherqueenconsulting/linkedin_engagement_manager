import { useQuery } from '@tanstack/react-query'
import api from '../api/client'

// Runtime feature flags in the SPA (issue #651, docs/feature-flags.md).
//
// The values come from the SERVER (`GET /api/flags`), already resolved by utilities/flags.py with
// PostHog local evaluation and the env-var fallback. That is deliberate and it is the whole point:
//
// - It is a BOOTSTRAP — one payload, resolved before the browser asks, so a gated section renders
//   correctly on the first paint instead of flickering from default to real value.
// - It is the SAME evaluation the API and the Celery workers just did, so the three can never
//   disagree about a flag.
// - It still works with browser analytics fully off (no VITE_POSTHOG_KEY): flags are a product
//   control, not an analytics feature, and must not depend on posthog-js loading.
//
// Flags are NOT read through posthog-js here. analytics.ts stays the SPA's ONE PostHog surface for
// events/replay/identify; adding a second client-side posthog reader would reintroduce exactly the
// split-brain this endpoint removes.

// Keys mirror utilities/flags.py FLAGS — same string on both sides, no second registry.
export const FLAGS = {
  commentResearch: 'comment-research-enabled',
  tutorialVideos: 'tutorial-videos-enabled',
  feedFallbackDefault: 'feed-fallback-when-empty-default',
  costRouting: 'cost-routing-enabled',
  posthogSurveys: 'posthog-surveys-enabled',
  brandShowcase: 'brand-showcase-enabled',
} as const

export type FlagKey = (typeof FLAGS)[keyof typeof FLAGS]

export interface FlagBootstrap {
  distinct_id: string
  flags: Record<string, boolean>
  // false = every value above is an env default, not a PostHog decision.
  local_evaluation: boolean
}

const EMPTY: FlagBootstrap = { distinct_id: 'system', flags: {}, local_evaluation: false }

export function useFeatureFlags() {
  return useQuery({
    queryKey: ['feature-flags'],
    queryFn: () => {
      // Not a hook dependency on purpose: the flags query is mounted app-wide, and an anonymous
      // visitor must get a payload rather than an error.
      const token = localStorage.getItem('lem_session')
      const qs = token ? `?session_token=${encodeURIComponent(token)}` : ''
      return api.get(`/flags${qs}`).then((r) => r.data.detail as FlagBootstrap)
    },
    // A flag flip should reach an open tab without a reload, but polling every render is waste —
    // the worker-side poll interval (30s) is the cadence this mirrors.
    staleTime: 30 * 1000,
    refetchOnWindowFocus: true,
    // An unreachable API must never turn a gated feature on. `fallback` below owns that decision.
    retry: 1,
  })
}

/**
 * One flag's value. `fallback` is what to render while the payload is in flight OR when the API is
 * unreachable — so pass the SAFE value (usually the off/hidden one), exactly like the server falls
 * back to its env var.
 */
export function useFeatureFlag(key: FlagKey, fallback = false): boolean {
  const { data } = useFeatureFlags()
  const flags = (data ?? EMPTY).flags
  return key in flags ? flags[key] : fallback
}
