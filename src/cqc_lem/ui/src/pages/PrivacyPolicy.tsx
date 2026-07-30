import { Link } from 'react-router-dom'

export default function PrivacyPolicy() {
  const year = new Date().getFullYear()

  return (
    <div className="min-h-screen bg-gray-50">
      <nav className="bg-white border-b border-gray-200 sticky top-0 z-10">
        <div className="max-w-5xl mx-auto px-4 flex items-center justify-between h-14">
          <Link to="/" className="font-bold text-blue-600 text-lg">
            LEM
          </Link>
          <Link
            to="/"
            className="text-sm font-medium text-gray-600 hover:text-gray-900"
          >
            Back to home
          </Link>
        </div>
      </nav>

      <main className="max-w-3xl mx-auto px-4 py-12">
        <h1 className="text-3xl font-bold text-gray-900 mb-2">Privacy Policy</h1>
        <p className="text-sm text-gray-500 mb-8">Last updated: {year}-07-30</p>

        <div className="space-y-8 text-gray-700 leading-relaxed">
          <section>
            <h2 className="text-xl font-semibold text-gray-900 mb-3">1. Overview</h2>
            <p>
              LinkedIn Engagement Manager (“LEM,” “we,” “us,” or “our") is operated by
              Christopher Queen Consulting. This Privacy Policy explains how we collect,
              use, store, and protect your information when you use our application,
              website, and services.
            </p>
          </section>

          <section>
            <h2 className="text-xl font-semibold text-gray-900 mb-3">2. Information we collect</h2>
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
            <h2 className="text-xl font-semibold text-gray-900 mb-3">3. How we use your information</h2>
            <p>
              We use your information solely to provide and improve LEM: to authenticate
              you, run the automation you configure, generate content according to your
              preferences, send notifications, process payments, and troubleshoot the
              service. We do not sell your personal information or use your LinkedIn data
              for advertising.
            </p>
          </section>

          <section>
            <h2 className="text-xl font-semibold text-gray-900 mb-3">4. LinkedIn integration</h2>
            <p>
              LEM acts through your own LinkedIn session with the settings and caps you
              choose. We access only the data necessary to perform actions you approve or
              configure, such as your feed, your own posts and comments, and messaging
              threads where you have directed us to engage. We do not bulk-scrape
              third-party profiles or resell LinkedIn data.
            </p>
          </section>

          <section>
            <h2 className="text-xl font-semibold text-gray-900 mb-3">5. Data retention and deletion</h2>
            <p>
              We keep your data for as long as your account is active. You can delete your
              account at any time from the Account page; when you do, we delete or
              anonymize associated content, credentials, and logs consistent with our legal
              obligations.
            </p>
          </section>

          <section>
            <h2 className="text-xl font-semibold text-gray-900 mb-3">6. Security</h2>
            <p>
              We use industry-standard measures to protect your data, including encryption
              for credentials and session tokens, access controls, and secure hosting
              environments. No system is completely secure; you are responsible for keeping
              your account credentials safe.
            </p>
          </section>

          <section>
            <h2 className="text-xl font-semibold text-gray-900 mb-3">7. Cookies and analytics</h2>
            <p>
              We use cookies and similar technologies to maintain sessions, remember
              preferences, and understand product usage through privacy-preserving
              analytics. You can control cookies through your browser settings.
            </p>
          </section>

          <section>
            <h2 className="text-xl font-semibold text-gray-900 mb-3">8. Changes to this policy</h2>
            <p>
              We may update this Privacy Policy as the service evolves. We will post the
              updated policy in the application and update the “Last updated” date above.
              Continued use after changes constitutes acceptance of the revised policy.
            </p>
          </section>

          <section>
            <h2 className="text-xl font-semibold text-gray-900 mb-3">9. Contact us</h2>
            <p>
              If you have questions about this Privacy Policy or your data, please contact
              us through the feedback widget in LEM or at the contact information on
              <a
                href="https://christopherqueenconsulting.com"
                target="_blank"
                rel="noopener noreferrer"
                className="text-blue-600 hover:underline"
              >
                {' '}christopherqueenconsulting.com
              </a>.
            </p>
          </section>
        </div>
      </main>

      <footer className="bg-white border-t border-gray-200 mt-12">
        <div className="max-w-5xl mx-auto px-4 py-6 text-center text-sm text-gray-500">
          © {year} Christopher Queen Consulting. All rights reserved.
        </div>
      </footer>
    </div>
  )
}
