// First-touch attribution for the launch funnel (issue #503). The UTMs are on the URL of the very
// first page a visitor lands on, but signup happens later and possibly after several navigations —
// so capture once into sessionStorage and replay it with the auth calls. First touch wins: a later
// tagged link inside the same session must not overwrite the one that actually acquired the visit.

export type Attribution = {
  utm_source?: string
  utm_medium?: string
  utm_campaign?: string
  utm_content?: string
  utm_term?: string
  // The referral link's referrer id (issue #658, `?ref=<user id>`). Read with the UTMs because a
  // referral link carries both and first touch has to keep them together.
  ref?: string
  referrer?: string
  landing_page?: string
}

const STORAGE_KEY = 'lem_attribution'
const UTM_KEYS = [
  'utm_source',
  'utm_medium',
  'utm_campaign',
  'utm_content',
  'utm_term',
  'ref',
] as const

function readStored(): Attribution | null {
  try {
    const raw = window.sessionStorage.getItem(STORAGE_KEY)
    return raw ? (JSON.parse(raw) as Attribution) : null
  } catch {
    return null
  }
}

// Call once on app load, before the visitor can navigate away from the landing URL.
export function captureAttribution(): Attribution {
  const existing = readStored()
  if (existing) return existing

  const attribution: Attribution = {}
  try {
    const params = new URLSearchParams(window.location.search)
    for (const key of UTM_KEYS) {
      const value = params.get(key)?.trim()
      if (value) attribution[key] = value
    }
    // An internal referrer tells us nothing about acquisition.
    const referrer = document.referrer
    if (referrer && !referrer.startsWith(window.location.origin)) attribution.referrer = referrer
    attribution.landing_page = window.location.pathname
    window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify(attribution))
  } catch {
    // Private mode / storage disabled — the funnel event just lands in the "direct" bucket.
  }
  return attribution
}

export function getAttribution(): Attribution {
  return readStored() ?? {}
}
