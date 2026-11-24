// Client-side span finder for the Daily review. The server says WHICH flags an item carries
// (`review_flags.py`); it does not say where, so each flag here has a pattern that mirrors its rule
// closely enough to underline the text that tripped it. A flag with no match highlights nothing —
// the badge still shows it. Only flagged spans are marked, so the skim stays on what needs reading.

export interface ReviewItem {
  lane: string
  id: number
  age_hours: number | null
  summary: string
  draft_text: string
  hold_reason: string | null
  risk_flags: string[]
  confidence: number | null
  allowed_actions: string[]
  edit_href: string
  // MCP W2: a DM or connection request the user's coding agent queued. Badged, and never part
  // of a bulk approve: it is approved one row at a time, after a human has read it.
  agent_created?: boolean
}

export interface ReviewSummary {
  total: number
  stale_48h: number
  oldest_age_hours: number | null
  by_lane: Record<string, number>
}

export const FLAG_LABELS: Record<string, string> = {
  number: 'Number',
  first_person_claim: 'First-person claim',
  named_entity: 'Named person/company',
  link: 'Link',
  meeting_ask: 'Meeting ask',
  gate_near_miss: 'Gate near-miss',
  near_duplicate: 'Near-duplicate',
  stale_slot: 'Stale slot',
}

const FLAG_PATTERNS: Record<string, RegExp> = {
  number: /\d[\d,.]*\s*(?:%|percent\b|x\b|k\b|m\b|million\b|billion\b)|\$\s?\d|\b\d{2,}\b/gi,
  first_person_claim:
    /\b(?:i|we)\s+(?:have\s+|'ve\s+|just\s+|recently\s+)?(?:built|launched|grew|grown|increased|reduced|saved|doubled|tripled|led|ran|closed|raised|hired|shipped|scaled|helped|worked|managed|founded|sold|generated|won)\b|\bmy (?:team|company|clients?|customers?)\b|\bwhen i was\b/gi,
  named_entity: /\b[A-Z][a-z]+\s+[A-Z][a-z]+\b/g,
  link: /https?:\/\/\S+|\bwww\.\S+/gi,
  meeting_ask:
    /\b(?:book|schedule|grab|hop on|jump on|set up)\b[^.!?\n]{0,30}\b(?:call|meeting|chat|demo|time|15|20|30)\b|\bcalendly\b|\blet'?s (?:chat|talk|connect|meet)\b|\bquick (?:call|chat)\b/gi,
}

export interface Segment {
  text: string
  flagged: boolean
}

/** Split `text` into plain and flagged segments for the flags the server reported. */
export function highlightSegments(text: string, flags: string[]): Segment[] {
  const spans: [number, number][] = []
  for (const flag of flags) {
    const rx = FLAG_PATTERNS[flag]
    if (!rx) continue
    for (const m of text.matchAll(new RegExp(rx.source, rx.flags))) {
      if (m[0].length) spans.push([m.index!, m.index! + m[0].length])
    }
  }
  spans.sort((a, b) => a[0] - b[0])
  const merged: [number, number][] = []
  for (const s of spans) {
    const last = merged[merged.length - 1]
    if (last && s[0] <= last[1]) last[1] = Math.max(last[1], s[1])
    else merged.push([s[0], s[1]])
  }
  const out: Segment[] = []
  let at = 0
  for (const [a, b] of merged) {
    if (a > at) out.push({ text: text.slice(at, a), flagged: false })
    out.push({ text: text.slice(a, b), flagged: true })
    at = b
  }
  if (at < text.length) out.push({ text: text.slice(at), flagged: false })
  return out
}

export type ReviewGroup = 'clean' | 'check' | 'held'

/** Held beats flagged beats clean: a hold reason is the strongest "read this first" signal. */
export function groupOf(item: ReviewItem): ReviewGroup {
  if (item.hold_reason) return 'held'
  return item.risk_flags.length ? 'check' : 'clean'
}

/** What "Approve all with no flags" may sweep in: unflagged, unheld, scored, approvable, and
 * not agent-created (MCP W2 — those are approved one at a time). */
export function isSweepable(item: ReviewItem): boolean {
  return (
    !item.agent_created &&
    item.risk_flags.length === 0 &&
    !item.hold_reason &&
    item.confidence !== null &&
    item.allowed_actions.includes('approve')
  )
}

// Cancel-until-sent is real only where an approved row waits for a scheduled slot. Connection
// requests, funnel stages and catch-up touches dispatch on the next scan, so there is nothing to undo.
export const UNDO_LANES = ['scheduled_dms']

/** The targets a bulk action may take. An approve of more than one item never includes an
 * agent-created row (MCP W2); the server refuses it too. Single-item approve and every cancel
 * are unchanged. */
export function bulkTargets<T extends Pick<ReviewItem, 'agent_created'>>(
  targets: T[],
  action: 'approve' | 'cancel',
): T[] {
  if (action !== 'approve' || targets.length < 2) return targets
  return targets.filter((t) => !t.agent_created)
}

export function ageTone(hours: number | null): 'red' | 'amber' | 'none' {
  if (hours === null) return 'none'
  if (hours > 48) return 'red'
  return hours > 24 ? 'amber' : 'none'
}
