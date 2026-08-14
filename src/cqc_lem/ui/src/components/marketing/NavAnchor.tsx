import type { ReactNode } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { scrollToSectionSoon } from './scrollToSection'

// One in-page nav link, for a chrome that is NOT only ever rendered on the front page.
//
// Since issue #1300 the marketing nav and footer also wrap `/privacy-policy` and
// `/terms-and-conditions` for a logged-out visitor. A bare `href="#features"` there resolves
// against the CURRENT path — `/privacy-policy#features` — where no such section exists, so every
// anchor in the nav and the footer is a dead control on both legal pages. The link has to name the
// front page when we are not on it, and route there before scrolling.

export default function NavAnchor({
  href,
  onNavigate,
  className,
  children,
}: {
  /** The in-page target, written as it appears in `NAV_ANCHORS` — `#features`. */
  href: string
  /** Called on click as well, e.g. to close the mobile sheet. */
  onNavigate?: () => void
  className?: string
  children: ReactNode
}) {
  const { pathname } = useLocation()
  const navigate = useNavigate()
  const onFrontPage = pathname === '/'
  const id = href.replace(/^#/, '')

  return (
    <a
      href={onFrontPage ? href : `/${href}`}
      className={className}
      onClick={(event) => {
        onNavigate?.()
        // On the front page the browser's own hash handling is already correct — and cheaper.
        if (onFrontPage) return
        event.preventDefault()
        navigate('/')
        scrollToSectionSoon(id)
      }}
    >
      {children}
    </a>
  )
}
