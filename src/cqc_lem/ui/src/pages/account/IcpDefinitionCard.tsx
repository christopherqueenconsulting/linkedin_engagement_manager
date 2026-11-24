import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import api from '../../api/client'
import { useAuth } from '../../contexts/useAuth'
import type { GetDetail } from '../../api/types'
import { useRegisterSaveSection, sectionSaveCallbacks } from './settingsSave'
import CsvInput from './settings/CsvInput'
import { inputClass } from './settings/Field'

type IcpResponse = GetDetail<'/api/user/icp-definition'>

type Segment = {
  id: string
  name?: string
  role_titles?: string[]
  industries?: string[]
  geographies?: string[]
  company_size_min?: number | null
  company_size_max?: number | null
}
type Exclusions = {
  clients?: string[]
  competitors?: string[]
  do_not_contact?: string[]
  seniority?: string[]
}
type Definition = { segments: Segment[]; exclusions?: Exclusions }

const EXCLUSION_FIELDS: { key: keyof Exclusions; label: string; placeholder: string }[] = [
  { key: 'clients', label: 'Existing clients (company names)', placeholder: 'Acme, Globex' },
  { key: 'competitors', label: 'Competitors (company names)', placeholder: 'Initech' },
  { key: 'do_not_contact', label: 'Never contact (profile handles)', placeholder: 'jane-doe' },
  { key: 'seniority', label: 'Skip titles containing', placeholder: 'intern, student' },
]

const newSegmentId = (segments: Segment[]) => {
  let n = segments.length + 1
  while (segments.some((s) => s.id === `segment-${n}`)) n += 1
  return `segment-${n}`
}

const toNumber = (raw: string): number | null => (raw.trim() === '' ? null : Number(raw))

// Empty strings and lists are noise the API would only store back; leaving them out keeps the
// saved JSON to what the user actually filled in.
const tidy = (def: Definition): Definition => ({
  segments: def.segments.map((s) => ({
    id: s.id,
    name: s.name ?? '',
    role_titles: s.role_titles ?? [],
    industries: s.industries ?? [],
    geographies: s.geographies ?? [],
    ...(s.company_size_min != null ? { company_size_min: s.company_size_min } : {}),
    ...(s.company_size_max != null ? { company_size_max: s.company_size_max } : {}),
  })),
  exclusions: def.exclusions ?? {},
})

/**
 * Ideal-customer-profile editor (issue #2655). Defines who connection scans should favour. The
 * scoring weights are fixed in code and not editable here; this card only says WHO fits. Off, the
 * scan keeps its previous scoring, so turning the switch off is the whole rollback.
 */
