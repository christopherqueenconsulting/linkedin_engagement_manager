// Topic Authority (Topic DNA) scoring — issue #384. Lives apart from the meter component that
// renders it so the component file exports a component and nothing else (Fast Refresh keeps
// working, and the score is importable without pulling a React tree in).
// The score MIRRORS the backend deterministic heuristic in content_alignment.topic_authority_score
// (token-set overlap coefficient) so the meter roughly agrees with the server-side governor.

// Kept in sync with content_framework._STOPWORDS — meaningful-token extraction must match the
// backend or the meter would disagree with the governor that actually flags off-niche posts.
const STOPWORDS = new Set(
  ('a an and are as at be been but by can could did do does for from had has have he her here his ' +
    'how i if in into is it its just me more most my no nor not of on or our out over she so some ' +
    'than that the their them then there these they this those to too up us was we were what when ' +
    'where which who why will with would you your').split(' '),
)

function tokens(text: string): Set<string> {
  const out = new Set<string>()
  for (const w of (text || '').toLowerCase().match(/[a-z0-9']+/g) || []) {
    if (w.length > 1 && !STOPWORDS.has(w)) out.add(w)
  }
  return out
}

// Matches content_alignment.TOPIC_AUTHORITY_MIN_DEFAULT.
export const OFF_NICHE_THRESHOLD = 0.15

// Overlap coefficient |content∩dna| / min(|content|,|dna|) — same measure as the Python scorer.
export function topicAuthorityScore(
  content: string,
  focusTopics: string[],
  headline?: string,
  about?: string,
): number {
  const dna = new Set<string>()
  for (const t of focusTopics || []) for (const w of tokens(t)) dna.add(w)
  for (const w of tokens(headline || '')) dna.add(w)
  for (const w of tokens(about || '')) dna.add(w)
  const ctokens = tokens(content)
  if (dna.size === 0 || ctokens.size === 0) return 1
  let hits = 0
  for (const w of ctokens) if (dna.has(w)) hits += 1
  return hits / Math.min(ctokens.size, dna.size)
}
