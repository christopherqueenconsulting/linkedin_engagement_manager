import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import api from '../../api/client'
import { useAuth } from '../../contexts/AuthContext'
import SettingsCard from '../../components/SettingsCard'

type SessionRow = {
  id: number
  label: string
  created_at: string | null
  last_seen_at: string | null
  expires_at: string | null
  is_current: boolean
}

type AuthEvent = {
  event: string
  success: boolean
  user_agent: string | null
  created_at: string | null
}

type SecurityDetail = {
  public_uid: string | null
  email: string
  sessions: SessionRow[]
  recent_events: AuthEvent[]
}

const EVENT_LABELS: Record<string, string> = {
  login_success: 'Signed in',
  login_failed: 'Failed sign-in',
  login_rate_limited: 'Sign-in rate limited',
  pin_locked: 'PIN locked',
  logout: 'Signed out',
  session_revoked: 'Device signed out',
  sessions_revoked_all: 'All other devices signed out',
  email_change_requested: 'Email change requested',
  email_changed: 'Email changed',
}

function when(value: string | null): string {
  if (!value) return '—'
  const d = new Date(value)
  return Number.isNaN(d.getTime()) ? '—' : d.toLocaleString()
}

/**
 * Security: the devices holding a session, the recent auth history, and the email attached to the
 * account (issue #745, phase 2b).
 *
 * The point of the card is the two things a person can actually DO when something looks wrong:
 * sign a device out, and take their email address back. Nothing here ever renders a session token
 * — the API returns row ids and labels only.
 */
