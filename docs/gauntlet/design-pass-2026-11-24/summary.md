# Gauntlet record: landing design pass, 2026-11-24

- PR: https://github.com/christopherqueenconsulting/linkedin_engagement_manager/pull/2792 (draft)
- Profile: `product-content`, reviewers D (claims auditor), I (recipient), J (brand editor), K (accessibility), O (art director)
- Method: the `premium-design` process from kit PR #162 (plan, capture at desktop, tablet and phone widths in light and dark, critique, then reviewer O on the captures)
- Plan: `docs/design/landing-design-plan-2026-11-24.md`
- Verdicts: one JSON file per reviewer per round in this folder (`round<N>-<letter>.json`)

## Rounds

| Round | Head | D | I | J | K | O |
|---|---|---|---|---|---|---|
| 1 | `5be88f95aad9fe3f27c6e1f3f5dbc636c8c7fb35` | fail | pass | pass | pass | fail |
| 2 | `d1c21ed426a212a215651aa57e5ecfc17dea96df` | pass | not re-run | not re-run | not re-run | fail |
| 3 | `2b3c849e29f806cddb369b6c731ab17d69a06323` | not re-run | not re-run | not re-run | not re-run | pass |

## What each round fixed

- Round 1 fails: D found two wrong word counts (hero body before the pass is 49 words, old H1 is 13). O found density, beat labels, unlabelled drawn panels, an oversized wordmark with no asset entry, and failed API requests in the captures.
- Round 2 (D pass): counts corrected, H1 cut to 10 words and body to 27, beat labels removed, example captions added, wordmarks resized with asset entries, captures retaken with a fixture API. O still failed on two items: the hero was a drawn panel rather than a real app screenshot, and density was about 131 words per desktop screen.
- Round 3 (O pass): hero uses owned screenshots of the app on sample data, each with an asset entry. Density is 89.5 words per desktop screen at 1440 (709 words over 7,129 px).

## Final status

O passes at the final reviewed head. D last passed at round 2, and I, J and K at round 1. Round 3 changed copy (density cuts) and added images, and D was not re-run on it, so the PR body's round 3 claims have not had a claims-auditor pass. The PR stays draft.

## Residuals carried forward

- Not verified from CI: the local lint, `tsc -b` and `npm test` results in the PR body. CI runs them at the PR head.
- This repo has no denylist file, so the denylist-guard check was skipped for the reviewer text. Reviewer O text was hand-filtered: references to private design inputs and local paths are left out of these public files.
- Resolved after round 3 (owner decision, 2026-11-24): the hero copy and density cuts were reverted and a density override recorded in `brand/lem/DESIGN.md`. Every cut fact is back on the page. These verdicts predate that change; the restored copy has not had a reviewer round.
- "a small first cohort" and the "Most popular" pricing badge sit beside a one-user pre-launch beta. Owner decision.
- Hero screenshot: tab row cut mid-word at 1440 and 390, filter chrome visible, light only until the app has a dark theme.
- Measurement beat panel teaches little. Full-page captures were judged at low resolution.
- Accessibility, not verified: dark and 390 focus pass, measured tap targets at 390, reduced-motion capture, automated contrast check, and Focus Not Obscured behind the sticky nav (consider `scroll-padding-top: 5rem`).
- Copy nits from J: "Credentials" vs "Secrets" term drift (consider "Sign-in tokens"); step 1 title and body say the same thing.
