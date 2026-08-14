// The trademark + non-affiliation line (issue #1300).
//
// It lives in its own component because the failure mode is placing it on ONE surface: a
// disclaimer that appears on the marketing page and nowhere else reads as decoration rather than a
// statement, and the app chrome and the legal pages are where a user is most likely to look for it.
// `components/Footer.tsx` (every in-app page, including /privacy-policy and /terms-and-conditions)
// and `MarketingFooter` both render this exact text.
export const TRADEMARK_NOTICE =
  'LinkedIn® is a registered trademark of LinkedIn Corporation. LinkedIn Engagement Manager is an ' +
  'independent product and is not affiliated with, endorsed by, or sponsored by LinkedIn.'

export default function LegalDisclaimer({ className = '' }: { className?: string }) {
  return <p className={`text-xs leading-relaxed ${className}`.trim()}>{TRADEMARK_NOTICE}</p>
}
