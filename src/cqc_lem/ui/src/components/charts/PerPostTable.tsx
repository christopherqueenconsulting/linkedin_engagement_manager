import { useState } from 'react'
import { compactNumber, formatRate, shortDate, titleCase } from './palette'

export interface PerPost {
  post_id: number
  scheduled_time: string | null
  format: string | null
  archetype: string | null
  hook_style: string | null
  topic: string | null
  buyer_stage: string | null
  reactions: number
  comments: number
  reposts: number
  saves: number
  impressions: number | null
  engagement: number
  engagement_rate: number | null
}

// Collapsed by default (#808). This drill-down is one row per measured post — over a 90-day window
// it is by far the tallest thing on the Home dashboard, and the KPI row, trends and leaderboards
// above it already answer the question most visits are asking. The row count stays in the header so
// the collapsed state still says how much is behind it.
export default function PerPostTable({ posts }: { posts: PerPost[] }) {
  const [open, setOpen] = useState(false)

  return (
    <div>
      <h3 className="text-sm font-semibold text-gray-700">
        <button
          type="button"
          onClick={() => setOpen(!open)}
          aria-expanded={open}
          className="w-full flex items-center justify-between gap-2 text-left hover:text-gray-900"
        >
          <span>
            Per-post performance{' '}
            <span className="font-normal text-gray-400">
              ({posts.length} post{posts.length === 1 ? '' : 's'})
            </span>
          </span>
          <span
            aria-hidden="true"
            className={`text-gray-400 leading-none transition-transform ${open ? 'rotate-180' : ''}`}
          >
            ▾
          </span>
        </button>
      </h3>
      {open &&
        (posts.length === 0 ? (
          <p className="text-xs text-gray-400 mt-2" data-testid="per-post-empty">
            No posts with captured stats in this window.
          </p>
        ) : (
          <div className="overflow-x-auto mt-2" data-testid="per-post-table">
            <table className="w-full text-xs text-left tabular-nums">
              <thead>
                <tr className="text-gray-500 border-b border-gray-200">
                  <th className="py-1.5 pr-3 font-medium">Date</th>
                  <th className="py-1.5 pr-3 font-medium">Format</th>
                  <th className="py-1.5 pr-3 font-medium">Hook</th>
                  <th className="py-1.5 pr-3 font-medium text-right">Impr.</th>
                  <th className="py-1.5 pr-3 font-medium text-right">Reactions</th>
                  <th className="py-1.5 pr-3 font-medium text-right">Comments</th>
                  <th className="py-1.5 pr-3 font-medium text-right">Reposts</th>
                  <th className="py-1.5 pr-3 font-medium text-right">Saves</th>
                  <th className="py-1.5 font-medium text-right">Eng. rate</th>
                </tr>
              </thead>
              <tbody>
                {posts.map((p) => (
                  <tr key={p.post_id} className="border-b border-gray-100 last:border-0 text-gray-700">
                    <td className="py-1.5 pr-3 whitespace-nowrap">
                      {p.scheduled_time ? shortDate(p.scheduled_time.slice(0, 10)) : '—'}
                    </td>
                    <td className="py-1.5 pr-3">{titleCase(p.format)}</td>
                    <td className="py-1.5 pr-3">{titleCase(p.hook_style)}</td>
                    <td className="py-1.5 pr-3 text-right">{p.impressions != null ? compactNumber(p.impressions) : '—'}</td>
                    <td className="py-1.5 pr-3 text-right">{p.reactions.toLocaleString()}</td>
                    <td className="py-1.5 pr-3 text-right">{p.comments.toLocaleString()}</td>
                    <td className="py-1.5 pr-3 text-right">{p.reposts.toLocaleString()}</td>
                    <td className="py-1.5 pr-3 text-right">{p.saves.toLocaleString()}</td>
                    <td className="py-1.5 text-right">{p.engagement_rate != null ? formatRate(p.engagement_rate) : '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ))}
    </div>
  )
}
