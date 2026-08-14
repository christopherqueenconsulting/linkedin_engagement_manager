/**
 * Scroll an in-page anchor into view, honouring `prefers-reduced-motion`.
 *
 * The CSS media query in `index.css` covers CSS-driven scrolling, but a JavaScript
 * `scrollIntoView({ behavior: 'smooth' })` ignores it entirely — the browser animates anyway. So
 * the preference has to be read here as well, or the page's one scripted motion is the one thing
 * that does not respect the setting.
 */
export function scrollToSection(id: string): void {
  const target = document.getElementById(id)
  if (!target) return
  const reduced =
    typeof window.matchMedia === 'function' &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches
  target.scrollIntoView({ behavior: reduced ? 'auto' : 'smooth' })
}

/**
 * Scroll to a section that may not have mounted yet.
 *
 * A nav anchor clicked from a legal page has to route back to `/` first, and the target section
 * does not exist until that render has committed — `scrollToSection` on its own would find nothing
 * and silently do nothing. Retries for a handful of frames, then gives up: a missing anchor is a
 * quiet no-op, never a spin.
 */
export function scrollToSectionSoon(id: string, attempts = 5): void {
  if (document.getElementById(id)) {
    scrollToSection(id)
    return
  }
  if (attempts <= 0) return
  const schedule =
    typeof window.requestAnimationFrame === 'function'
      ? window.requestAnimationFrame.bind(window)
      : (callback: FrameRequestCallback) => window.setTimeout(() => callback(0), 16)
  schedule(() => scrollToSectionSoon(id, attempts - 1))
}
