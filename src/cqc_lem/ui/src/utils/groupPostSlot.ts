// The weekly group-post beat publishes on Tuesdays at 15:00 UTC (issue #932). Given a reference
// instant, return the next occurrence of that slot so the UI can show when a queued draft ships.
export function nextGroupPublishSlot(from: Date = new Date()): Date {
  const base = new Date(from)
  const candidate = new Date(
    Date.UTC(base.getUTCFullYear(), base.getUTCMonth(), base.getUTCDate(), 15, 0, 0, 0)
  )
  const day = candidate.getUTCDay()
  const daysUntilTuesday = (2 - day + 7) % 7
  candidate.setUTCDate(candidate.getUTCDate() + daysUntilTuesday)
  if (candidate.getTime() <= base.getTime()) {
    candidate.setUTCDate(candidate.getUTCDate() + 7)
  }
  return candidate
}
