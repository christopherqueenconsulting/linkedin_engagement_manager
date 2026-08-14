import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import api from '../../api/client'
import { useAuth } from '../../contexts/useAuth'
import SettingsCard from '../../components/SettingsCard'
import { enrollPasskey, useStepUp } from '../../hooks/useStepUp'
import { isPasskeySupported } from '../../utils/webauthn'

type Factor = {
  id: number
  kind: string
  label: string | null
  created_at: string | null
  last_used_at: string | null
}

type FactorsDetail = {
  factors: Factor[]
  recovery_codes_unused: number
  recovery_codes_total: number
  passkeys_supported: boolean
  has_strong_factor: boolean
  pin_is_bootstrap_only: boolean
  step_up_satisfied: boolean
  // Mandatory enrolment (issue #905) — the date after which a PIN alone stops opening a session.
  strong_factor_deadline: string | null
  enrollment_required: boolean
}

const KIND_LABELS: Record<string, string> = {
  passkey: 'Passkey',
  totp: 'Authenticator app',
}

function when(value: string | null): string {
  if (!value) return '—'
  const d = new Date(value)
  return Number.isNaN(d.getTime()) ? '—' : d.toLocaleDateString()
}

/**
 * Two-factor enrolment (issue #745, phase 2c).
 *
 * The card exists to move a person from "my email inbox is the only thing protecting my LinkedIn
 * account" to "a key on my phone is", so it is deliberately opinionated: passkey first because it
 * is the only phishing-resistant option, the authenticator app as the fallback for people passkeys
 * do not fit, and recovery codes shown ONCE with no way to see them again — which is why the panel
 * that reveals them will not close by accident.
 */
