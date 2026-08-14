import { useEffect, useRef } from 'react'
import { EVENTS, capture } from '../utils/analytics'

// "Which section did the visitor actually reach?" for the marketing page (issue #1300).
//
// Fires `landing_section_viewed` the FIRST time a section is at least a quarter on screen, once per
// page load — a section the visitor scrolls back past is not a second view, and counting it as one
// would make a tall section look like the most engaging thing on the page.
//
// IntersectionObserver is absent in jsdom and in older browsers. The section still renders there;
// only the event is skipped. An analytics gap is the correct failure for a marketing page.
export function useSectionViewed<T extends Element>(section: string) {
  const ref = useRef<T | null>(null)
  const reported = useRef(false)

  useEffect(() => {
    const element = ref.current
    if (!element) return
    if (typeof IntersectionObserver === 'undefined') return

    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (!entry.isIntersecting || reported.current) continue
          reported.current = true
          capture(EVENTS.landingSectionViewed, { section })
          observer.disconnect()
        }
      },
      { threshold: 0.25 },
    )
    observer.observe(element)
    return () => observer.disconnect()
  }, [section])

  return ref
}
