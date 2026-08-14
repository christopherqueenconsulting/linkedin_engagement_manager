import TableScroll from '../TableScroll'
import { IncludedMark } from './Icon'
import Section, { SectionHeading } from './Section'

// The objection-handling section (issue #1300). It compares APPROACHES — doing it by hand, pointing
// a general-purpose AI writer at LinkedIn, or running LEM — and deliberately names no competitor:
// a named-rival table is a legal and maintenance liability, and the real objection a visitor has is
// "why not just do it myself / just use ChatGPT" anyway.
//
// The `<table>` is wrapped in TableScroll because a data table has no way to say "I need more room
// than this phone" (issue #894), and `mobileViewport.test.ts` greps this file for that import.

type Cell = boolean | string

const COLUMNS = ['Doing it by hand', 'A general AI writer', 'LEM'] as const

const ROWS: { capability: string; cells: [Cell, Cell, Cell] }[] = [
  { capability: 'Writes in your own voice', cells: [true, 'Generic unless you prompt it every time', true] },
  { capability: 'Plans a month across a buyer journey', cells: [false, false, true] },
  { capability: 'Publishes at your golden hours', cells: ['If you are free at the time', false, true] },
  { capability: 'Comments and replies for you daily', cells: ['An hour a day', false, true] },
  { capability: 'Follows up on DMs on a schedule', cells: ['If you remember', false, true] },
  { capability: 'Per-day caps and human pacing', cells: ['Whatever you feel like', false, true] },
  { capability: 'Stops on a rate limit automatically', cells: [false, false, true] },
  { capability: 'Approval before anything is published', cells: [true, true, true] },
  { capability: 'Measures what each comment earned', cells: [false, false, true] },
]

function CellValue({ value }: { value: Cell }) {
  if (typeof value === 'string') return <span className="text-sm text-ink-600">{value}</span>
  return <IncludedMark included={value} />
}

export default function ApproachComparison() {
  return (
    <Section id="comparison" analyticsName="comparison" tone="tinted">
      <SectionHeading
        eyebrow="Honest comparison"
        title="LEM, doing it yourself, or a general AI writer"
        lede="Doing it by hand works — it just costs an hour a day. A general writer solves the smaller half of the problem."
      />
      <TableScroll label="How LEM compares with doing it by hand and with a general AI writer">
        <table className="w-full border-collapse bg-white text-left">
          <caption className="sr-only">
            Capabilities compared across three approaches: doing it by hand, a general AI writer, and
            LEM.
          </caption>
          <thead>
            <tr className="border-b border-line-300">
              <th scope="col" className="p-4 text-sm font-semibold text-ink-900">
                Capability
              </th>
              {COLUMNS.map((column) => (
                <th key={column} scope="col" className="p-4 text-sm font-semibold text-ink-900">
                  {column}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {ROWS.map((row) => (
              <tr key={row.capability} className="border-b border-line-200">
                <th scope="row" className="p-4 text-sm font-medium text-ink-900">
                  {row.capability}
                </th>
                {row.cells.map((cell, index) => (
                  <td key={COLUMNS[index]} className="p-4">
                    <CellValue value={cell} />
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </TableScroll>
    </Section>
  )
}
