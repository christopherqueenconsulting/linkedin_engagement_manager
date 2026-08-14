import { useCallback, useRef, useState } from 'react'
import api from '../api/client'
import { useAuth } from '../contexts/useAuth'
import { createPasskey, getPasskeyAssertion, isPasskeySupported } from '../utils/webauthn'

/**
 * The step-up gate, from the browser's side (issue #745, phase 2c).
 *
 * The server answers a credential-touching write it will not run yet with **403** and
 * `{code: 'step_up_required', methods: [...]}` — deliberately not 401, which the axios interceptor
 * would read as a dead session and sign the user out of. `guard()` wraps the call: it runs it, and
 * if that is the answer it comes back with, it opens the modal, waits for a factor, and RE-RUNS the
 * original call. The caller writes one line and never learns the gate exists.
 */

type StepUpMethod = 'passkey' | 'totp'

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function stepUpMethods(error: any): StepUpMethod[] | null {
  const detail = error?.response?.data?.detail
  if (error?.response?.status !== 403 || detail?.code !== 'step_up_required') return null
  const methods = (detail.methods ?? []).filter(
    (m: string): m is StepUpMethod => m === 'passkey' || m === 'totp',
  )
  // No usable method left (the account's only factor is a deployment that cannot serve passkeys):
  // still open the modal so the user is told WHY, rather than watching a button do nothing.
  return methods
}

export function useStepUp() {
  const { sessionToken } = useAuth()
  const [methods, setMethods] = useState<StepUpMethod[] | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [code, setCode] = useState('')
  const pending = useRef<(() => Promise<unknown>) | null>(null)
  const resolvePending = useRef<((ok: boolean) => void) | null>(null)

  const close = useCallback((ok: boolean) => {
    setMethods(null)
    setCode('')
    setError(null)
    setBusy(false)
    pending.current = null
    resolvePending.current?.(ok)
    resolvePending.current = null
  }, [])

  const guard = useCallback(
    async <T,>(run: () => Promise<T>): Promise<T | null> => {
      try {
        return await run()
      } catch (e) {
        const needed = stepUpMethods(e)
        if (!needed) throw e
        pending.current = run as () => Promise<unknown>
        setMethods(needed)
        const verified = await new Promise<boolean>((resolve) => {
          resolvePending.current = resolve
        })
        if (!verified) return null
        return (await run()) as T
      }
    },
    [],
  )

  async function verify(method: StepUpMethod) {
    setBusy(true)
    setError(null)
    try {
      if (method === 'passkey') {
        const begun = await api.post('/user/step-up/begin', { session_token: sessionToken })
        const credential = await getPasskeyAssertion(begun.data.detail.options)
        if (!credential) {
          setBusy(false)
          return
        }
        await api.post('/user/step-up/verify', {
          session_token: sessionToken,
          method: 'passkey',
          handle: begun.data.detail.handle,
          credential,
        })
      } else {
        await api.post('/user/step-up/verify', {
          session_token: sessionToken,
          method: 'totp',
          code: code.trim(),
        })
      }
      close(true)
    } catch (e) {
      const detail = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
      setError(typeof detail === 'string' ? detail : 'That did not verify — try again.')
      setBusy(false)
    }
  }

  const modal = methods === null ? null : (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 px-4">
      <div className="bg-white rounded-xl shadow-xl w-full max-w-sm p-6 max-h-viewport overflow-y-auto" role="dialog"
           aria-modal="true" aria-label="Confirm it's you">
        <h3 className="text-lg font-bold text-gray-800 mb-1">Confirm it's you</h3>
        <p className="text-sm text-gray-500 mb-4">
          This change touches your LinkedIn credentials, so it needs one more check.
        </p>

        {methods.length === 0 && (
          <p className="text-sm text-red-600 mb-4">
            None of your enrolled factors can be used from this browser. Add an authenticator app
            in Security, or sign in again with a passkey.
          </p>
        )}

        {methods.includes('passkey') && isPasskeySupported() && (
          <button
            type="button"
            onClick={() => verify('passkey')}
            disabled={busy}
            className="w-full bg-blue-600 text-white py-2.5 rounded-lg text-sm font-semibold hover:bg-blue-700 disabled:opacity-50 mb-3"
          >
            Use your passkey
          </button>
        )}

        {methods.includes('totp') && (
          <div className="space-y-2 mb-3">
            <input
              type="text"
              inputMode="numeric"
              maxLength={6}
              aria-label="Authenticator code"
              value={code}
              onChange={(e) => setCode(e.target.value.replace(/\D/g, ''))}
              placeholder="6-digit code"
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm text-center tracking-widest font-mono"
            />
            <button
              type="button"
              onClick={() => verify('totp')}
              disabled={busy || code.trim().length !== 6}
              className="w-full bg-gray-800 text-white py-2.5 rounded-lg text-sm font-semibold hover:bg-gray-900 disabled:opacity-50"
            >
              {busy ? 'Checking…' : 'Confirm'}
            </button>
          </div>
        )}

        {error && <p className="text-xs text-red-600 mb-2">{error}</p>}
        <button
          type="button"
          onClick={() => close(false)}
          className="w-full text-xs text-gray-500 hover:text-gray-700"
        >
          Cancel
        </button>
      </div>
    </div>
  )

  return { guard, stepUpModal: modal }
}

/** Enrolment helpers, kept beside the gate they feed — a new passkey is the thing that makes the
 * gate satisfiable in the first place. Returns false when the user dismissed the browser prompt. */
export async function enrollPasskey(sessionToken: string | null, label?: string): Promise<boolean> {
  const begun = await api.post('/user/passkeys/register/begin', { session_token: sessionToken })
  const credential = await createPasskey(begun.data.detail.options)
  if (!credential) return false
  await api.post('/user/passkeys/register/complete', {
    session_token: sessionToken,
    handle: begun.data.detail.handle,
    credential,
    label,
  })
  return true
}
