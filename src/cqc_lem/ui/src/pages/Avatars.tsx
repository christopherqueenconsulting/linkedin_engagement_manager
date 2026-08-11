import { useCallback, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import api from '../api/client'
import { useAuth } from '../contexts/AuthContext'
import { importWithChunkRecovery } from '../utils/chunkReload'

const PACKAGES = [
  { key: 'starter', price: '$5',  credits: 1,  label: '1 Training',     badge: '',           savings: '' },
  { key: 'value',   price: '$10', credits: 3,  label: '3 Trainings',    badge: 'Popular',    savings: 'Save 33%' },
  { key: 'pro',     price: '$25', credits: 8,  label: '8 Trainings',    badge: 'Best Value', savings: 'Save 37%' },
  { key: 'max',     price: '$40', credits: 15, label: '15 Trainings',   badge: '',           savings: 'Save 47%' },
]

const STATUS_COLORS: Record<string, string> = {
  starting:   'bg-yellow-100 text-yellow-800',
  processing: 'bg-blue-100 text-blue-800',
  succeeded:  'bg-green-100 text-green-800',
  failed:     'bg-red-100 text-red-800',
  canceled:   'bg-gray-100 text-gray-600',
}

const APPROVAL_COLORS: Record<string, string> = {
  pending:  'bg-amber-100 text-amber-800',
  approved: 'bg-green-100 text-green-800',
  rejected: 'bg-red-100 text-red-800',
}

// Mirrors utilities/avatar/attributes.py — the user picks; nothing is ever inferred from photos.
const GENDER_OPTIONS = [
  { value: '',                  label: 'Not specified' },
  { value: 'man',               label: 'Man' },
  { value: 'woman',             label: 'Woman' },
  { value: 'non-binary',        label: 'Non-binary' },
  { value: 'prefer-not-to-say', label: 'Prefer not to say' },
]
const AGE_OPTIONS = ['', '20s', '30s', '40s', '50s', '60s', '70+']

const GUARDRAILS = [
  { key: 'avatar_use_post_image', label: 'Post images',    hint: 'Standalone images generated for a post' },
  { key: 'avatar_use_carousel',   label: 'Carousel slides', hint: 'Slide artwork on personal-story carousels' },
  { key: 'avatar_use_video',      label: 'Video frames',    hint: 'The source frame every generated video is built from' },
  { key: 'avatar_use_newsletter', label: 'Newsletter covers', hint: 'Cover art when an edition is about you or your story' },
  { key: 'avatar_caption_overlay', label: 'Captions over your avatar', hint: 'Let burned-in video captions sit on frames your avatar appears in' },
] as const

type Training = {
  id: number
  training_id: string
  model_ref: string | null
  trigger_word: string
  status: string
  is_active: boolean
  gender_presentation: string | null
  age_band: string | null
  attributes_confirmed_at: string | null
  approval_status: string
  approved_at: string | null
  sample_paths: { label: string; path: string }[]
  samples_generated_at: string | null
  sample_regen_count: number
  created_at: string | null
}

type Samples = {
  avatar_id: number
  approval_status: string
  samples: { label: string; url: string }[]
  samples_generated_at: string | null
  sample_regen_count: number
  sample_regen_remaining: number
  gender_presentation: string | null
  age_band: string | null
}

type AvatarPreferences = {
  avatar_disabled: boolean
  avatar_use_post_image: boolean
  avatar_use_carousel: boolean
  avatar_use_video: boolean
  avatar_use_newsletter: boolean
  avatar_caption_overlay: boolean
}

export default function Avatars() {
  const { sessionToken, user } = useAuth()
  const queryClient = useQueryClient()

  const [files, setFiles]             = useState<FileList | null>(null)
  const [triggerWord, setTriggerWord] = useState(`LEMAVTR${user?.userId ?? ''}`)
  const [trainError, setTrainError]   = useState('')
  const fileRef = useRef<HTMLInputElement>(null)

  // --- Credit balance + active avatar ---
  const { data: creditData } = useQuery({
    queryKey: ['avatar-credits', sessionToken],
    queryFn: async () => {
      const r = await api.get('/avatar/credits', { params: { session_token: sessionToken } })
      return r.data.detail as { balance: number; active_avatar: Training | null }
    },
    enabled: !!sessionToken,
  })

  // --- Training list ---
  const { data: trainings = [] } = useQuery({
    queryKey: ['avatar-trainings', sessionToken],
    queryFn: async () => {
      const r = await api.get('/avatar/trainings', { params: { session_token: sessionToken } })
      return r.data.detail as Training[]
    },
    enabled: !!sessionToken,
    refetchInterval: (query) =>
      (query.state.data ?? []).some((t: Training) =>
        ['starting', 'processing'].includes(t.status) ||
        // Samples render in the background after training succeeds — keep polling until they land,
        // or the gallery stays empty until the user reloads the page.
        (t.status === 'succeeded' && (t.sample_paths?.length ?? 0) === 0))
        ? 20_000
        : false,
  })

  // --- Guardrails ---
  const { data: prefs } = useQuery({
    queryKey: ['avatar-preferences', sessionToken],
    queryFn: async () => {
      const r = await api.get('/avatar/preferences', { params: { session_token: sessionToken } })
      return r.data.detail as AvatarPreferences
    },
    enabled: !!sessionToken,
  })

  const prefsMutation = useMutation({
    mutationFn: async (patch: Partial<AvatarPreferences>) => {
      await api.put('/avatar/preferences', { session_token: sessionToken, ...patch })
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['avatar-preferences'] }),
  })

  // --- Buy credits ---
  const buyMutation = useMutation({
    mutationFn: async (pkg: string) => {
      const r = await api.post('/avatar/credits/checkout', {
        session_token: sessionToken,
        package: pkg,
        success_url: `${window.location.origin}/avatars?credits=purchased`,
        cancel_url:  `${window.location.origin}/avatars`,
      })
      return r.data.detail.checkout_url as string
    },
    onSuccess: (url) => { window.location.href = url },
  })

  // --- Train avatar ---
  const trainMutation = useMutation({
    mutationFn: async () => {
      if (!files || files.length === 0) throw new Error('Please select photos to upload.')
      if (!triggerWord.trim()) throw new Error('Trigger word is required.')

      // react-query catches this rejection, so the window-level handler would never see it —
      // the wrapper is what makes a stale jszip chunk self-heal instead of failing the training.
      const { default: JSZip } = await importWithChunkRecovery(() => import('jszip'))
      const zip = new JSZip()
      Array.from(files).forEach((f) => zip.file(f.name, f))
      const zipBlob = await zip.generateAsync({ type: 'blob' })

      const form = new FormData()
      form.append('session_token', sessionToken ?? '')
      form.append('trigger_word', triggerWord.trim().toUpperCase())
      form.append('photos', zipBlob, 'photos.zip')

      const r = await api.post('/avatar/training', form, {
        headers: { 'Content-Type': 'multipart/form-data' },
      })
      return r.data.detail
    },
    onSuccess: () => {
      setFiles(null)
      setTrainError('')
      if (fileRef.current) fileRef.current.value = ''
      queryClient.invalidateQueries({ queryKey: ['avatar-trainings'] })
      queryClient.invalidateQueries({ queryKey: ['avatar-credits'] })
    },
    onError: (err: unknown) => {
      const msg = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail
        ?? (err as Error)?.message
        ?? 'Training failed'
      setTrainError(msg)
    },
  })

  // --- Sync status ---
  const syncMutation = useMutation({
    mutationFn: async (avatarId: number) => {
      const r = await api.get(`/avatar/training/${avatarId}/status`, {
        params: { session_token: sessionToken },
      })
      return r.data.detail
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['avatar-trainings'] }),
  })

  // --- Activate ---
  const activateMutation = useMutation({
    mutationFn: async (avatarId: number) => {
      await api.put(`/avatar/training/${avatarId}/activate`, { session_token: sessionToken })
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['avatar-trainings'] })
      queryClient.invalidateQueries({ queryKey: ['avatar-credits'] })
    },
  })

  const balance = creditData?.balance ?? 0
  const hasCredits = balance > 0
  const inProgress = trainings.some((t) => ['starting', 'processing'].includes(t.status))
  const avatarOff = !!prefs?.avatar_disabled

  const handleFileChange = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    setFiles(e.target.files)
    setTrainError('')
  }, [])

  return (
    <div className="max-w-4xl mx-auto px-4 py-8 space-y-10">
      <h1 className="text-2xl font-bold text-gray-900">My Avatars</h1>

      {/* Credit balance + usage */}
      <div
        className="flex items-center justify-between bg-blue-50 border border-blue-200 rounded-xl px-6 py-4"
        data-testid="avatar-credit-balance"
      >
        <div>
          <p className="text-sm text-blue-700 font-medium">Avatar Training Credits</p>
          <p className="text-3xl font-bold text-blue-900">{balance}</p>
          <p className="text-xs text-blue-700 mt-1" data-testid="avatar-usage-summary">
            {trainings.length} training{trainings.length === 1 ? '' : 's'} used ·{' '}
            {trainings.filter((t) => t.approval_status === 'approved').length} approved
          </p>
        </div>
        {hasCredits ? (
          <span className="text-sm text-blue-700">
            {balance === 1 ? '1 training available' : `${balance} trainings available`}
          </span>
        ) : (
          <span className="text-sm text-gray-500">Purchase credits below to train your first avatar</span>
        )}
      </div>

      {/* Guardrails */}
      <section data-testid="avatar-guardrails">
        <h2 className="text-lg font-semibold text-gray-800 mb-1">Where your avatar may be used</h2>
        <p className="text-sm text-gray-500 mb-4">
          Off by default. Your avatar is only used on the content types you switch on here, and only
          after you have approved its preview images. Posts you compose can override this per post.
        </p>

        <div className="border border-gray-200 rounded-xl divide-y divide-gray-100 bg-white">
          <label className="flex items-start gap-3 px-5 py-4 cursor-pointer">
            <input
              type="checkbox"
              checked={avatarOff}
              onChange={(e) => prefsMutation.mutate({ avatar_disabled: e.target.checked })}
              data-testid="avatar-disabled-toggle"
              className="mt-0.5 h-4 w-4 rounded border-gray-300 text-red-600 focus:ring-red-500"
            />
            <span>
              <span className="block text-sm font-semibold text-gray-800">Don’t use my avatar</span>
              <span className="block text-xs text-gray-500">
                Forces every image and video back to stock or generic AI artwork. Overrides everything below.
              </span>
            </span>
          </label>

          {GUARDRAILS.map((g) => (
            <label
              key={g.key}
              className={`flex items-start gap-3 px-5 py-4 ${avatarOff ? 'opacity-50' : 'cursor-pointer'}`}
            >
              <input
                type="checkbox"
                disabled={avatarOff}
                checked={!!prefs?.[g.key]}
                onChange={(e) => prefsMutation.mutate({ [g.key]: e.target.checked })}
                data-testid={`avatar-toggle-${g.key}`}
                className="mt-0.5 h-4 w-4 rounded border-gray-300 text-blue-600 focus:ring-blue-500"
              />
              <span>
                <span className="block text-sm font-medium text-gray-800">{g.label}</span>
                <span className="block text-xs text-gray-500">{g.hint}</span>
              </span>
            </label>
          ))}
        </div>
      </section>

      {/* Pricing */}
      <section>
        <h2 className="text-lg font-semibold text-gray-800 mb-4">Buy Training Credits</h2>
        <p className="text-sm text-gray-500 mb-5">
          Each credit lets you train one personalized AI avatar using your photos on Replicate's
          FLUX.1 model. Training takes ~2 minutes.
        </p>
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-4" data-testid="avatar-pricing-cards">
          {PACKAGES.map((pkg) => (
            <div
              key={pkg.key}
              className={`relative border rounded-xl p-4 flex flex-col items-center gap-2 shadow-sm hover:shadow-md transition-shadow ${
                pkg.key === 'value' ? 'border-blue-400 bg-blue-50' : 'border-gray-200 bg-white'
              }`}
            >
              {pkg.badge && (
                <span className="absolute -top-3 bg-blue-600 text-white text-xs font-bold px-2 py-0.5 rounded-full">
                  {pkg.badge}
                </span>
              )}
              <span className="text-2xl font-bold text-gray-900">{pkg.price}</span>
              <span className="text-sm font-medium text-gray-700">{pkg.label}</span>
              {pkg.savings && (
                <span className="text-xs text-green-700 font-semibold">{pkg.savings}</span>
              )}
              <button
                onClick={() => buyMutation.mutate(pkg.key)}
                disabled={buyMutation.isPending}
                className="mt-2 w-full py-1.5 rounded-lg bg-blue-600 hover:bg-blue-700 text-white text-sm font-medium disabled:opacity-50 transition-colors"
              >
                {buyMutation.isPending ? 'Loading…' : 'Buy'}
              </button>
            </div>
          ))}
        </div>
        {buyMutation.isError && (
          <p className="mt-3 text-sm text-red-600">
            Could not start checkout. Please try again.
          </p>
        )}
      </section>

      {/* Train new avatar */}
      <section>
        <h2 className="text-lg font-semibold text-gray-800 mb-1">Train New Avatar</h2>
        <p className="text-sm text-gray-500 mb-4">
          Upload 10–20 clear photos of yourself (different angles and lighting). Each training
          costs 1 credit. Credits are refunded automatically if training fails.
        </p>

        <div
          className={`border-2 border-dashed rounded-xl p-6 space-y-4 ${
            hasCredits ? 'border-gray-300 bg-white' : 'border-gray-200 bg-gray-50 opacity-60'
          }`}
        >
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">
              Photos (JPG / PNG / WebP)
            </label>
            <input
              ref={fileRef}
              type="file"
              multiple
              accept="image/jpeg,image/png,image/webp"
              disabled={!hasCredits}
              onChange={handleFileChange}
              className="block w-full text-sm text-gray-600 file:mr-4 file:py-1.5 file:px-3 file:rounded-lg file:border-0 file:bg-blue-50 file:text-blue-700 file:font-medium hover:file:bg-blue-100"
            />
            {files && (
              <p className="mt-1 text-xs text-gray-500">{files.length} file(s) selected</p>
            )}
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">
              Trigger Word
              <span className="ml-1 text-xs text-gray-400">(used in image prompts to activate your avatar)</span>
            </label>
            <input
              type="text"
              value={triggerWord}
              onChange={(e) => setTriggerWord(e.target.value.toUpperCase())}
              disabled={!hasCredits}
              placeholder="e.g. LEMAVTR42"
              className="block w-full rounded-lg border border-gray-300 px-3 py-2 text-sm focus:border-blue-400 focus:outline-none disabled:bg-gray-50"
            />
          </div>

          {trainError && (
            <p className="text-sm text-red-600">{trainError}</p>
          )}

          <button
            onClick={() => trainMutation.mutate()}
            disabled={!hasCredits || trainMutation.isPending || inProgress}
            className="py-2 px-5 rounded-lg bg-blue-600 hover:bg-blue-700 text-white text-sm font-semibold disabled:opacity-50 transition-colors"
          >
            {trainMutation.isPending
              ? 'Starting training…'
              : !hasCredits
              ? 'No credits — purchase above'
              : inProgress
              ? 'Training in progress…'
              : `Train Avatar (uses 1 credit)`}
          </button>
        </div>
      </section>

      {/* Training list */}
      {trainings.length > 0 && (
        <section>
          <h2 className="text-lg font-semibold text-gray-800 mb-4">My Trained Avatars</h2>
          <div className="space-y-3">
            {trainings.map((t) => (
              <div
                key={t.id}
                data-testid={`avatar-card-${t.id}`}
                className={`border rounded-xl px-5 py-4 space-y-4 ${
                  t.is_active ? 'border-green-400 bg-green-50' : 'border-gray-200 bg-white'
                }`}
              >
                <div className="flex items-center justify-between gap-4">
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="font-mono text-sm font-semibold text-gray-800">
                        {t.trigger_word}
                      </span>
                      {t.is_active && (
                        <span className="text-xs bg-green-600 text-white px-2 py-0.5 rounded-full font-medium">
                          Active
                        </span>
                      )}
                      <span
                        className={`text-xs px-2 py-0.5 rounded-full font-medium ${
                          STATUS_COLORS[t.status] ?? 'bg-gray-100 text-gray-600'
                        }`}
                      >
                        {t.status}
                      </span>
                      {t.status === 'succeeded' && (
                        <span
                          data-testid={`avatar-approval-${t.id}`}
                          className={`text-xs px-2 py-0.5 rounded-full font-medium ${
                            APPROVAL_COLORS[t.approval_status] ?? 'bg-gray-100 text-gray-600'
                          }`}
                        >
                          {t.approval_status}
                        </span>
                      )}
                    </div>
                    <p className="text-xs text-gray-400 mt-0.5">
                      Started {t.created_at ? new Date(t.created_at).toLocaleDateString() : '—'}
                    </p>
                  </div>

                  <div className="flex gap-2 flex-shrink-0">
                    {['starting', 'processing'].includes(t.status) && (
                      <button
                        onClick={() => syncMutation.mutate(t.id)}
                        disabled={syncMutation.isPending}
                        className="text-xs px-3 py-1.5 rounded-lg border border-blue-300 text-blue-700 hover:bg-blue-50 disabled:opacity-50"
                      >
                        Refresh
                      </button>
                    )}
                    {t.status === 'succeeded' && !t.is_active && (
                      <button
                        onClick={() => activateMutation.mutate(t.id)}
                        disabled={activateMutation.isPending || t.approval_status !== 'approved'}
                        title={t.approval_status !== 'approved'
                          ? 'Approve the preview images first'
                          : undefined}
                        className="text-xs px-3 py-1.5 rounded-lg bg-green-600 hover:bg-green-700 text-white font-medium disabled:opacity-50"
                      >
                        Set Active
                      </button>
                    )}
                  </div>
                </div>

                {t.status === 'succeeded' && (
                  <AvatarReviewPanel training={t} sessionToken={sessionToken ?? ''} />
                )}
              </div>
            ))}
          </div>
        </section>
      )}
    </div>
  )
}

