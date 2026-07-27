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

type Training = {
  id: number
  training_id: string
  model_ref: string | null
  trigger_word: string
  status: string
  is_active: boolean
  created_at: string | null
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
      (query.state.data ?? []).some((t: Training) => ['starting', 'processing'].includes(t.status))
        ? 20_000
        : false,
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

  const handleFileChange = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    setFiles(e.target.files)
    setTrainError('')
  }, [])

  return (
    <div className="max-w-4xl mx-auto px-4 py-8 space-y-10">
      <h1 className="text-2xl font-bold text-gray-900">My Avatars</h1>

      {/* Credit balance */}
      <div
        className="flex items-center justify-between bg-blue-50 border border-blue-200 rounded-xl px-6 py-4"
        data-testid="avatar-credit-balance"
      >
        <div>
          <p className="text-sm text-blue-700 font-medium">Avatar Training Credits</p>
          <p className="text-3xl font-bold text-blue-900">{balance}</p>
        </div>
        {hasCredits && (
          <span className="text-sm text-blue-700">
            {balance === 1 ? '1 training available' : `${balance} trainings available`}
          </span>
        )}
        {!hasCredits && (
          <span className="text-sm text-gray-500">Purchase credits below to train your first avatar</span>
        )}
      </div>

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
                className={`border rounded-xl px-5 py-4 flex items-center justify-between gap-4 ${
                  t.is_active ? 'border-green-400 bg-green-50' : 'border-gray-200 bg-white'
                }`}
              >
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2">
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
                      disabled={activateMutation.isPending}
                      className="text-xs px-3 py-1.5 rounded-lg bg-green-600 hover:bg-green-700 text-white font-medium disabled:opacity-50"
                    >
                      Set Active
                    </button>
                  )}
                </div>
              </div>
            ))}
          </div>
        </section>
      )}
    </div>
  )
}
