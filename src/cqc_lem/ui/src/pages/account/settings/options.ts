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

export const INACTIVATE_OPTIONS = [
  { label: '30 days', value: 30 },
  { label: '60 days', value: 60 },
  { label: '90 days (default)', value: 90 },
  { label: '120 days', value: 120 },
  { label: '365 days', value: 365 },
  { label: 'Never', value: null },
]