function AvatarReviewPanel({ training, sessionToken }: { training: Training; sessionToken: string }) {
  const queryClient = useQueryClient()
  const [gender, setGender] = useState(training.gender_presentation ?? '')
  const [ageBand, setAgeBand] = useState(training.age_band ?? '')
  const [actionError, setActionError] = useState('')

  const { data: samples } = useQuery({
    queryKey: ['avatar-samples', training.id, training.samples_generated_at],
    queryFn: async () => {
      const r = await api.get(`/avatar/training/${training.id}/samples`, {
        params: { session_token: sessionToken },
      })
      return r.data.detail as Samples
    },
    enabled: !!sessionToken,
  })

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['avatar-trainings'] })
    queryClient.invalidateQueries({ queryKey: ['avatar-samples', training.id] })
  }

  const onError = (err: unknown) => {
    setActionError(
      (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      ?? 'Something went wrong. Please try again.')
  }

  const attributesMutation = useMutation({
    mutationFn: async () => {
      await api.put(`/avatar/training/${training.id}/attributes`, {
        session_token: sessionToken,
        gender_presentation: gender || null,
        age_band: ageBand || null,
      })
    },
    onSuccess: () => { setActionError(''); invalidate() },
    onError,
  })

  const verdictMutation = useMutation({
    mutationFn: async (verdict: 'approve' | 'reject') => {
      await api.post(`/avatar/training/${training.id}/${verdict}`, { session_token: sessionToken })
    },
    onSuccess: () => { setActionError(''); invalidate() },
    onError,
  })

  const regenerateMutation = useMutation({
    mutationFn: async () => {
      await api.post(`/avatar/training/${training.id}/samples`, { session_token: sessionToken })
    },
    onSuccess: () => { setActionError(''); invalidate() },
    onError,
  })

  const gallery = samples?.samples ?? []
  const regenRemaining = samples?.sample_regen_remaining ?? 0

  return (
    <div className="border-t border-gray-100 pt-4 space-y-4">
      {/* Declared attributes */}
      <div>
        <p className="text-sm font-medium text-gray-800">How should you be described?</p>
        <p className="text-xs text-gray-500 mb-2">
          Written into every image prompt so the generator can’t invent someone else. You choose
          these — we never guess them from your photos, and leaving them blank adds nothing.
        </p>
        <div className="flex flex-wrap items-end gap-3">
          <label className="text-xs text-gray-600">
            <span className="block mb-1">Gender presentation</span>
            <select
              value={gender}
              onChange={(e) => setGender(e.target.value)}
              data-testid={`avatar-gender-${training.id}`}
              className="rounded-lg border border-gray-300 px-2 py-1.5 text-sm"
            >
              {GENDER_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>{o.label}</option>
              ))}
            </select>
          </label>
          <label className="text-xs text-gray-600">
            <span className="block mb-1">Age band</span>
            <select
              value={ageBand}
              onChange={(e) => setAgeBand(e.target.value)}
              data-testid={`avatar-age-${training.id}`}
              className="rounded-lg border border-gray-300 px-2 py-1.5 text-sm"
            >
              {AGE_OPTIONS.map((o) => (
                <option key={o} value={o}>{o || 'Not specified'}</option>
              ))}
            </select>
          </label>
          <button
            onClick={() => attributesMutation.mutate()}
            disabled={attributesMutation.isPending}
            className="text-xs px-3 py-1.5 rounded-lg border border-blue-300 text-blue-700 hover:bg-blue-50 disabled:opacity-50"
          >
            {attributesMutation.isPending ? 'Saving…' : 'Save'}
          </button>
        </div>
      </div>

      {/* Preview gallery */}
      <div>
        <p className="text-sm font-medium text-gray-800 mb-2">Preview</p>
        {gallery.length === 0 ? (
          <p className="text-xs text-gray-500" data-testid={`avatar-samples-empty-${training.id}`}>
            Preview images are still rendering. This takes a minute or two after training finishes.
          </p>
        ) : (
          <div className="grid grid-cols-3 gap-3" data-testid={`avatar-samples-${training.id}`}>
            {gallery.map((s) => (
              <figure key={s.label} className="space-y-1">
                <img
                  src={s.url}
                  alt={`${training.trigger_word} ${s.label} sample`}
                  loading="lazy"
                  className="w-full aspect-square object-cover rounded-lg border border-gray-200"
                />
                <figcaption className="text-[11px] text-gray-500 text-center">
                  {s.label.replace('_', ' ')}
                </figcaption>
              </figure>
            ))}
          </div>
        )}
      </div>

      {actionError && <p className="text-sm text-red-600">{actionError}</p>}

      <div className="flex flex-wrap items-center gap-2">
        <button
          onClick={() => verdictMutation.mutate('approve')}
          disabled={verdictMutation.isPending || gallery.length === 0}
          data-testid={`avatar-approve-${training.id}`}
          className="text-xs px-3 py-1.5 rounded-lg bg-green-600 hover:bg-green-700 text-white font-medium disabled:opacity-50"
        >
          Approve — this looks like me
        </button>
        <button
          onClick={() => verdictMutation.mutate('reject')}
          disabled={verdictMutation.isPending}
          data-testid={`avatar-reject-${training.id}`}
          className="text-xs px-3 py-1.5 rounded-lg border border-red-300 text-red-700 hover:bg-red-50 disabled:opacity-50"
        >
          Reject
        </button>
        <button
          onClick={() => regenerateMutation.mutate()}
          disabled={regenerateMutation.isPending || regenRemaining <= 0}
          data-testid={`avatar-regenerate-${training.id}`}
          className="text-xs px-3 py-1.5 rounded-lg border border-gray-300 text-gray-700 hover:bg-gray-50 disabled:opacity-50"
        >
          Regenerate previews
        </button>
        <span className="text-[11px] text-gray-500">
          {regenRemaining} regeneration{regenRemaining === 1 ? '' : 's'} left
        </span>
      </div>
    </div>
  )
}
