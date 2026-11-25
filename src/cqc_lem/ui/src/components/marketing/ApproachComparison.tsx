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
// Below `sm` the same rows render as one block per capability instead: four columns at 390px only
// fit by scrolling sideways, and the header row was cut mid-word with no cue that more existed.
//
// All seven rows stay (owner decision 2026-11-24, the density override in brand/lem/DESIGN.md): a
// table is already the compact form for them. The voice and approval rows concede to the
// alternatives.

type Cell = boolean | string

const COLUMNS = ['Doing it by hand', 'A general AI writer', 'LEM'] as const

const ROWS: { capability: string; cells: [Cell, Cell, Cell] }[] = [
  { capability: 'Writes in your own voice', cells: [true, 'Generic unless you prompt it every time', true] },
  { capability: 'Plans a month across a buyer journey', cells: [false, false, true] },
  { capability: 'Publishes at your golden hours', cells: ['If you are free at the time', false, true] },
  { capability: 'Suggests comments and replies you send yourself', cells: ['You write each one', 'Only for posts you paste in', true] },
  { capability: 'Suggests DM follow-ups', cells: ['If you remember', false, true] },
  { capability: 'Approval before a post is published', cells: [true, true, true] },
  { capability: 'Shows how each published post performed', cells: [false, false, true] },
]

function CellValue({ value }: { value: Cell }) {
  if (typeof value === 'string') return <span className="text-sm text-fg-muted">{value}</span>
  return <IncludedMark included={value} />
}

export default function ApproachComparison() {
  return (
    <Section id="comparison" analyticsName="comparison" tone="tinted">
      <SectionHeading
        title="LEM, doing it yourself, or a general AI writer"
        lede="Doing it by hand works — it just costs an hour a day. A general writer solves the smaller half of the problem."
      />
      <ul className="border-t border-hairline sm:hidden">
        {ROWS.map((row) => (
          <li key={row.capability} className="border-b border-hairline py-4">
            <p className="font-semibold text-fg">{row.capability}</p>
            <dl className="mt-2 space-y-2">
              {row.cells.map((cell, index) => (
                <div key={COLUMNS[index]} className="flex items-start justify-between gap-4">
                  <dt className="text-fg-muted">{COLUMNS[index]}</dt>
                  <dd className="text-right">
                    <CellValue value={cell} />
                  </dd>
                </div>
              ))}
            </dl>
          </li>
        ))}
      </ul>
      <TableScroll
        label="How LEM compares with doing it by hand and with a general AI writer"
        className="hidden sm:block"
      >
        <table className="w-full border-collapse bg-raised text-left">
          <caption className="sr-only">
            Capabilities compared across three approaches: doing it by hand, a general AI writer, and
            LEM.
          </caption>
          <thead>
            <tr className="border-b border-hairline">
              <th scope="col" className="p-4 text-sm font-semibold text-fg">
                Capability
              </th>
              {COLUMNS.map((column) => (
                <th
                  key={column}
                  scope="col"
                  // LEM's column is the one the table argues for, so its header carries the accent.
                  className={`p-4 text-sm font-semibold ${column === 'LEM' ? 'text-accent' : 'text-fg'}`}
                >
                  {column}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {ROWS.map((row) => (
              <tr key={row.capability} className="border-b border-hairline">
                <th scope="row" className="p-4 text-sm font-medium text-fg">
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
