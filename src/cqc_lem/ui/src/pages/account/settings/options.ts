// BCP-47 tags kept in lockstep with users.content_language VARCHAR(16). Empty = inherit the
// Login Location locale. Drives the language of premium (Veo) video audio — issue #548.
export const CONTENT_LANGUAGE_OPTIONS = [
  { label: 'English (US)', value: 'en-US' },
  { label: 'English (UK)', value: 'en-GB' },
  { label: 'Spanish', value: 'es-ES' },
  { label: 'Spanish (Latin America)', value: 'es-419' },
  { label: 'French', value: 'fr-FR' },
  { label: 'German', value: 'de-DE' },
  { label: 'Portuguese (Brazil)', value: 'pt-BR' },
  { label: 'Italian', value: 'it-IT' },
  { label: 'Dutch', value: 'nl-NL' },
  { label: 'Japanese', value: 'ja-JP' },
  { label: 'Korean', value: 'ko-KR' },
  { label: 'Chinese (Simplified)', value: 'zh-CN' },
  { label: 'Hindi', value: 'hi-IN' },
  { label: 'Arabic', value: 'ar-SA' },
]

// Publishing cadence (issue #621). Each step adds one weekday to the fixed day-type calendar in the
// order the planner fills it: Tue, Thu, Wed, Mon, Fri, Sun, Sat. 2-4/week is the 2026 sweet spot;
// 5+ is offered but flagged, because daily posting costs ~26% of each post's average reach.
export const CADENCE_OPTIONS = [
  { label: '2 a week (Tue, Thu)', value: 2 },
  { label: '3 a week (Tue, Wed, Thu) — recommended', value: 3 },
  { label: '4 a week (Mon-Thu)', value: 4 },
  { label: '5 a week (Mon-Fri)', value: 5 },
  { label: '6 a week (no Saturday)', value: 6 },
  { label: '7 a week (daily)', value: 7 },
]

export const INACTIVATE_OPTIONS = [
  { label: '30 days', value: 30 },
  { label: '60 days', value: 60 },
  { label: '90 days (default)', value: 90 },
  { label: '120 days', value: 120 },
  { label: '365 days', value: 365 },
  { label: 'Never', value: null },
]
