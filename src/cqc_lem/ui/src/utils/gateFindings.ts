// A quality gate's verdict on a generated post (issue #421), as returned on `gate_reason` by
// /posts/ and /user/post/rescore. `demoted` marks the findings that are actually HOLDING the post
// at PENDING — the rest are advisory notes.
export type GateFinding = {
  gate: string
  label: string
  score: number | null
  threshold: number | null
  demoted: boolean
  explanation: string
  remediation: string
  details?: string[]
}

export function gateHold(findings?: GateFinding[] | null): GateFinding[] {
  return (findings ?? []).filter((f) => f.demoted)
}
