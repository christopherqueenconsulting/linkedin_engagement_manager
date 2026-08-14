import { Link } from 'react-router-dom'

export default function TermsAndConditions() {
  const year = new Date().getFullYear()

  return (
    // No nav, no <main> and no footer of its own (issue #1300): this page always renders inside a
    // chrome that supplies all three — the marketing one when nobody is signed in, the app's
    // `Layout` when someone is. Drawing them here too gave the page two stacked `sticky top-0`
    // bars, two footers and a <main> nested inside a <main>, which is the exact defect the front
    // page was rebuilt to remove.
    <div className="bg-gray-50">
      <div className="max-w-3xl mx-auto px-4 py-12">
        <Link to="/" className="text-sm font-medium text-blue-600 hover:underline">
          Back to home
        </Link>
        <h1 className="mt-6 text-3xl font-bold text-gray-900 mb-2">Terms and Conditions</h1>
        <p className="text-sm text-gray-500 mb-8">Last updated: {year}-07-30</p>

        <div className="space-y-8 text-gray-700 leading-relaxed">
          <section>
            <h2 className="text-xl font-semibold text-gray-900 mb-3">1. Acceptance of terms</h2>
            <p>
              By accessing or using LinkedIn Engagement Manager (“LEM”), you agree to be bound
              by these Terms and Conditions and our Privacy Policy. If you do not agree,
              do not use the service.
            </p>
          </section>

          <section>
            <h2 className="text-xl font-semibold text-gray-900 mb-3">2. Description of service</h2>
            <p>
              LEM is an AI-assisted tool that helps users create, schedule, and publish
              content on LinkedIn, and that automates certain engagement actions according
              to the targeting, tone, and per-day limits you set. LEM operates through your
              own LinkedIn account; it does not replace your judgment or responsibility for
              what is posted.
            </p>
          </section>

          <section>
            <h2 className="text-xl font-semibold text-gray-900 mb-3">3. LinkedIn terms and your responsibilities</h2>
            <p>
              LinkedIn’s User Agreement, Privacy Policy, and Professional Community Policies
              apply to any activity LEM performs on your behalf. You are solely responsible
              for complying with those terms. You may use LEM only for lawful purposes and in
              a way that does not infringe the rights of others, interfere with LinkedIn’s
              services, or violate LinkedIn’s rules.
            </p>
            <p className="mt-2">
              You are responsible for reviewing, approving, or rejecting AI-generated
              content and automation settings. We strongly recommend keeping caps and
              activity at human-like levels and pausing automation if LinkedIn flags,
              limits, or restricts your account.
            </p>
          </section>

          <section>
            <h2 className="text-xl font-semibold text-gray-900 mb-3">4. Account and security</h2>
            <p>
              You must provide accurate account information and keep your credentials
              secure. You are responsible for all activity that occurs under your account.
              Notify us promptly of any unauthorized use.
            </p>
          </section>

          <section>
            <h2 className="text-xl font-semibold text-gray-900 mb-3">5. Subscription, billing, and cancellation</h2>
            <p>
              LEM offers subscription plans with a free trial. Billing terms are presented at
              signup. You may cancel at any time; cancellation takes effect at the end of the
              current billing period. No refunds for partial months unless required by law.
            </p>
          </section>

          <section>
            <h2 className="text-xl font-semibold text-gray-900 mb-3">6. Acceptable use</h2>
            <p>You agree not to use LEM to:</p>
            <ul className="list-disc pl-6 space-y-2 mt-2">
              <li>spam, harass, or send unsolicited messages at scale;</li>
              <li>scrape, collect, or resell LinkedIn member data without consent;</li>
              <li>publish hate speech, defamatory content, illegal material, or misinformation;</li>
              <li>circumvent LinkedIn security, rate limits, or anti-automation measures;</li>
              <li>impersonate any person or misrepresent your identity.</li>
            </ul>
          </section>

          <section>
            <h2 className="text-xl font-semibold text-gray-900 mb-3">7. Intellectual property</h2>
            <p>
              You retain ownership of content you create and publish through LEM. We grant
              you a limited, non-exclusive, non-transferable license to use the LEM software
              during your subscription. LEM and its underlying code, design, and trademarks
              remain our property.
            </p>
          </section>

          <section>
            <h2 className="text-xl font-semibold text-gray-900 mb-3">8. Limitation of liability</h2>
            <p>
              LEM is provided “as is” without warranties of any kind. To the maximum extent
              permitted by law, Christopher Queen Consulting is not liable for indirect,
              incidental, consequential, or punitive damages arising from your use of the
              service, including any action LinkedIn takes against your account.
            </p>
          </section>

          <section>
            <h2 className="text-xl font-semibold text-gray-900 mb-3">9. Modifications to terms</h2>
            <p>
              We may update these Terms from time to time. Material changes will be
              communicated through the application or by email. Continued use after changes
              constitutes acceptance of the revised Terms.
            </p>
          </section>

          <section>
            <h2 className="text-xl font-semibold text-gray-900 mb-3">10. Governing law and contact</h2>
            <p>
              These Terms are governed by the laws of the State of Texas, USA, without
              regard to conflict-of-laws principles. For questions, contact us through the
              feedback widget in LEM or at the contact information on
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
      </div>
    </div>
  )
}
