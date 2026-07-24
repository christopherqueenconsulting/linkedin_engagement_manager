import { useMemo, useRef, useState } from 'react'
import { VIZ } from './palette'

export interface LinePoint {
  /** Short x-axis label (e.g. a date). */
  x: string
  /** Value at this point, or null for a gap (no data / not measurable). */
  y: number | null
}

interface LineChartProps {
  title: string
  subtitle?: string
  points: LinePoint[]
  /** Formats a value for the y-axis ticks, end-label, tooltip, and table. */
  format?: (v: number) => string
  /** Tooltip/table header for the value column. */
  valueLabel: string
  emptyMessage?: string
}

// viewBox geometry (unitless; the SVG scales to its container width).
const W = 720
const H = 250
const M = { top: 16, right: 60, bottom: 34, left: 52 }
const PLOT_W = W - M.left - M.right
const PLOT_H = H - M.top - M.bottom

function niceTicks(max: number, count = 4): number[] {
  if (max <= 0) return [0]
  const raw = max / count
  const mag = Math.pow(10, Math.floor(Math.log10(raw)))
  const norm = raw / mag
  const step = (norm >= 5 ? 10 : norm >= 2 ? 5 : norm >= 1 ? 2 : 1) * mag
  const ticks: number[] = []
  for (let t = 0; t <= max + step / 2; t += step) ticks.push(t)
  return ticks
}

