import { useEngagementPrefs } from './EngagementPrefsContext'

// The one-time email-forwarding setup for event-driven replies, promoted to a status chip so
// "forwarding was never confirmed" stops being a silent failure (conflict C6).
export default function ReplySetupSteps() {
  const { eng } = useEngagementPrefs()
  if (!eng) return null
  const confirmation = eng.gmail_forward_confirmation
  // Three states, not two (issue #813). "Pending" is the user who added the address and is waiting
  // on the first forwarded email — telling them it is "not confirmed" reads as "your setup failed".
  const state = confirmation?.confirmed ? 'confirmed' : confirmation ? 'pending' : 'missing'
  const chip = {
    confirmed: { text: '✓ Forwarding confirmed', className: 'bg-green-100 text-green-800' },
    pending: { text: 'Waiting for your first forwarded email', className: 'bg-amber-100 text-amber-800' },
    missing: { text: 'Forwarding not confirmed yet', className: 'bg-amber-100 text-amber-800' },
  }[state]

  return (
    <div className="text-xs text-gray-500 space-y-3">
      <div className="flex items-center gap-2">
        <span
          data-testid="forwarding-status"
          className={`inline-block rounded-full px-2 py-0.5 text-[11px] font-semibold ${chip.className}`}
        >
          {chip.text}
        </span>
      </div>
      {state === 'confirmed' && confirmation?.source === 'forwarded_email' && (
        <p className="text-green-700">
          A LinkedIn notification reached your forwarding address, so the whole chain is working.
        </p>
      )}
      <p className="text-gray-600">
        We reply the moment LinkedIn emails you about a comment — no browser polling, so it can't trip
        LinkedIn's rate limits. One-time setup, about 2 minutes:
      </p>

      <div>
        <p className="font-medium text-gray-700">1. Turn on LinkedIn email notifications for comments</p>
        <p>
          On LinkedIn: <span className="text-gray-600">Me → Settings &amp; Privacy → Notifications →
          "Posts, comments and mentions"</span>, and make sure <span className="font-medium">Email</span> is
          ON for comments &amp; replies on your posts. Without this, LinkedIn never sends the email we listen for.
        </p>
      </div>

      <div>
        <p className="font-medium text-gray-700">2. Copy your personal forwarding address</p>
        {eng.reply_inbound_address ? (
          <div className="flex items-center gap-2 mt-1">
            <code className="bg-gray-100 rounded px-2 py-1 text-gray-700 break-all">{eng.reply_inbound_address}</code>
            <button type="button" onClick={() => navigator.clipboard?.writeText(eng.reply_inbound_address || '')}
              className="text-blue-600 hover:underline shrink-0">Copy</button>
          </div>
        ) : (
          <p className="text-gray-400">Save your settings once to generate your address.</p>
        )}
        <p className="mt-1 text-gray-400">Keep this private — it's unique to your account.</p>
      </div>

      <div>
        <p className="font-medium text-gray-700">3. Auto-forward those emails to it (Gmail)</p>
        <p>
          In Gmail: <span className="text-gray-600">Settings → Forwarding and POP/IMAP → Add a forwarding
          address</span> (paste the address above and confirm it). Then <span className="text-gray-600">Settings →
          Filters and Blocked Addresses → Create a new filter</span> with
          From <code className="bg-gray-100 rounded px-1">linkedin.com</code> and
          Subject <code className="bg-gray-100 rounded px-1">commented OR replied</code>, click
          <span className="italic"> Create filter</span>, then check
          <span className="italic"> "Forward it to"</span> and pick your address. Using another provider?
          Any rule that forwards LinkedIn comment emails to the address works.
        </p>
        <p className="mt-1 text-green-700">
          When Gmail sends its "verify permission" confirmation to that address, we confirm it for you
          automatically — no need to fish out the code.
        </p>
        {confirmation && !confirmation.confirmed && confirmation.code && (
          <p className="mt-1 text-amber-600">
            We received Gmail's confirmation but couldn't auto-click it. If forwarding still shows
            "pending" in Gmail, enter this code there:{' '}
            <code className="bg-gray-100 rounded px-1">{confirmation.code}</code>
          </p>
        )}
      </div>
    </div>
  )
}