export default function SecurityCard() {
  const { sessionToken } = useAuth()
  const queryClient = useQueryClient()
  const [newEmail, setNewEmail] = useState('')
  const [pin, setPin] = useState('')
  const [pinSent, setPinSent] = useState(false)
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)

  const flash = (ok: boolean, text: string) => {
    setMsg({ ok, text })
    setTimeout(() => setMsg(null), 5000)
  }

  const { data } = useQuery({
    queryKey: ['user-security', sessionToken],
    queryFn: () =>
      api
        .get(`/user/security?session_token=${encodeURIComponent(sessionToken!)}`)
        .then((r) => r.data.detail as SecurityDetail),
    enabled: !!sessionToken,
    staleTime: 30 * 1000,
  })

  const revoke = useMutation({
    mutationFn: (body: { session_id?: number; all_others?: boolean }) =>
      api.post('/user/sessions/revoke', { session_token: sessionToken, ...body }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['user-security'] })
      flash(true, 'Signed out.')
    },
    onError: () => flash(false, 'Could not sign that device out — please try again.'),
  })

  const changeInit = useMutation({
    mutationFn: () =>
      api.post('/user/email/change/init', { session_token: sessionToken, new_email: newEmail.trim() }),
    onSuccess: () => {
      setPinSent(true)
      flash(true, `We sent a confirmation code to ${newEmail.trim()}.`)
    },
    onError: (e: { response?: { data?: { detail?: string } } }) =>
      flash(false, e?.response?.data?.detail || 'Could not start the email change.'),
  })

  const changeVerify = useMutation({
    mutationFn: () =>
      api.post('/user/email/change/verify', {
        session_token: sessionToken,
        new_email: newEmail.trim(),
        pin: pin.trim(),
      }),
    onSuccess: () => {
      setPinSent(false)
      setNewEmail('')
      setPin('')
      queryClient.invalidateQueries({ queryKey: ['user-security'] })
      flash(true, 'Email updated. Every other device was signed out.')
    },
    onError: (e: { response?: { data?: { detail?: string } } }) =>
      flash(false, e?.response?.data?.detail || 'That code did not work.'),
  })

  const sessions = data?.sessions ?? []
  const otherSessions = sessions.filter((s) => !s.is_current).length

  return (
    <SettingsCard
      title="Security"
      subtitle="Where you're signed in, what's happened on your account, and the email address it answers to."
    >
      <div>
        <h4 className="text-sm font-semibold text-gray-800 mb-2">Signed-in devices</h4>
        {sessions.length === 0 ? (
          <p className="text-xs text-gray-500">No active sessions to show.</p>
        ) : (
          <ul className="divide-y divide-gray-100 border border-gray-200 rounded-lg">
            {sessions.map((s) => (
              <li key={s.id} className="flex items-center justify-between gap-3 px-3 py-2">
                <div className="min-w-0">
                  <p className="text-sm font-medium text-gray-800 truncate">
                    {s.label}
                    {s.is_current && (
                      <span className="ml-2 text-[11px] font-semibold text-green-700 bg-green-50 px-2 py-0.5 rounded">
                        This device
                      </span>
                    )}
                  </p>
                  <p className="text-xs text-gray-500">Last used {when(s.last_seen_at ?? s.created_at)}</p>
                </div>
                <button
                  type="button"
                  onClick={() => revoke.mutate({ session_id: s.id })}
                  disabled={revoke.isPending}
                  className="text-xs font-semibold text-red-600 hover:underline disabled:opacity-50 whitespace-nowrap"
                >
                  Sign out
                </button>
              </li>
            ))}
          </ul>
        )}
        {otherSessions > 0 && (
          <button
            type="button"
            onClick={() => revoke.mutate({ all_others: true })}
            disabled={revoke.isPending}
            className="mt-2 text-xs font-semibold text-blue-600 hover:underline disabled:opacity-50"
          >
            Sign out all other devices ({otherSessions})
          </button>
        )}
      </div>

      <div>
        <h4 className="text-sm font-semibold text-gray-800 mb-2">Email address</h4>
        <p className="text-xs text-gray-500 mb-2">
          Your account is identified internally, not by this address — changing it keeps everything
          you've created. We send a code to the new address first, and signing in there is what
          completes the move. Every other device is signed out when it does.
        </p>
        <div className="flex flex-col sm:flex-row gap-2">
          <input
            type="email"
            aria-label="New email address"
            value={newEmail}
            onChange={(e) => setNewEmail(e.target.value)}
            placeholder={data?.email || 'you@example.com'}
            className="flex-1 border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
          />
          <button
            type="button"
            onClick={() => changeInit.mutate()}
            disabled={changeInit.isPending || !newEmail.trim()}
            className="bg-blue-600 text-white px-4 py-2 rounded-lg text-sm font-semibold hover:bg-blue-700 disabled:opacity-50"
          >
            {changeInit.isPending ? 'Sending…' : 'Send code'}
          </button>
        </div>
        {pinSent && (
          <div className="flex flex-col sm:flex-row gap-2 mt-2">
            <input
              type="text"
              inputMode="numeric"
              aria-label="Confirmation code"
              value={pin}
              onChange={(e) => setPin(e.target.value)}
              placeholder="6-digit code"
              className="flex-1 border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
            <button
              type="button"
              onClick={() => changeVerify.mutate()}
              disabled={changeVerify.isPending || !pin.trim()}
              className="bg-gray-800 text-white px-4 py-2 rounded-lg text-sm font-semibold hover:bg-gray-900 disabled:opacity-50"
            >
              {changeVerify.isPending ? 'Confirming…' : 'Confirm change'}
            </button>
          </div>
        )}
      </div>

      {(data?.recent_events?.length ?? 0) > 0 && (
        <div>
          <h4 className="text-sm font-semibold text-gray-800 mb-2">Recent activity</h4>
          <ul className="space-y-1">
            {data!.recent_events.slice(0, 8).map((e, i) => (
              <li key={i} className="text-xs text-gray-600 flex justify-between gap-3">
                <span className={e.success ? '' : 'text-red-600 font-medium'}>
                  {EVENT_LABELS[e.event] ?? e.event}
                </span>
                <span className="text-gray-400 whitespace-nowrap">{when(e.created_at)}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {msg && (
        <p className={`text-sm font-medium ${msg.ok ? 'text-green-600' : 'text-red-600'}`}>{msg.text}</p>
      )}
    </SettingsCard>
  )
}
