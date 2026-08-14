import { useState } from 'react'
import { useAuth } from '../contexts/useAuth'
import api from '../api/client'
import { getAttribution } from '../utils/attribution'
import { recordSignup } from '../utils/analytics'
import { getPasskeyAssertion, isPasskeySupported } from '../utils/webauthn'

type Step = 'email' | 'pin' | 'second-factor'

// The factors the server will accept to finish a bootstrapped login. `passkey` is not in this list:
// a passkey sign-in is standalone (it proves possession AND identity in one ceremony), so the SPA
// sends the user down the passkey path instead of asking for an email code first.
type SecondFactorMethod = 'totp' | 'recovery_code'

export default function LoginModal() {
  const { closeLoginModal, login } = useAuth()
  const [step, setStep] = useState<Step>('email')
  const [email, setEmail] = useState('')
  const [pin, setPin] = useState('')
  const [isNewUser, setIsNewUser] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // Set when a PIN succeeded but the account has a strong factor enrolled (issue #745, 2c).
  const [pendingToken, setPendingToken] = useState<string | null>(null)
  const [methods, setMethods] = useState<string[]>([])
  const [factorMethod, setFactorMethod] = useState<SecondFactorMethod>('totp')
  const [factorCode, setFactorCode] = useState('')

  function errorText(err: unknown, fallback: string): string {
    const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
    return typeof detail === 'string' ? detail : fallback
  }

  /** A login that came back needing a second factor, on either the PIN or the bypass path. */
  function enterSecondFactor(detail: { pending_token: string; methods?: string[] }) {
    setPendingToken(detail.pending_token)
    setMethods(detail.methods ?? [])
    setFactorMethod((detail.methods ?? []).includes('totp') ? 'totp' : 'recovery_code')
    setStep('second-factor')
  }

  async function handleEmailSubmit(e: React.FormEvent) {
    e.preventDefault()
    setError(null)
    setLoading(true)
    try {
      const r = await api.post('/auth/email/init', {
        email: email.trim().toLowerCase(),
        attribution: getAttribution(),
      })
      const detail = r.data.detail

      if (detail.second_factor_required) {
        enterSecondFactor(detail)
        return
      }
      if (detail.bypass) {
        // No email provider — session already created server-side, log in immediately
        if (detail.is_new_user) recordSignup({ method: 'email_pin', pin_bypassed: true })
        login(detail.session_token, detail.email)
        return
      }

      setIsNewUser(detail.user_exists === false)
      setStep('pin')
    } catch (err: unknown) {
      setError(errorText(err, 'Failed to send code. Please try again.'))
    } finally {
      setLoading(false)
    }
  }

  async function handlePinSubmit(e: React.FormEvent) {
    e.preventDefault()
    setError(null)
    setLoading(true)
    try {
      const r = await api.post('/auth/email/verify', { email, pin, attribution: getAttribution() })
      const detail = r.data.detail
      // The emailed code is a bootstrap once a strong factor exists — it proved the mailbox, and
      // that is all it proves now.
      if (detail.second_factor_required) {
        enterSecondFactor(detail)
        return
      }
      // Only a brand-new account is a signup conversion — a returning user re-authenticating is a
      // login, the same line the API's own funnel event draws (issue #658).
      if (detail.is_new_user) recordSignup({ method: 'email_pin', pin_bypassed: false })
      login(detail.session_token, detail.email)
    } catch (err: unknown) {
      setError(errorText(err, 'Invalid or expired code. Please try again.'))
    } finally {
      setLoading(false)
    }
  }

  async function handleSecondFactorSubmit(e: React.FormEvent) {
    e.preventDefault()
    setError(null)
    setLoading(true)
    try {
      const r = await api.post('/auth/second-factor/verify', {
        pending_token: pendingToken,
        method: factorMethod,
        code: factorCode.trim(),
      })
      login(r.data.detail.session_token, r.data.detail.email)
    } catch (err: unknown) {
      // 401 is a wrong code and the pending sign-in survives it — stay here and let them retype.
      // 400 means the handle is spent (expired, or out of attempts), so retrying this form can
      // only fail forever: send them back to the start instead of a dead end.
      const status = (err as { response?: { status?: number } })?.response?.status
      setError(errorText(err, 'That code did not work.'))
      if (status === 400) {
        setPendingToken(null)
        setFactorCode('')
        setPin('')
        setStep('email')
      }
      setLoading(false)
    }
  }

  /** Passkey sign-in: the only path here that a phishing proxy cannot relay, so it is offered
   * first and needs no email at all. */
  async function handlePasskey() {
    setError(null)
    setLoading(true)
    try {
      const begun = await api.post('/auth/passkey/login/begin', {})
      const credential = await getPasskeyAssertion(begun.data.detail.options)
      if (!credential) {
        setLoading(false)
        return
      }
      const r = await api.post('/auth/passkey/login/complete', {
        handle: begun.data.detail.handle,
        credential,
      })
      login(r.data.detail.session_token, r.data.detail.email)
    } catch (err: unknown) {
      setError(errorText(err, 'That passkey did not work — use your email instead.'))
      setLoading(false)
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 px-4"
      onClick={(e) => { if (e.target === e.currentTarget) closeLoginModal() }}
    >
      {/* Height-capped and scrollable: a landscape phone is ~375px tall, and an uncapped panel put
          the code field and the submit button below the fold with no way to reach them (#894). */}
      <div className="bg-white rounded-xl shadow-xl w-full max-w-md p-6 sm:p-8 relative max-h-viewport overflow-y-auto">
        <button
          onClick={closeLoginModal}
          className="absolute top-4 right-4 text-gray-400 hover:text-gray-600 text-2xl leading-none"
          aria-label="Close"
        >
          ×
        </button>

        {step === 'email' && (
          <>
            <h2 className="text-xl font-bold text-gray-800 mb-1">Sign in / Sign up</h2>
            <p className="text-sm text-gray-500 mb-6">
              Enter your email and we'll send you a 6-digit code to continue.
            </p>
            {isPasskeySupported() && (
              <>
                <button
                  type="button"
                  onClick={handlePasskey}
                  disabled={loading}
                  className="w-full border border-gray-300 text-gray-700 py-2.5 rounded-lg text-sm font-semibold hover:bg-gray-50 disabled:opacity-50 mb-4"
                >
                  Sign in with a passkey
                </button>
                <p className="text-[11px] uppercase tracking-wide text-gray-400 text-center mb-4">or</p>
              </>
            )}
            <form onSubmit={handleEmailSubmit} className="space-y-4">
              <input
                type="email"
                required
                autoFocus
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="your@email.com"
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
              />
              {error && <p className="text-xs text-red-600">{error}</p>}
              <button
                type="submit"
                disabled={loading || !email.trim()}
                className="w-full bg-blue-600 text-white py-2.5 rounded-lg text-sm font-semibold hover:bg-blue-700 disabled:opacity-50 transition-colors"
              >
                {loading ? 'Sending code…' : 'Continue'}
              </button>
            </form>
          </>
        )}

        {step === 'pin' && (
          <>
            <h2 className="text-xl font-bold text-gray-800 mb-1">
              {isNewUser ? 'Verify your email' : 'Enter your code'}
            </h2>
            <p className="text-sm text-gray-500 mb-6">
              We sent a 6-digit code to <strong>{email}</strong>.
              {isNewUser && ' Confirming will create your account.'}
            </p>
            <form onSubmit={handlePinSubmit} className="space-y-4">
              <input
                type="text"
                inputMode="numeric"
                pattern="[0-9]{6}"
                maxLength={6}
                required
                autoFocus
                value={pin}
                onChange={(e) => setPin(e.target.value.replace(/\D/g, ''))}
                placeholder="123456"
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm text-center tracking-widest font-mono text-lg focus:outline-none focus:ring-2 focus:ring-blue-500"
              />
              {error && <p className="text-xs text-red-600">{error}</p>}
              <button
                type="submit"
                disabled={loading || pin.length !== 6}
                className="w-full bg-blue-600 text-white py-2.5 rounded-lg text-sm font-semibold hover:bg-blue-700 disabled:opacity-50 transition-colors"
              >
                {loading ? 'Verifying…' : isNewUser ? 'Create Account' : 'Sign In'}
              </button>
              <button
                type="button"
                onClick={() => { setStep('email'); setPin(''); setError(null) }}
                className="w-full text-xs text-gray-500 hover:text-gray-700"
              >
                ← Use a different email
              </button>
            </form>
          </>
        )}

        {step === 'second-factor' && (
          <>
            <h2 className="text-xl font-bold text-gray-800 mb-1">One more step</h2>
            <p className="text-sm text-gray-500 mb-6">
              Your account has two-factor sign-in turned on, so an emailed code isn't enough on its
              own.
            </p>
            {methods.includes('passkey') && isPasskeySupported() && (
              <>
                <button
                  type="button"
                  onClick={handlePasskey}
                  disabled={loading}
                  className="w-full bg-blue-600 text-white py-2.5 rounded-lg text-sm font-semibold hover:bg-blue-700 disabled:opacity-50 mb-4"
                >
                  Use your passkey
                </button>
                <p className="text-[11px] uppercase tracking-wide text-gray-400 text-center mb-4">or</p>
              </>
            )}
            <form onSubmit={handleSecondFactorSubmit} className="space-y-4">
              <input
                type="text"
                required
                autoFocus
                aria-label={factorMethod === 'totp' ? 'Authenticator code' : 'Recovery code'}
                value={factorCode}
                onChange={(e) => setFactorCode(e.target.value)}
                placeholder={factorMethod === 'totp' ? '123456' : 'ABCD234XYZ'}
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm text-center tracking-widest font-mono text-lg focus:outline-none focus:ring-2 focus:ring-blue-500"
              />
              {error && <p className="text-xs text-red-600">{error}</p>}
              <button
                type="submit"
                disabled={loading || !factorCode.trim()}
                className="w-full bg-gray-800 text-white py-2.5 rounded-lg text-sm font-semibold hover:bg-gray-900 disabled:opacity-50"
              >
                {loading ? 'Verifying…' : 'Sign In'}
              </button>
              {/* Only a toggle when there is something to toggle BETWEEN: an account whose only
                  factor is a passkey is offered recovery codes and nothing else, and switching it
                  to an authenticator it never enrolled is a dead end. */}
              {methods.includes('recovery_code') && methods.includes('totp') && (
                <button
                  type="button"
                  onClick={() => {
                    setFactorMethod(factorMethod === 'totp' ? 'recovery_code' : 'totp')
                    setFactorCode('')
                    setError(null)
                  }}
                  className="w-full text-xs text-gray-500 hover:text-gray-700"
                >
                  {factorMethod === 'totp'
                    ? 'Lost your device? Use a recovery code'
                    : '← Use your authenticator app'}
                </button>
              )}
            </form>
          </>
        )}
      </div>
    </div>
  )
}
