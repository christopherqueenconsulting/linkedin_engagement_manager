// A quality gate's verdict on a generated post (issue #421), as returned on `gate_reason` by
// /posts/ and /user/post/rescore. `demoted` marks the findings that are actually HOLDING the post
// at PENDING — the rest are advisory notes.
//
// GENERATED from the published schema (issue #1446), not written here: `quality_gates.build_finding`
// is the only writer of a stored finding, so this type is that dict rather than a copy of it.
import type { components } from '../api/schema'

export type GateFinding = components['schemas']['GateFinding']

export function gateHold(findings?: GateFinding[] | null): GateFinding[] {
  return (findings ?? []).filter((f) => f.demoted)
}
