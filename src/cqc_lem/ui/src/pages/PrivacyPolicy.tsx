import { Link } from 'react-router-dom'

export default function PrivacyPolicy() {
  const year = new Date().getFullYear()

  return (
    // No nav, no <main> and no footer of its own (issue #1300): this page always renders inside a
    // chrome that supplies all three — the marketing one when nobody is signed in, the app's
    // `Layout` when someone is. Drawing them here too gave the page two stacked `sticky top-0`
    // bars, two footers and a <main> nested inside a <main>, which is the exact defect the front
    // page was rebuilt to remove.
    <div className="lem-marketing bg-page">
      <div className="max-w-3xl mx-auto px-4 py-12">
        <Link to="/" className="text-sm font-medium text-accent hover:underline">
          Back to home
        </Link>
        <h1 className="mt-6 text-section-lg font-semibold text-fg mb-2">Privacy Policy</h1>
        <p className="text-sm text-fg-muted mb-8">Last updated: {year}-07-30</p>

        <div className="max-w-measure space-y-8 text-fg leading-relaxed">
          <section>
            <h2 className="text-xl font-semibold text-fg mb-3">1. Overview</h2>
            <p>
              LinkedIn Engagement Manager (“LEM,” “we,” “us,” or “our") is operated by
              Christopher Queen Consulting. This Privacy Policy explains how we collect,
              use, store, and protect your information when you use our application,
              website, and services.
            </p>
          </section>

          <section>
            <h2 className="text-xl font-semibold text-fg mb-3">2. Information we collect</h2>
            <ul className="list-disc pl-6 space-y-2">
              <li>
                <strong>Account information:</strong> name, email address, billing details,
                and LinkedIn profile information you authorize.
              </li>
              <li>
                <strong>LinkedIn session data:</strong> encrypted session cookies and
                tokens required to perform the automation you configure. We do not store
                your LinkedIn password.
              </li>
              <li>
                <strong>Content data:</strong> posts, comments, drafts, DMs, and other
                content you create, approve, or generate through LEM.
              </li>
              <li>
                <strong>Usage data:</strong> logs of automation runs, feature usage, and
                diagnostic data used to operate and improve the service.
              </li>
            </ul>
          </section>

          <section>
            <h2 className="text-xl font-semibold text-fg mb-3">3. How we use your information</h2>
            <p>
              We use your information solely to provide and improve LEM: to authenticate
              you, run the automation you configure, generate content according to your
              preferences, send notifications, process payments, and troubleshoot the
              service. We do not sell your personal information or use your LinkedIn data
              for advertising.
            </p>
          </section>

          <section>
            <h2 className="text-xl font-semibold text-fg mb-3">4. LinkedIn integration</h2>
            <p>
              LEM acts through your own LinkedIn session with the settings and caps you
              choose. We access only the data necessary to perform actions you approve or
              configure, such as your feed, your own posts and comments, and messaging
              threads where you have directed us to engage. We do not bulk-scrape
              third-party profiles or resell LinkedIn data.
            </p>
          </section>

          <section>
            <h2 className="text-xl font-semibold text-fg mb-3">5. Data retention and deletion</h2>
            <p>
              We keep your data for as long as your account is active. You can delete your
              account at any time from the Account page; when you do, we delete or
              anonymize associated content, credentials, and logs consistent with our legal
              obligations.
            </p>
          </section>

          <section>
            <h2 className="text-xl font-semibold text-fg mb-3">6. Security</h2>
            <p>
              We use industry-standard measures to protect your data, including encryption
              for credentials and session tokens, access controls, and secure hosting
              environments. No system is completely secure; you are responsible for keeping
              your account credentials safe.
            </p>
          </section>

          <section>
            <h2 className="text-xl font-semibold text-fg mb-3">7. Cookies and analytics</h2>
            <p>
              We use cookies and similar technologies to maintain sessions, remember
              preferences, and understand product usage through privacy-preserving
              analytics. You can control cookies through your browser settings.
            </p>
          </section>

          <section>
            <h2 className="text-xl font-semibold text-fg mb-3">8. Changes to this policy</h2>
            <p>
              We may update this Privacy Policy as the service evolves. We will post the
              updated policy in the application and update the “Last updated” date above.
              Continued use after changes constitutes acceptance of the revised policy.
            </p>
          </section>

          <section>
            <h2 className="text-xl font-semibold text-fg mb-3">9. Contact us</h2>
            <p>
              If you have questions about this Privacy Policy or your data, please contact
              us through the feedback widget in LEM or at the contact information on
              <a
                href="https://christopherqueenconsulting.com"
                target="_blank"
                rel="noopener noreferrer"
                className="text-accent hover:underline"
              >
                {' '}christopherqueenconsulting.com
              </a>.
            </p>
          </section>

          <section data-testid="calls-draft">
            <h2 className="text-xl font-semibold text-fg mb-3">
              10. Calls and transcripts{' '}
              <span className="text-sm font-semibold text-danger">(DRAFT — not final)</span>
            </h2>
            <p className="mb-3 font-medium">
              DRAFT: this section is awaiting legal review and is not in force. The feature it
              describes is limited to the service owner's own account until that review is done.
            </p>
            <ul className="list-disc pl-6 space-y-2">
              <li>
                <strong>Roles:</strong> you, the LEM user, are the controller of any call or
                voice-note transcript you upload, including what it says about other people.
                Christopher Queen Consulting is your processor and acts on your instructions.
              </li>
              <li>
                <strong>Consent:</strong> you may upload a call only after confirming that
                everyone on it was told it was recorded. We do not ingest from a source without
                that confirmation.
              </li>
              <li>
                <strong>What we keep:</strong> only your own turns, with other people's names,
                email addresses, phone numbers and links replaced by placeholders before any
                model sees the text. The transcript is stored encrypted and deleted once you
                finish reviewing the call, and in every case within 7 days. Facts you accept keep
                only a short quote and your edits.
              </li>
              <li>
                <strong>Deleting:</strong> “Purge all call data” in the Story Bank removes every
                transcript, candidate, source and fact learned from calls. Encrypted database
                backups are kept for 35 days, so a deleted item can persist in a backup for up to
                that long before it is overwritten.
              </li>
              <li>
                <strong>Subprocessors (draft list):</strong> our hosting provider, and the AI
                model providers we route text through (currently OpenAI, Anthropic and
                OpenRouter). Redacted text is sent to a model only to propose facts, and is not
                used by us to train models.
              </li>
              <li>
                <strong>Recordings (draft):</strong> an audio recording is sent whole to OpenAI,
                our transcription subprocessor, because it has to be transcribed before anyone's
                words can be told apart or redacted. We then keep only your own turns, matched to
                a voice sample you enrol, and delete the recording as soon as the transcript is
                written, or within 24 hours if transcription fails. Your voice sample is stored
                as a numeric voiceprint, encrypted, and removed by “Purge all call data”.
              </li>
            </ul>
          </section>
        </div>
      </div>
    </div>
  )
}
