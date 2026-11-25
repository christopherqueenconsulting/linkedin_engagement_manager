// Pure helpers for the "Changes your agent asked for" card (#2803), kept out of the component file so
// it exports only components (react-refresh).

export const SETTINGS_PROPOSALS_KEY = 'settings-proposals'

export type Value = string | number | boolean | null

export interface ProposalField {
  field: string
  label: string
  before: Value
  requested: Value
  loosens: boolean
  warning: string
}

export interface Proposal {
  id: number
  reason: string | null
  fields: ProposalField[]
  created_at: string | null
  expires_at: string | null
}

export interface Decision {
  status: string
  applied: string[]
  stale: string[]
  declined: string[]
  failed: string[]
}

const MODE_TEXT: Record<string, string> = {
  pre_review: 'Review first',
  auto_approve: 'Send without review',
}

/** A value as a person reads it: a switch is On or Off, a mode says what it does, a NULL is Default. */
export function formatValue(value: Value): string {
  if (value === null || value === undefined) return 'Default'
  if (typeof value === 'boolean') return value ? 'On' : 'Off'
  if (typeof value === 'string') return MODE_TEXT[value] ?? value
  return String(value)
}
