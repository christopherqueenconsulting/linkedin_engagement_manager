import { Outlet } from 'react-router-dom'
import MarketingFooter from './MarketingFooter'
import MarketingNav from './MarketingNav'

// The chrome a LOGGED-OUT visitor sees (issue #1300): one nav, one `<main>`, one footer, and no
// `max-w-5xl` box around the page — the app's `<main>` is what used to clip every "full-bleed"
// marketing band to 1024px with 16px of padding.
//
// The legal pages ride in here too when nobody is signed in. Clicking "Privacy Policy" mid-funnel
// used to hand a prospect the application's chrome — app nav, app footer, a narrow measure — which
// reads as having left the site.
export default function MarketingLayout() {
  return (
    <div className="min-h-screen flex flex-col bg-white">
      {/* The keyboard user's way past the nav. Visually hidden until it takes focus. */}
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:left-4 focus:top-4 focus:z-50 focus:rounded-lg focus:bg-brand-600 focus:px-4 focus:py-2 focus:text-white"
      >
        Skip to main content
      </a>
      <MarketingNav />
      <main id="main" className="flex-1">
        <Outlet />
      </main>
      <MarketingFooter />
    </div>
  )
}
