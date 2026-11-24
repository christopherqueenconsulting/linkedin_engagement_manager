export interface IngestResult {
  received: number
  stored: number
  duplicate: number
  too_short_or_long: number
  over_cap: number
  embed_failed: number
  unavailable?: boolean
}

export const summarize = (r: IngestResult): string => {
  if (r.unavailable) return 'Could not read your saved examples right now — try again in a minute.'
  const skipped: string[] = []
  if (r.duplicate) skipped.push(`${r.duplicate} already saved`)
  if (r.too_short_or_long) skipped.push(`${r.too_short_or_long} too short or too long`)
  if (r.over_cap) skipped.push(`${r.over_cap} over the upload limit`)
  if (r.embed_failed) skipped.push(`${r.embed_failed} could not be processed — upload again to retry`)
  const head = `Saved ${r.stored} of ${r.received} rows.`
  return skipped.length ? `${head} Skipped: ${skipped.join(', ')}.` : head
}
