// Only http(s) values are safe to render as a clickable link. Legacy activity rows may carry a
// synthetic dedup key (e.g. "feedpost://<hash>") as post_url — those must render as plain text.
export function isHttpUrl(value: string | null | undefined): boolean {
  return typeof value === 'string' && /^https?:\/\//i.test(value)
}
