import { useState, type ReactNode } from 'react'
import { SETTING_BY_KEY } from './registry'
import { useConflicts } from './ConflictsContext'
import type { Finding, Severity } from './conflicts'

const TONE: Record<Severity, string> = {
  inform: 'bg-gray-50 border-gray-200 text-gray-600',
  warn: 'bg-amber-50 border-amber-200 text-amber-800',
  block: 'bg-red-50 border-red-200 text-red-700',
}

const ICON: Record<Severity, string> = { inform: 'ⓘ', warn: '⚠', block: '✕' }

export function ConflictNotice({ finding }: { finding: Finding }) {
  const { applyFix } = useConflicts()
  return (
    <div
      data-testid={`conflict-${finding.id}`}
      className={`mt-1.5 rounded-lg border px-3 py-2 text-xs ${TONE[finding.severity]}`}
    >
      <span className="mr-1.5 font-semibold">{ICON[finding.severity]}</span>
      {finding.message}
      {finding.fix && (
        <button
          type="button"
          onClick={() => applyFix(finding)}
          className="ml-2 font-semibold underline underline-offset-2 hover:no-underline"
        >
          {finding.fix.label}
        </button>
      )}
    </div>
  )
}

/** Every finding anchored to a setting, rendered next to it. Warnings never block the save. */
export function ConflictNotices({ anchor }: { anchor: string }) {
  const { findings } = useConflicts()
  const mine = findings.filter((f) => f.anchor === anchor)
  if (mine.length === 0) return null
  return <>{mine.map((f) => <ConflictNotice key={f.id} finding={f} />)}</>
}

/**
 * One control rendered with the microcopy contract from the registry: what it does (always), why it
 * matters + what we recommend (behind a `?`, so dense sections stay scannable), and any conflict
 * findings anchored to it.
 */
export function Field({
  settingKey, children, className = '',
}: { settingKey: string; children: ReactNode; className?: string }) {
  const [open, setOpen] = useState(false)
  const d = SETTING_BY_KEY[settingKey]
  if (!d) return <div className={className}>{children}</div>
  return (
    <div className={className} data-testid={`field-${settingKey}`}>
      <div className="flex items-start justify-between gap-2 mb-1">
        <label className="block text-sm font-medium text-gray-700">{d.label}</label>
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          aria-label={`Why ${d.label} matters`}
          className="shrink-0 w-5 h-5 rounded-full border border-gray-300 text-[11px] font-bold text-gray-500 hover:bg-gray-100"
        >
          ?
        </button>
      </div>
      {children}
      <p className="text-xs text-gray-500 mt-1">{d.what}</p>
      {open && (
        <div className="mt-1.5 rounded-lg bg-gray-50 border border-gray-100 px-3 py-2 text-xs text-gray-600 space-y-1">
          <p><span className="font-semibold text-gray-700">Why it matters</span> — {d.why}</p>
          <p><span className="font-semibold text-gray-700">Recommended</span> — {d.recommended}</p>
        </div>
      )}
      <ConflictNotices anchor={settingKey} />
    </div>
  )
}

/** Progressive disclosure — each section keeps ≤6 primary controls and parks the rest in here. */
export function Advanced({ children, label = 'Advanced' }: { children: ReactNode; label?: string }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="border-t border-gray-100 pt-4">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="text-sm font-semibold text-gray-600 hover:text-gray-800"
      >
        {open ? '▾' : '▸'} {label}
      </button>
      {open && <div className="mt-4 space-y-4">{children}</div>}
    </div>
  )
}

export function SectionCard({
  title, blurb, children,
}: { title: string; blurb?: string; children: ReactNode }) {
  return (
    <div className="bg-white rounded-lg shadow-sm border border-gray-200 p-6 space-y-5">
      <div>
        <h2 className="text-base font-semibold text-gray-700">{title}</h2>
        {blurb && <p className="text-xs text-gray-500 mt-0.5">{blurb}</p>}
      </div>
      {children}
    </div>
  )
}

export const inputClass = 'w-full border border-gray-300 rounded-lg px-3 py-2 text-sm'
