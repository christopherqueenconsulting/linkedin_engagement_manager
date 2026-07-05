// Only http(s) values are safe to render as a clickable link. The API blanks non-http post_url
// values (e.g. the synthetic "feedpost://<hash>" dedup key for permalink-less feed comments), so
// this narrows post_url to a real URL string for the link branch.
export function isHttpUrl(value: string | null | undefined): value is string {
  return typeof value === 'string' && /^https?:\/\//i.test(value)
}

// Home-feed comments have no per-post permalink. Fall back to the user's own LinkedIn
// "recent activity → comments" page, where all their comments live. Given a stored profile
// URL like "https://www.linkedin.com/in/christopherqueen/" this returns
// "https://www.linkedin.com/in/christopherqueen/recent-activity/comments/". Returns null if the
// input isn't a parseable /in/<vanity> LinkedIn URL.
export function commentsActivityUrl(linkedinProfileUrl: string | null | undefined): string | null {
  if (typeof linkedinProfileUrl !== 'string') return null
  const match = linkedinProfileUrl.match(/^https?:\/\/([\w.-]*\.)?linkedin\.com\/in\/([^/?#]+)/i)
  if (!match) return null
  const vanity = match[2]
  if (!vanity) return null
  return `https://www.linkedin.com/in/${vanity}/recent-activity/comments/`
}