export default function LineChart({ title, subtitle, points, format, valueLabel, emptyMessage }: LineChartProps) {
  const fmt = format ?? ((v: number) => v.toLocaleString())
  const svgRef = useRef<SVGSVGElement>(null)
  const [hover, setHover] = useState<number | null>(null)

  const values = points.map((p) => p.y).filter((v): v is number => v != null)
  const hasData = values.length > 0

  const { positions, ticks, yMax } = useMemo(() => {
    const rawMax = hasData ? Math.max(...values) : 1
    const tk = niceTicks(rawMax || 1)
    const ymax = tk[tk.length - 1] || 1
    const n = points.length
    const xAt = (i: number) => (n <= 1 ? M.left + PLOT_W / 2 : M.left + (PLOT_W * i) / (n - 1))
    const yAt = (v: number) => M.top + PLOT_H - (v / ymax) * PLOT_H
    const pos = points.map((p, i) => ({ i, x: xAt(i), y: p.y == null ? null : yAt(p.y), value: p.y, label: p.x }))
    return { positions: pos, ticks: tk, yMax: ymax }
  }, [points, hasData, values])

  // Build the line path, breaking into a fresh sub-path across null gaps.
  const path = useMemo(() => {
    let d = ''
    let pen = false
    for (const p of positions) {
      if (p.y == null) { pen = false; continue }
      d += `${pen ? 'L' : 'M'}${p.x.toFixed(1)} ${p.y.toFixed(1)} `
      pen = true
    }
    return d.trim()
  }, [positions])

  const lastDrawn = [...positions].reverse().find((p) => p.y != null)

  // Show at most ~6 x-axis labels so they never collide.
  const labelEvery = Math.max(1, Math.ceil(points.length / 6))

  function onMove(e: React.PointerEvent<SVGSVGElement>) {
    const svg = svgRef.current
    if (!svg || points.length === 0) return
    const rect = svg.getBoundingClientRect()
    const vx = ((e.clientX - rect.left) / rect.width) * W
    let nearest = 0
    let best = Infinity
    for (const p of positions) {
      const dist = Math.abs(p.x - vx)
      if (dist < best) { best = dist; nearest = p.i }
    }
    setHover(nearest)
  }

  const hoverPos = hover != null ? positions[hover] : null

  return (
    <div className="lem-viz">
      <style>{`
        .lem-viz .viz-grid { stroke: ${VIZ.gridline}; stroke-width: 1; }
        .lem-viz .viz-baseline { stroke: ${VIZ.baseline}; stroke-width: 1; }
        .lem-viz .viz-line { stroke: ${VIZ.series1}; stroke-width: 2; fill: none; stroke-linejoin: round; stroke-linecap: round; }
        .lem-viz .viz-area { fill: ${VIZ.series1}; opacity: 0.10; }
        .lem-viz .viz-dot { fill: ${VIZ.series1}; stroke: ${VIZ.surface}; stroke-width: 2; }
        .lem-viz .viz-tick { fill: ${VIZ.muted}; font-size: 11px; font-variant-numeric: tabular-nums; }
        .lem-viz .viz-endlabel { fill: ${VIZ.textSecondary}; font-size: 12px; font-weight: 600; }
        .lem-viz text { font-family: system-ui, -apple-system, 'Segoe UI', sans-serif; }
      `}</style>
      <div className="mb-1">
        <h3 className="text-sm font-semibold text-gray-700">{title}</h3>
        {subtitle && <p className="text-xs text-gray-500">{subtitle}</p>}
      </div>

      {!hasData ? (
        <p className="text-xs text-gray-400 py-8 text-center">{emptyMessage ?? 'No data yet.'}</p>
      ) : (
        <>
          <svg
            ref={svgRef}
            viewBox={`0 0 ${W} ${H}`}
            width="100%"
            role="img"
            aria-label={`${title} line chart`}
            style={{ display: 'block', touchAction: 'none' }}
            onPointerMove={onMove}
            onPointerLeave={() => setHover(null)}
          >
            {/* Horizontal gridlines + y ticks */}
            {ticks.map((t) => {
              const y = M.top + PLOT_H - (t / yMax) * PLOT_H
              return (
                <g key={t}>
                  <line className="viz-grid" x1={M.left} y1={y} x2={M.left + PLOT_W} y2={y} />
                  <text className="viz-tick" x={M.left - 8} y={y + 4} textAnchor="end">{fmt(t)}</text>
                </g>
              )
            })}
            {/* Baseline */}
            <line className="viz-baseline" x1={M.left} y1={M.top + PLOT_H} x2={M.left + PLOT_W} y2={M.top + PLOT_H} />

            {/* Area wash under the line (single continuous run only, for a calm fill) */}
            {lastDrawn && path && (
              <path
                className="viz-area"
                d={`${path} L${lastDrawn.x.toFixed(1)} ${(M.top + PLOT_H).toFixed(1)} L${positions.find((p) => p.y != null)!.x.toFixed(1)} ${(M.top + PLOT_H).toFixed(1)} Z`}
              />
            )}
            {/* The line */}
            <path className="viz-line" d={path} />

            {/* x-axis labels (subset) */}
            {positions.map((p) =>
              p.i % labelEvery === 0 || p.i === positions.length - 1 ? (
                <text key={p.i} className="viz-tick" x={p.x} y={H - 12} textAnchor="middle">{p.label}</text>
              ) : null,
            )}

            {/* End marker + direct label */}
            {lastDrawn && (
              <>
                <circle className="viz-dot" cx={lastDrawn.x} cy={lastDrawn.y!} r={4} />
                <text className="viz-endlabel" x={Math.min(lastDrawn.x + 8, W - 4)} y={lastDrawn.y! + 4} textAnchor={lastDrawn.x + 8 > W - 40 ? 'end' : 'start'}>
                  {fmt(lastDrawn.value!)}
                </text>
              </>
            )}

            {/* Hover crosshair + tooltip */}
            {hoverPos && (
              <g pointerEvents="none">
                <line className="viz-grid" x1={hoverPos.x} y1={M.top} x2={hoverPos.x} y2={M.top + PLOT_H} />
                {hoverPos.y != null && <circle className="viz-dot" cx={hoverPos.x} cy={hoverPos.y} r={4} />}
                {(() => {
                  const boxW = 132
                  const bx = Math.min(Math.max(hoverPos.x - boxW / 2, M.left), M.left + PLOT_W - boxW)
                  const by = M.top + 4
                  const valueText = hoverPos.value == null ? 'No data' : fmt(hoverPos.value)
                  return (
                    <g transform={`translate(${bx}, ${by})`}>
                      <rect width={boxW} height={40} rx={4} fill={VIZ.surface} stroke="rgba(11,11,11,0.10)" />
                      <text x={8} y={17} style={{ fill: VIZ.textPrimary, fontSize: 13, fontWeight: 700 }}>{valueText}</text>
                      <text x={8} y={32} style={{ fill: VIZ.textSecondary, fontSize: 11 }}>{`${valueLabel} · ${hoverPos.label}`}</text>
                    </g>
                  )
                })()}
              </g>
            )}
          </svg>

          {/* Table view — every value reachable without hover (WCAG twin) */}
          <details className="mt-2">
            <summary className="text-xs text-gray-500 cursor-pointer hover:text-gray-700">Show data table</summary>
            <div className="mt-2 max-h-48 overflow-y-auto">
              <table className="w-full text-xs text-left tabular-nums">
                <thead>
                  <tr className="text-gray-500 border-b border-gray-200">
                    <th className="py-1 pr-3 font-medium">Date</th>
                    <th className="py-1 font-medium text-right">{valueLabel}</th>
                  </tr>
                </thead>
                <tbody>
                  {points.map((p, i) => (
                    <tr key={i} className="border-b border-gray-100 last:border-0 text-gray-700">
                      <td className="py-1 pr-3">{p.x}</td>
                      <td className="py-1 text-right">{p.y == null ? '—' : fmt(p.y)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </details>
        </>
      )}
    </div>
  )
}
