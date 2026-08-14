// The marketing page's in-page destinations, in the order a visitor meets them.
//
// Its own module rather than a const beside the nav component: `MarketingFooter` repeats the same
// list, and a file that exports both a component and a constant loses React Fast Refresh
// (`react-refresh/only-export-components`).
//
// `#features` and `#pricing` are not free to rename — `TUTORIAL_FLOWS['getting-started']` in
// `utilities/marketing/video_tutorials.py` navigates to both and raises on a missing anchor.
export const NAV_ANCHORS = [
  { href: '#how-it-works', label: 'How it works' },
  { href: '#features', label: 'Features' },
  { href: '#safety', label: 'Safety' },
  { href: '#pricing', label: 'Pricing' },
  { href: '#faq', label: 'FAQ' },
] as const
