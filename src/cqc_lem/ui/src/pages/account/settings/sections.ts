// The 8-section Settings IA (issue #558, docs/SETTINGS_IA_RESEARCH.md §5). Sections are grouped by
// the question the user is asking, not by which table the value lands in.
export type SectionKey =
  | 'setup'
  | 'voice'
  | 'content'
  | 'targeting'
  | 'volume'
  | 'outreach'
  | 'newsletter'
  | 'billing'

export type Section = {
  key: SectionKey
  label: string
  blurb: string
}

export const SECTIONS: Section[] = [
  { key: 'setup', label: 'Setup & Connection', blurb: "If this isn't right, nothing else runs." },
  { key: 'voice', label: 'My Voice', blurb: 'How LEM sounds — in posts, comments, DMs and your newsletter.' },
  { key: 'content', label: 'Content & Publishing', blurb: 'What gets written, and what has to clear review before it ships.' },
  { key: 'targeting', label: 'Who I Engage', blurb: 'Which posts and people LEM engages with.' },
  { key: 'volume', label: 'How Much & How Often', blurb: 'Your daily activity budget, in one place.' },
  { key: 'outreach', label: 'Outreach & DMs', blurb: 'Everything that sends a person a message.' },
  { key: 'newsletter', label: 'Newsletter', blurb: 'Your recurring LinkedIn newsletter.' },
  { key: 'billing', label: 'Account & Billing', blurb: 'Plan, trial and credits.' },
]

export const SECTION_KEYS = SECTIONS.map((s) => s.key)

export const isSectionKey = (v: string | null | undefined): v is SectionKey =>
  !!v && (SECTION_KEYS as string[]).includes(v)

// Back-compat for the four tabs this hub replaces: every ?tab= deep link still lands somewhere
// sensible (the readiness banner, onboarding checklist and older emails all point at these).
export const LEGACY_TAB_TO_SECTION: Record<string, SectionKey> = {
  account: 'billing',
  linkedin: 'setup',
  content: 'content',
  automation: 'targeting',
}

export const sectionLabel = (key: SectionKey): string =>
  SECTIONS.find((s) => s.key === key)?.label ?? key

export const DEFAULT_SECTION: SectionKey = 'setup'

/** ?section= wins; a legacy ?tab= link still lands on whichever section absorbed that tab. */
export function resolveSection(section: string | null, tab: string | null): SectionKey {
  if (isSectionKey(section)) return section
  if (tab && LEGACY_TAB_TO_SECTION[tab]) return LEGACY_TAB_TO_SECTION[tab]
  return DEFAULT_SECTION
}
