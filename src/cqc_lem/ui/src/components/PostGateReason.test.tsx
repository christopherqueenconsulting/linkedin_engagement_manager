import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import PostGateReason from './PostGateReason'
import type { GateFinding } from '../utils/gateFindings'

// The review queue has to say WHY a video post is held (issue #1402). A rejected video file is
// never stored, so the missing-media hold on its own reads as "the render failed" — the probe's
// reason is the only thing that says the file arrived and was unusable.

const MISSING: GateFinding = {
  gate: 'missing_asset',
  label: 'Missing media',
  score: null,
  threshold: null,
  demoted: true,
  explanation: 'This video post has no video yet, so it cannot be published as-is.',
  remediation: 'Wait for the media backfill to finish, re-generate the post, or switch it to a text post.',
  details: [],
}

const MALFORMED: GateFinding = {
  gate: 'malformed_asset',
  label: 'Unusable media file',
  score: null,
  threshold: null,
  demoted: false,
  explanation: "This video post's video failed the probe: zero-byte file.",
  remediation: 'Wait for the media backfill to retry, re-generate the post, or replace the video manually.',
  details: ['zero-byte file'],
}

function harness(findings: GateFinding[]) {
  return render(
    <MemoryRouter>
      <PostGateReason findings={findings} status="pending" />
    </MemoryRouter>
  )
}

afterEach(cleanup)

describe('PostGateReason — malformed media (issue #1402)', () => {
  it('renders the probe reason next to the missing-media hold', () => {
    const { container } = harness([MISSING, MALFORMED])
    expect(container.textContent).toContain('Unusable media file')
    expect(container.textContent).toContain('failed the probe: zero-byte file')
    expect(container.textContent).toContain('replace the video manually')
  })

  it('marks the probe reason advisory, so the hold still reads as the missing media', () => {
    const { container } = harness([MISSING, MALFORMED])
    expect(container.textContent).toContain('advisory')
    // The header names the hold, not the note.
    expect(screen.getByText('⏸ Why this is pending')).toBeTruthy()
  })

  it('shows a demoting probe reason without the advisory marker (VIDEO_PROBE_ENABLED)', () => {
    const { container } = harness([{ ...MALFORMED, demoted: true }])
    expect(container.textContent).toContain('Unusable media file')
    expect(container.textContent).not.toContain('advisory')
  })
})

// A deck's off-topic hold (issue #2106) carries a 0-1 overlap score. Rounded as a 0-100 score it
// rendered "score 0 · your limit 0" — a hold that reads as passing.
describe('PostGateReason — deck off-topic hold (issue #2106)', () => {
  it('renders the topic overlap as a percentage, not a rounded-to-zero score', () => {
    const { container } = harness([
      {
        gate: 'deck_off_topic',
        label: "Slides off the post's topic",
        score: 0.0175,
        threshold: 0.15,
        demoted: true,
        explanation: 'The slides share 2% of their topic vocabulary with the post text.',
        remediation: 'Regenerate the carousel.',
        details: [],
      },
    ])
    expect(container.textContent).toContain('score 2%')
    expect(container.textContent).toContain('your limit 15%')
  })
})
