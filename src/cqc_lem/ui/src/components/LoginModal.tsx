import { useState } from 'react'
import { useAuth } from '../contexts/AuthContext'
import api from '../api/client'
import { getAttribution } from '../utils/attribution'
import { recordSignup } from '../utils/analytics'

type Step = 'email' | 'pin'

export default function LoginModal() {
  const { closeLoginModal, login } = useAuth()
  const [step, setStep] = useState<Step>('email')
  const [email, setEmail] = useState('')
  const [pin, setPin] = useState('')
  const [isNewUser, setIsNewUser] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

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

      if (detail.bypass) {
        // No email provider — session already created server-side, log in immediately
        if (detail.is_new_user) recordSignup({ method: 'email_pin', pin_bypassed: true })
        login(detail.session_token, detail.email)
        return
      }

      setIsNewUser(detail.user_exists === false)
      setStep('pin')
    } catch (err: unknown) {
      const msg = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      setError(msg ?? 'Failed to send code. Please try again.')
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
      const { session_token, email: verifiedEmail, is_new_user } = r.data.detail
      // Only a brand-new account is a signup conversion — a returning user re-authenticating is a
      // login, the same line the API's own funnel event draws (issue #658).
      if (is_new_user) recordSignup({ method: 'email_pin', pin_bypassed: false })
      login(session_token, verifiedEmail)
    } catch (err: unknown) {
      const msg = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      setError(msg ?? 'Invalid or expired code. Please try again.')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 px-4"
      onClick={(e) => { if (e.target === e.currentTarget) closeLoginModal() }}
    >
      <div className="bg-white rounded-xl shadow-xl w-full max-w-md p-8 relative">
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
      </div>
    </div>
  )
}
