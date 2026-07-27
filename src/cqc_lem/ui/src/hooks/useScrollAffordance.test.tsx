import { describe, expect, it, afterEach, beforeEach } from 'vitest'
import { useCallback, useRef } from 'react'
import { act, cleanup, render, screen } from '@testing-library/react'
import { useScrollAffordance } from './useScrollAffordance'

interface Metrics {
  scrollHeight: number
  clientHeight: number
  scrollTop: number
}

// jsdom has no layout, so scrollHeight/clientHeight/scrollTop are all 0 — stamp them onto the
// instance the hook actually measures.
function stamp(node: HTMLElement, metrics: Metrics) {
  for (const [key, value] of Object.entries(metrics)) {
    Object.defineProperty(node, key, { value, configurable: true, writable: true })
  }
}

function Probe({ metrics }: { metrics: Metrics }) {
  const { attach: bind, canScrollUp, canScrollDown } = useScrollAffordance<HTMLDivElement>()
  const initial = useRef(metrics)
  // Stable identity: an inline ref callback would detach/re-attach on every render.
  const attach = useCallback(
    (node: HTMLDivElement | null) => {
      if (node) stamp(node, initial.current)
      bind(node)
    },
    [bind],
  )
  return (
    <>
      <div data-testid="scroller" ref={attach}>
        <ul>
          <li>row</li>
        </ul>
      </div>
      <span data-testid="up">{String(canScrollUp)}</span>
      <span data-testid="down">{String(canScrollDown)}</span>
    </>
  )
}

const flag = (id: string) => screen.getByTestId(id).textContent

type GlobalWithRO = { ResizeObserver?: unknown }
const globalRO = globalThis as unknown as GlobalWithRO

class FakeResizeObserver {
  static instances: FakeResizeObserver[] = []
  observed: Element[] = []
  disconnected = false
  callback: () => void
  constructor(callback: () => void) {
    this.callback = callback
    FakeResizeObserver.instances.push(this)
  }
  observe(target: Element) {
    this.observed.push(target)
  }
  unobserve(target: Element) {
    this.observed = this.observed.filter((o) => o !== target)
  }
  disconnect() {
    this.disconnected = true
  }
}

const latestObserver = () => FakeResizeObserver.instances[FakeResizeObserver.instances.length - 1]

afterEach(cleanup)

describe('useScrollAffordance (issue #583)', () => {
  it('reports no affordance when the content fits', () => {
    render(<Probe metrics={{ scrollHeight: 320, clientHeight: 320, scrollTop: 0 }} />)
    expect(flag('down')).toBe('false')
    expect(flag('up')).toBe('false')
  })

  it('reports more-below on first measure, without waiting for a scroll event', () => {
    render(<Probe metrics={{ scrollHeight: 900, clientHeight: 400, scrollTop: 0 }} />)
    expect(flag('down')).toBe('true')
    expect(flag('up')).toBe('false')
  })

  it('re-measures on scroll and drops the cue at the bottom', () => {
    render(<Probe metrics={{ scrollHeight: 900, clientHeight: 400, scrollTop: 0 }} />)
    const scroller = screen.getByTestId('scroller')

    stamp(scroller, { scrollHeight: 900, clientHeight: 400, scrollTop: 250 })
    act(() => {
      scroller.dispatchEvent(new Event('scroll'))
    })
    expect(flag('down')).toBe('true')
    expect(flag('up')).toBe('true')

    stamp(scroller, { scrollHeight: 900, clientHeight: 400, scrollTop: 500 })
    act(() => {
      scroller.dispatchEvent(new Event('scroll'))
    })
    expect(flag('down')).toBe('false')
    expect(flag('up')).toBe('true')
  })

  it('re-measures when the viewport resizes', () => {
    render(<Probe metrics={{ scrollHeight: 900, clientHeight: 900, scrollTop: 0 }} />)
    expect(flag('down')).toBe('false')

    stamp(screen.getByTestId('scroller'), { scrollHeight: 900, clientHeight: 300, scrollTop: 0 })
    act(() => {
      window.dispatchEvent(new Event('resize'))
    })
    expect(flag('down')).toBe('true')
  })

  it('ignores sub-pixel overflow', () => {
    render(<Probe metrics={{ scrollHeight: 320.4, clientHeight: 320, scrollTop: 0 }} />)
    expect(flag('down')).toBe('false')
  })

  // Owns the global rather than asserting jsdom's current shape: jsdom shipping ResizeObserver one
  // day must not quietly take this branch out of the suite.
  it('still measures and unmounts cleanly with no ResizeObserver available', () => {
    const original = globalRO.ResizeObserver
    delete globalRO.ResizeObserver
    try {
      const view = render(<Probe metrics={{ scrollHeight: 900, clientHeight: 400, scrollTop: 0 }} />)
      expect(flag('down')).toBe('true')
      expect(() => view.unmount()).not.toThrow()
    } finally {
      if (original === undefined) delete globalRO.ResizeObserver
      else globalRO.ResizeObserver = original
    }
  })
})

// The 30s activity re-poll is the case the hook exists for: it changes what overflows WITHOUT
// firing a scroll or resize event, and the height-constrained container's own box never moves —
// so only an observation of the growing child reports it. jsdom has no ResizeObserver, so without
// a stub this whole path ships untested.
describe('useScrollAffordance ResizeObserver wiring (issue #583)', () => {
  let original: unknown

  beforeEach(() => {
    original = globalRO.ResizeObserver
    FakeResizeObserver.instances = []
    globalRO.ResizeObserver = FakeResizeObserver
  })

  afterEach(() => {
    if (original === undefined) delete globalRO.ResizeObserver
    else globalRO.ResizeObserver = original
  })

  it('observes the scroll container AND its children', () => {
    render(<Probe metrics={{ scrollHeight: 320, clientHeight: 320, scrollTop: 0 }} />)
    const scroller = screen.getByTestId('scroller')

    expect(latestObserver().observed).toContain(scroller)
    expect(latestObserver().observed).toContain(scroller.querySelector('ul'))
  })

  it('re-measures when the list grows under an unchanged container box', () => {
    render(<Probe metrics={{ scrollHeight: 320, clientHeight: 320, scrollTop: 0 }} />)
    expect(flag('down')).toBe('false')

    // clientHeight unchanged — only the content grew, which is exactly what a re-poll does.
    stamp(screen.getByTestId('scroller'), { scrollHeight: 900, clientHeight: 320, scrollTop: 0 })
    act(() => {
      latestObserver().callback()
    })
    expect(flag('down')).toBe('true')
  })

  it('drops the cue when the list shrinks back to fitting', () => {
    render(<Probe metrics={{ scrollHeight: 900, clientHeight: 320, scrollTop: 0 }} />)
    expect(flag('down')).toBe('true')

    stamp(screen.getByTestId('scroller'), { scrollHeight: 320, clientHeight: 320, scrollTop: 0 })
    act(() => {
      latestObserver().callback()
    })
    expect(flag('down')).toBe('false')
  })

  it('disconnects the observer on unmount', () => {
    const view = render(<Probe metrics={{ scrollHeight: 900, clientHeight: 400, scrollTop: 0 }} />)
    const observer = latestObserver()
    expect(observer.disconnected).toBe(false)

    view.unmount()
    expect(observer.disconnected).toBe(true)
  })
})
