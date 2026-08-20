import { describe, expect, it, afterEach } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import LineChart, { type LinePoint } from './LineChart'

afterEach(cleanup)

function points(values: (number | null)[]): LinePoint[] {
  return values.map((y, i) => ({ x: `d${i}`, y }))
}

/** Reads the y-axis tick labels rendered in the chart, in DOM order (top to bottom). */
function tickLabels(): string[] {
  return screen.getAllByText(/^-?[\d.,]+[kKmM]?$/).map((el) => el.textContent ?? '')
}

describe('LineChart', () => {
  it('defaults the y-axis to start at 0 when rangePadding is not set', () => {
    render(<LineChart title="Followers" points={points([5000, 5010, 5005])} valueLabel="Followers" />)
    expect(tickLabels()).toContain('0')
  })

  it('hugs the data with rangePadding instead of always starting at 0 (#1700)', () => {
    render(
      <LineChart title="Followers" points={points([5000, 5010, 5005])} valueLabel="Followers" rangePadding={250} />,
    )
    const labels = tickLabels()
    expect(labels).not.toContain('0')
    // Lowest tick should sit near dataMin (5000) - 250 = 4750, not 0.
    const lowest = Math.min(...labels.map((l) => Number(l.replace(/,/g, ''))))
    expect(lowest).toBeGreaterThan(4000)
  })

  it('clamps the padded minimum at 0 so it never reads negative followers', () => {
    render(<LineChart title="Followers" points={points([100, 120, 90])} valueLabel="Followers" rangePadding={250} />)
    const labels = tickLabels().map((l) => Number(l.replace(/,/g, '')))
    expect(Math.min(...labels)).toBeGreaterThanOrEqual(0)
  })

  it('still renders the empty state when there is no data, regardless of rangePadding', () => {
    render(<LineChart title="Followers" points={points([null, null])} valueLabel="Followers" rangePadding={250} />)
    expect(screen.getByText('No data yet.')).not.toBeNull()
  })
})
