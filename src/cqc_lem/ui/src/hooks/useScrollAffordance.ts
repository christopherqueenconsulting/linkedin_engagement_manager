import { useCallback, useEffect, useState } from 'react'

export interface ScrollAffordance<T extends HTMLElement> {
  attach: (node: T | null) => void
  canScrollUp: boolean
  canScrollDown: boolean
}

interface Affordance {
  canScrollUp: boolean
  canScrollDown: boolean
}

// Sub-pixel rounding leaves a fraction of a pixel of "overflow" on a list that is fully visible.
const EPSILON = 1

function affordanceOf(node: HTMLElement): Affordance {
  const { scrollTop, scrollHeight, clientHeight } = node
  return {
    canScrollUp: scrollTop > EPSILON,
    canScrollDown: scrollHeight - scrollTop - clientHeight > EPSILON,
  }
}

function same(a: Affordance, b: Affordance): boolean {
  return a.canScrollUp === b.canScrollUp && a.canScrollDown === b.canScrollDown
}

// Tracks whether a scroll container currently hides content above/below, so a caller can render a
// visible cue instead of leaving the overflow invisible (issue #583).
export function useScrollAffordance<T extends HTMLElement>(): ScrollAffordance<T> {
  const [node, setNode] = useState<T | null>(null)
  const [state, setState] = useState<Affordance>({ canScrollUp: false, canScrollDown: false })

  // Measured in the ref callback (commit time, children already mounted) rather than in an effect,
  // so the cue is right on the first paint.
  const attach = useCallback((next: T | null) => {
    setNode(next)
    if (next) {
      const measured = affordanceOf(next)
      setState((prev) => (same(prev, measured) ? prev : measured))
    }
  }, [])

  const measure = useCallback(() => {
    if (!node) return
    const next = affordanceOf(node)
    // Scrolling fires per frame — only re-render the page when the cue actually flips.
    setState((prev) => (same(prev, next) ? prev : next))
  }, [node])

  useEffect(() => {
    if (!node) return
    node.addEventListener('scroll', measure, { passive: true })
    window.addEventListener('resize', measure)
    // Content arriving after first paint (the activity feed re-polls every 30s) changes what
    // overflows without emitting a scroll or resize event — and the container's own box does not
    // change when it is height-constrained, so the growing child has to be observed too.
    const observer = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(measure) : null
    if (observer) {
      observer.observe(node)
      for (const child of Array.from(node.children)) observer.observe(child)
    }
    return () => {
      node.removeEventListener('scroll', measure)
      window.removeEventListener('resize', measure)
      observer?.disconnect()
    }
  }, [node, measure])

  return { attach, ...state }
}