export default function AuthFactorsCard() {
  const { sessionToken } = useAuth()
  const queryClient = useQueryClient()
  const { guard, stepUpModal } = useStepUp()
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)
  const [totpSecret, setTotpSecret] = useState<{ secret: string; uri: string } | null>(null)
  const [totpCode, setTotpCode] = useState('')
  const [freshCodes, setFreshCodes] = useState<string[] | null>(null)

  const flash = (ok: boolean, text: string) => {
    setMsg({ ok, text })
    setTimeout(() => setMsg(null), 6000)
  }

  const { data } = useQuery({
    queryKey: ['auth-factors', sessionToken],
    queryFn: () =>
      api
        .get(`/user/auth-factors?session_token=${encodeURIComponent(sessionToken!)}`)
        .then((r) => r.data.detail as FactorsDetail),
    enabled: !!sessionToken,
    staleTime: 30 * 1000,
  })

  const refresh = () => queryClient.invalidateQueries({ queryKey: ['auth-factors'] })

  // Enrolment is guarded like every other write here, because adding a factor to an account that
  // already has one is step-up gated server-side (the FIRST one is not — see
  // auth_factors.enrollment_allowed). Without the guard a user adding a second passkey would just
  // see "could not be registered" with no way to get past it.
  const addPasskey = useMutation({
    mutationFn: () => guard(() => enrollPasskey(sessionToken, 'Passkey')),
    onSuccess: (added) => {
      if (!added) return
      refresh()
      flash(true, 'Passkey added. Save your recovery codes before you leave this page.')
    },
    onError: (e: { response?: { data?: { detail?: string } } }) =>
      flash(false, e?.response?.data?.detail || 'That passkey could not be registered.'),
  })

  const startTotp = useMutation({
    mutationFn: () =>
      guard(() =>
        api
          .post('/user/totp/enroll/begin', { session_token: sessionToken })
          .then((r) => r.data.detail as { secret: string; otpauth_uri: string }),
      ),
    onSuccess: (detail) => {
      if (!detail) return
      setTotpSecret({ secret: detail.secret, uri: detail.otpauth_uri })
    },
    onError: (e: { response?: { data?: { detail?: string } } }) =>
      flash(false, e?.response?.data?.detail || 'Could not start authenticator setup.'),
  })

  const confirmTotp = useMutation({
    mutationFn: () =>
      guard(() =>
        api.post('/user/totp/enroll/confirm', {
          session_token: sessionToken,
          code: totpCode.trim(),
        }),
      ),
    onSuccess: (result) => {
      if (result === null) return
      setTotpSecret(null)
      setTotpCode('')
      refresh()
      flash(true, 'Authenticator app added. Save your recovery codes.')
    },
    onError: (e: { response?: { data?: { detail?: string } } }) =>
      flash(false, e?.response?.data?.detail || 'That code did not match.'),
  })

  const removeFactor = useMutation({
    mutationFn: (factorId: number) =>
      guard(() =>
        api.post('/user/auth-factors/delete', { session_token: sessionToken, factor_id: factorId }),
      ),
    onSuccess: (result) => {
      if (result === null) return
      refresh()
      flash(true, 'Removed.')
    },
    onError: () => flash(false, 'Could not remove that factor.'),
  })

  const regenerate = useMutation({
    mutationFn: () =>
      guard(() =>
        api
          .post('/user/recovery-codes/regenerate', { session_token: sessionToken })
          .then((r) => r.data.detail.codes as string[]),
      ),
    onSuccess: (codes) => {
      if (codes === null) return
      setFreshCodes(codes)
      refresh()
    },
    onError: () => flash(false, 'Could not generate recovery codes.'),
  })

  const factors = data?.factors ?? []
  const hasTotp = factors.some((f) => f.kind === 'totp')

  return (
    <SettingsCard
      title="Two-factor sign-in"
      subtitle="A key on your device instead of a code in your inbox — the one change that stops a stolen mailbox from becoming a stolen LinkedIn account."
    >
      {stepUpModal}

      {data && (
        <p className="text-xs text-gray-500">
          {data.pin_is_bootstrap_only
            ? 'An emailed code alone will no longer sign you in — it now has to be followed by one of the factors below.'
            : 'Right now, anyone who can read your email can sign in as you. Add a factor to change that.'}
          {!data.has_strong_factor && data.strong_factor_deadline && (
            <span className="block mt-1 font-semibold text-amber-700" data-testid="factor-deadline">
              {data.enrollment_required
                ? 'Required now — the rest of LEM unlocks once a factor is saved.'
                : `Required from ${when(data.strong_factor_deadline)}.`}
            </span>
          )}
        </p>
      )}

      <div>
        <h4 className="text-sm font-semibold text-gray-800 mb-2">Your factors</h4>
        {factors.length === 0 ? (
          <p className="text-xs text-gray-500">Nothing enrolled yet.</p>
        ) : (
          <ul className="divide-y divide-gray-100 border border-gray-200 rounded-lg">
            {factors.map((f) => (
              <li key={f.id} className="flex items-center justify-between gap-3 px-3 py-2">
                <div className="min-w-0">
                  <p className="text-sm font-medium text-gray-800 truncate">
                    {KIND_LABELS[f.kind] ?? f.kind}
                    {f.label && f.label !== KIND_LABELS[f.kind] ? ` — ${f.label}` : ''}
                  </p>
                  <p className="text-xs text-gray-500">
                    Added {when(f.created_at)} · last used {when(f.last_used_at)}
                  </p>
                </div>
                <button
                  type="button"
                  onClick={() => removeFactor.mutate(f.id)}
                  disabled={removeFactor.isPending}
                  className="text-xs font-semibold text-red-600 hover:underline disabled:opacity-50 whitespace-nowrap"
                >
                  Remove
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="flex flex-col sm:flex-row gap-2">
        {data?.passkeys_supported && isPasskeySupported() && (
          <button
            type="button"
            onClick={() => addPasskey.mutate()}
            disabled={addPasskey.isPending}
            className="bg-blue-600 text-white px-4 py-2 rounded-lg text-sm font-semibold hover:bg-blue-700 disabled:opacity-50"
          >
            {addPasskey.isPending ? 'Waiting for your device…' : 'Add a passkey'}
          </button>
        )}
        {!hasTotp && (
          <button
            type="button"
            onClick={() => startTotp.mutate()}
            disabled={startTotp.isPending}
            className="border border-gray-300 text-gray-700 px-4 py-2 rounded-lg text-sm font-semibold hover:bg-gray-50 disabled:opacity-50"
          >
            Use an authenticator app
          </button>
        )}
      </div>

      {totpSecret && (
        <div className="border border-gray-200 rounded-lg p-3 space-y-2">
          <p className="text-xs text-gray-600">
            Add this key to your authenticator app, then enter the 6-digit code it shows.
          </p>
          <code className="block text-sm font-mono bg-gray-50 px-2 py-1 rounded break-all"
                data-testid="totp-secret">
            {totpSecret.secret}
          </code>
          <div className="flex flex-col sm:flex-row gap-2">
            <input
              type="text"
              inputMode="numeric"
              maxLength={6}
              aria-label="Authenticator code"
              value={totpCode}
              onChange={(e) => setTotpCode(e.target.value.replace(/\D/g, ''))}
              placeholder="123456"
              className="flex-1 border border-gray-300 rounded-lg px-3 py-2 text-sm text-center tracking-widest font-mono"
            />
            <button
              type="button"
              onClick={() => confirmTotp.mutate()}
              disabled={confirmTotp.isPending || totpCode.trim().length !== 6}
              className="bg-gray-800 text-white px-4 py-2 rounded-lg text-sm font-semibold hover:bg-gray-900 disabled:opacity-50"
            >
              {confirmTotp.isPending ? 'Checking…' : 'Confirm'}
            </button>
          </div>
        </div>
      )}

      <div>
        <h4 className="text-sm font-semibold text-gray-800 mb-1">Recovery codes</h4>
        <p className="text-xs text-gray-500 mb-2">
          {data ? `${data.recovery_codes_unused} of ${data.recovery_codes_total || 0} unused.` : ''}{' '}
          Each one signs you in once if you lose your device — then lets you enrol a new factor. They
          are shown once and never again.
        </p>
        <button
          type="button"
          onClick={() => regenerate.mutate()}
          disabled={regenerate.isPending}
          className="text-xs font-semibold text-blue-600 hover:underline disabled:opacity-50"
        >
          {data?.recovery_codes_total ? 'Generate a new set' : 'Generate recovery codes'}
        </button>
      </div>

      {freshCodes && (
        <div className="border-2 border-amber-300 bg-amber-50 rounded-lg p-3 space-y-2">
          <p className="text-xs font-semibold text-amber-900">
            Save these now — this is the only time they will be shown. The previous set no longer
            works.
          </p>
          <ul className="grid grid-cols-2 gap-1 font-mono text-sm" data-testid="recovery-codes">
            {freshCodes.map((c) => (
              <li key={c}>{c}</li>
            ))}
          </ul>
          <button
            type="button"
            onClick={() => setFreshCodes(null)}
            className="text-xs font-semibold text-amber-900 hover:underline"
          >
            I've saved them
          </button>
        </div>
      )}

      {msg && (
        <p className={`text-sm font-medium ${msg.ok ? 'text-green-600' : 'text-red-600'}`}>{msg.text}</p>
      )}
    </SettingsCard>
  )
}
