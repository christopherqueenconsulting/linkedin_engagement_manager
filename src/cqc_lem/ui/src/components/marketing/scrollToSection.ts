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