export default function IcpDefinitionCard() {
  const { sessionToken } = useAuth()
  const queryClient = useQueryClient()
  // null means "show what the API returned", so a refetch can never discard rows being typed.
  const [draft, setDraft] = useState<Definition | null>(null)
  const [enabled, setEnabled] = useState<boolean | null>(null)
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)

  const { data } = useQuery({
    queryKey: ['icp-definition', sessionToken],
    queryFn: () =>
      api
        .get(`/user/icp-definition?session_token=${encodeURIComponent(sessionToken!)}`)
        .then((r) => r.data.detail as IcpResponse),
    enabled: !!sessionToken,
    staleTime: 60 * 1000,
  })

  const saved = (data?.icp_definition as Definition | null | undefined) ?? null
  const def: Definition = draft ?? saved ?? { segments: [], exclusions: {} }
  const flag = enabled ?? !!data?.icp_builder_enabled
  const suggestion = (data?.draft as Definition | null | undefined) ?? null

  const edit = (fn: (d: Definition) => Definition) => setDraft((d) => fn(d ?? def))
  const patchSegment = (idx: number, patch: Partial<Segment>) =>
    edit((d) => ({ ...d, segments: d.segments.map((s, i) => (i === idx ? { ...s, ...patch } : s)) }))
  const patchExclusions = (patch: Exclusions) =>
    edit((d) => ({ ...d, exclusions: { ...(d.exclusions ?? {}), ...patch } }))

  const saveMutation = useMutation({
    mutationFn: () =>
      api.put('/user/icp-definition', {
        session_token: sessionToken,
        icp_builder_enabled: flag,
        // `{}` clears the stored definition; an empty segment list would be refused by the API.
        icp_definition: def.segments.length ? tidy(def) : {},
      }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['icp-definition'] })
      setDraft(null)
      setEnabled(null)
      setMsg({ ok: true, text: 'Ideal customer profile saved.' })
      setTimeout(() => setMsg(null), 3000)
    },
    onError: () => {
      setMsg({ ok: false, text: 'Could not save — check each segment has an id and try again.' })
      setTimeout(() => setMsg(null), 5000)
    },
  })

  const save = async (): Promise<boolean> => {
    const analytics = sectionSaveCallbacks('icp-definition')
    try {
      await saveMutation.mutateAsync()
    } catch {
      analytics.onError()
      return false
    }
    analytics.onSuccess()
    return true
  }

  const isDirty = draft !== null || enabled !== null
  useRegisterSaveSection('icp-definition', 'Ideal Customer Profile', isDirty, save)

  if (!data) return null

  return (
    <div className="bg-raised rounded-panel shadow-panel border border-hairline p-6 space-y-4"
         data-testid="icp-definition">
      <h2 className="text-base font-semibold text-fg">Ideal Customer Profile</h2>
      <p className="text-xs text-fg-muted">
        Describe who you want to connect with. When this is on, connection scans score people
        against these segments instead of the default fit score. People LEM has no profile facts for
        score neutrally and are held for a profile read, never filtered out blind. Scoring weights
        are fixed.
      </p>

      <label className="flex items-start gap-2 text-sm text-fg">
        <input type="checkbox" checked={flag} data-testid="icp-enabled"
          aria-label="Use my ideal customer profile when scoring connections"
          onChange={(e) => setEnabled(e.target.checked)} className="mt-0.5" />
        <span>
          Use my ideal customer profile when scoring connections
          <span className="block text-xs text-fg-muted">
            Off by default. Turn it off at any time to go back to the default scoring.
          </span>
        </span>
      </label>

      {!saved && !draft && suggestion && (
        <div className="rounded-control border border-hairline bg-page p-3 space-y-2"
             data-testid="icp-draft-offer">
          <p className="text-xs text-fg">
            Draft from your roster and best leads ({data.draft_sample_size}{' '}
            {data.draft_sample_size === 1 ? 'person' : 'people'} with scraped profiles):{' '}
            {(suggestion.segments[0]?.role_titles ?? []).join(', ') || 'no common titles'}
            {(suggestion.segments[0]?.industries ?? []).length > 0
              && ` · ${(suggestion.segments[0]?.industries ?? []).join(', ')}`}
          </p>
          <button type="button" onClick={() => setDraft(suggestion)}
            className="text-xs text-accent font-medium">Use this draft</button>
        </div>
      )}

      <div className="space-y-4">
        {def.segments.map((s, idx) => (
          <fieldset key={s.id} className="border border-hairline rounded-control p-3 space-y-2"
                    data-testid={`icp-segment-${s.id}`}>
            <div className="flex items-center gap-2">
              <input type="text" value={s.name ?? ''} maxLength={120} aria-label="Segment name"
                onChange={(e) => patchSegment(idx, { name: e.target.value })}
                placeholder="Segment name (e.g. SaaS founders)" className={inputClass} />
              <button type="button" aria-label={`Remove segment ${s.name || s.id}`}
                onClick={() => edit((d) => ({ ...d, segments: d.segments.filter((_, i) => i !== idx) }))}
                className="text-danger">×</button>
            </div>
            <label className="block text-xs text-fg-muted">Job titles
              <CsvInput value={s.role_titles} aria-label="Job titles"
                onChange={(v) => patchSegment(idx, { role_titles: v })}
                placeholder="founder, ceo, head of growth" />
            </label>
            <label className="block text-xs text-fg-muted">Industries
              <CsvInput value={s.industries} aria-label="Industries"
                onChange={(v) => patchSegment(idx, { industries: v })}
                placeholder="software, fintech" />
            </label>
            <label className="block text-xs text-fg-muted">Locations
              <CsvInput value={s.geographies} aria-label="Locations"
                onChange={(v) => patchSegment(idx, { geographies: v })}
                placeholder="united states, canada" />
            </label>
            <div className="flex gap-2 text-xs text-fg-muted">
              <label>Company size min
                <input type="number" min={0} value={s.company_size_min ?? ''}
                  aria-label="Company size min"
                  onChange={(e) => patchSegment(idx, { company_size_min: toNumber(e.target.value) })}
                  className={inputClass} />
              </label>
              <label>Company size max
                <input type="number" min={0} value={s.company_size_max ?? ''}
                  aria-label="Company size max"
                  onChange={(e) => patchSegment(idx, { company_size_max: toNumber(e.target.value) })}
                  className={inputClass} />
              </label>
            </div>
          </fieldset>
        ))}
      </div>

      <button type="button" className="text-xs text-accent font-medium"
        onClick={() => edit((d) => ({
          ...d, segments: [...d.segments, { id: newSegmentId(d.segments), name: '' }],
        }))}>+ Add segment</button>

      {def.segments.length > 0 && (
        <div className="border-t border-hairline pt-3 space-y-2">
          <p className="text-xs font-medium text-fg">Never file these people</p>
          {EXCLUSION_FIELDS.map((f) => (
            <label key={f.key} className="block text-xs text-fg-muted">{f.label}
              <CsvInput value={def.exclusions?.[f.key]} aria-label={f.label}
                onChange={(v) => patchExclusions({ [f.key]: v })} placeholder={f.placeholder} />
            </label>
          ))}
        </div>
      )}

      {msg && <p className={`text-sm font-medium ${msg.ok ? 'text-success' : 'text-danger'}`}>{msg.text}</p>}
      <button type="button" onClick={() => { void save() }} disabled={saveMutation.isPending}
        className="w-full bg-accent text-white py-2 rounded-panel text-sm font-semibold hover:bg-accent/90 disabled:opacity-50 transition-colors">
        {saveMutation.isPending ? 'Saving…' : 'Save Ideal Customer Profile'}
      </button>
    </div>
  )
}
