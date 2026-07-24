import { VIZ, formatRate } from './palette'

export interface RankEntry {
  key: string
  score: number
  avg_engagement: number
  samples: number
  metric: string
}

interface LeaderboardProps {
  title: string
  subtitle?: string
  entries: RankEntry[]
  emptyMessage?: string
  /** Prettify a raw attribute key (e.g. `bold_claim` → `Bold claim`). */
  humanizeKey?: (k: string) => string
}

function defaultHumanize(k: string): string {
  const s = String(k).replace(/[_-]+/g, ' ').trim()
  return s.charAt(0).toUpperCase() + s.slice(1)
}

// A ranked list of single-hue horizontal bars: magnitude comparison, so one hue (never a
// value-ramp on nominal categories). Bars scale to the top entry; each carries a direct value
// label, so no axis or gridlines are needed.
export default function Leaderboard({ title, subtitle, entries, emptyMessage, humanizeKey }: LeaderboardProps) {
  const humanize = humanizeKey ?? defaultHumanize
  const maxAvg = entries.reduce((m, e) => Math.max(m, e.avg_engagement), 0) || 1
  const isRate = entries[0]?.metric === 'engagement_rate'
  const fmt = (v: number) => (isRate ? formatRate(v) : v.toLocaleString())

  return (
    <div>
      <div className="mb-2">
        <h3 className="text-sm font-semibold text-gray-700">{title}</h3>
        <p className="text-xs text-gray-500">
          {subtitle ?? (isRate ? 'Avg engagement rate per impression' : 'Avg engagement per post')}
        </p>
      </div>
      {entries.length === 0 ? (
        <p className="text-xs text-gray-400 py-4 text-center">{emptyMessage ?? 'Not enough data yet.'}</p>
      ) : (
        <ul className="space-y-2">
          {entries.map((e) => (
            <li key={e.key} className="text-xs">
              <div className="flex items-baseline justify-between mb-0.5">
                <span className="font-medium text-gray-700 truncate pr-2">{humanize(e.key)}</span>
                <span className="text-gray-500 tabular-nums whitespace-nowrap">
                  {fmt(e.avg_engagement)} · {e.samples} post{e.samples === 1 ? '' : 's'}
                </span>
              </div>
              <div className="h-2 w-full rounded-full" style={{ backgroundColor: '#f0efec' }}>
                <div
                  className="h-2 rounded-full"
                  style={{
                    width: `${Math.max(4, (e.avg_engagement / maxAvg) * 100)}%`,
                    backgroundColor: VIZ.series1,
                  }}
                  aria-hidden
                />
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
