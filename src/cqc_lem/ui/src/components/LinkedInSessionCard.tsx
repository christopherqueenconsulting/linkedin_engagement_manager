import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import api from '../api/client'
import { useAuth } from '../contexts/AuthContext'
import { useStepUp } from '../hooks/useStepUp'

// Connect LinkedIn by reusing the user's existing session cookie (li_at) so automation
// resumes a trusted session instead of a password login (which trips LinkedIn's
// new-device challenge). Easiest path is the one-click browser extension.
//
// This is now the DEFAULT engagement login (issue #745, design decision 2A): a stored LinkedIn
// password stays reversible even encrypted, so `migrationNeeded` accounts are prompted once to
// move over, and saving a cookie from that prompt deletes the password instead of keeping both.
export default function LinkedInSessionCard({
  connected,
  migrationNeeded,
}: { connected?: boolean; migrationNeeded?: boolean }) {
  const { sessionToken } = useAuth()
  const queryClient = useQueryClient()
  // Storing a li_at IS handing over a LinkedIn session, so both writes below are step-up gated
  // since 2c. Minting the extension token is gated for the same reason: that token is what later
  // lets the extension post a cookie without a ceremony it could never run.
  const { guard, stepUpModal } = useStepUp()
  const [liAt, setLiAt] = useState('')
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)
  const [showSteps, setShowSteps] = useState(false)
  const [tokenCopied, setTokenCopied] = useState(false)
  const [dropPassword, setDropPassword] = useState(true)

  // Since #745 (2b) the app's own session is an httpOnly cookie, so there is no token to hand over
  // — the extension is minted its OWN session instead. That is the better shape anyway: it shows up
  // as its own device on the Security card and can be revoked without signing the person out here.
  const copyToken = async () => {
    if (!sessionToken) return
    try {
      const r = await guard(() =>
        api.post('/user/extension-token', { session_token: sessionToken }),
      )
      if (r === null) return
      await navigator.clipboard.writeText(r.data.detail.session_token as string)
      setTokenCopied(true)
      setTimeout(() => setTokenCopied(false), 2500)
    } catch {
      setMsg({ ok: false, text: 'Could not create an extension token — please try again.' })
      setTimeout(() => setMsg(null), 4000)
    }
  }

  const mutation = useMutation({
    mutationFn: () =>
      guard(() =>
        api.post('/user/linkedin-cookie', {
          session_token: sessionToken,
          li_at: liAt.trim(),
          // Only ever true from the migration prompt — a user without a stored password has
          // nothing to drop, and one who keeps the box unchecked keeps their fallback login.
          drop_password: !!migrationNeeded && dropPassword,
        }),
      ),
    onSuccess: (result) => {
      if (result === null) return
      setLiAt('')
      setMsg({
        ok: true,
        text: migrationNeeded && dropPassword
          ? 'LinkedIn session saved and your stored password was deleted.'
          : 'LinkedIn session saved. Automation will reuse it.',
      })
      queryClient.invalidateQueries({ queryKey: ['account-readiness'] })
      setTimeout(() => setMsg(null), 5000)
    },
    onError: () => {
      setMsg({ ok: false, text: 'Could not save — paste the full li_at cookie value.' })
      setTimeout(() => setMsg(null), 6000)
    },
  })

  return (
    <div className="bg-white rounded-lg shadow-sm border border-gray-200 p-6 space-y-4">
      {stepUpModal}
      <div>
        <h2 className="text-base font-semibold text-gray-700">
          LinkedIn Session <span className="text-red-500">*</span>
          <span className="ml-2 text-[11px] font-semibold text-red-600 bg-red-50 px-2 py-0.5 rounded">
            Required
          </span>
        </h2>
        <p className="text-xs text-gray-500 mt-1">
          Lets LEM resume your existing LinkedIn session instead of logging in with a
          password — which avoids LinkedIn's "Check your app" security challenge. The
          easiest way is the one-click browser extension; or paste your <code>li_at</code>{' '}
          cookie value below.
        </p>
      </div>

      {migrationNeeded && (
        <div
          data-testid="cookie-migration-notice"
          className="rounded-lg border border-amber-300 bg-amber-50 p-4 space-y-2"
        >
          <p className="text-sm font-semibold text-amber-900">
            Switch to session-only login
          </p>
          <p className="text-xs text-amber-900/90">
            Your account still logs in with a saved LinkedIn password. A password has to be stored
            reversibly for automation to type it, so connecting a session cookie instead is safer —
            you can revoke it any time from LinkedIn's “Sign out of all sessions”.
          </p>
          <label className="flex items-center gap-2 text-xs text-amber-900">
            <input
              type="checkbox"
              checked={dropPassword}
              onChange={(e) => setDropPassword(e.target.checked)}
              className="rounded border-amber-400"
            />
            Delete my saved LinkedIn password once the session is saved
          </label>
        </div>
      )}

      {/* Recommended path: the browser extension grabs the httpOnly li_at cookie the paste
          flow can't reach, and sends it in one click. */}
      <div className="rounded-lg border border-blue-200 bg-blue-50 p-4 space-y-3">
        <div className="flex items-center justify-between gap-3">
          <div>
            <p className="text-sm font-semibold text-blue-900">Recommended: one-click extension</p>
            <p className="text-xs text-blue-800/80">
              Grabs your LinkedIn session automatically — no DevTools, no cookie hunting.
            </p>
          </div>
          <a
            href="/api/extension/linkedin-connect.zip"
            className="shrink-0 bg-blue-600 text-white px-3 py-2 rounded-lg text-xs font-semibold hover:bg-blue-700 transition-colors"
          >
            Download extension
          </a>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={copyToken}
            disabled={!sessionToken}
            className="bg-white border border-blue-300 text-blue-700 px-3 py-1.5 rounded-lg text-xs font-semibold hover:bg-blue-100 disabled:opacity-50 transition-colors"
          >
            {tokenCopied ? '✓ Token copied' : 'Copy my LEM token'}
          </button>
          <span className="text-[11px] text-blue-800/70">
            Paste this into the extension once, then click Connect.
          </span>
        </div>

        <button
          type="button"
          onClick={() => setShowSteps((s) => !s)}
          className="text-xs font-medium text-blue-700 hover:underline"
        >
          {showSteps ? 'Hide install steps' : 'How to install (30 seconds)'}
        </button>
        {showSteps && (
          <ol className="list-decimal list-inside text-xs text-blue-900/90 space-y-1">
            <li>Download the extension above and unzip it.</li>
            <li>Open <code>chrome://extensions</code> (or <code>edge://extensions</code>).</li>
            <li>Turn on <strong>Developer mode</strong> (top-right), then click <strong>Load unpacked</strong> and pick the unzipped folder.</li>
            <li>Open <code>linkedin.com</code> and stay signed in.</li>
            <li>Click the <strong>LEM LinkedIn Connect</strong> extension, paste your LEM token (copied above), and click <strong>Connect</strong>.</li>
          </ol>
        )}
      </div>

      {connected && (
        <div className="flex items-center gap-2 text-sm text-green-700">
          <span className="w-2.5 h-2.5 rounded-full bg-green-500" /> A session is saved.
          Re-paste only if automation reports it disconnected.
        </div>
      )}

      <div>
        <label htmlFor="li-at-input" className="block text-sm font-medium text-gray-700 mb-1">
          li_at cookie value
        </label>
        <input
          id="li-at-input"
          type="password"
          value={liAt}
          onChange={(e) => setLiAt(e.target.value)}
          className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
          placeholder="paste your LinkedIn li_at cookie value"
          autoComplete="off"
        />
        <p className="text-xs text-gray-400 mt-1">
          DevTools (F12) → Application → Cookies → https://www.linkedin.com → copy the value of{' '}
          <code>li_at</code>. This is sensitive — treat it like a password.
        </p>
      </div>

      {msg && (
        <p className={`text-sm font-medium ${msg.ok ? 'text-green-600' : 'text-red-600'}`}>
          {msg.text}
        </p>
      )}

      <button
        type="button"
        onClick={() => mutation.mutate()}
        disabled={mutation.isPending || liAt.trim().length < 20}
        className="w-full bg-blue-600 text-white py-2 rounded-lg text-sm font-semibold hover:bg-blue-700 disabled:opacity-50 transition-colors"
      >
        {mutation.isPending ? 'Saving…' : 'Save LinkedIn Session'}
      </button>
    </div>
  )
}
