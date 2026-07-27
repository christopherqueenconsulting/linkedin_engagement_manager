# Surveys (issue #653)

LEM asks its users four different questions, and after this change they are owned by two different
systems on purpose.

| Ask | Owner | Why there |
|---|---|---|
| **NPS** — 30 days after activation | **PostHog Surveys** | Targeting and cadence should be tunable without a deploy |
| **Post-quality CSAT** — after 5+ approvals | **PostHog Surveys** | Event-triggered; lands in the moment the user just judged a draft |
| **Review** — trial T-3d | `utilities/surveys.py` | It is the extended-trial gate (#499); PostHog cannot unlock anything |
| **Fix CSAT** — "did this fix it?" | `utilities/surveys.py` | Per-ISSUE (`fix_csat_<n>`), scheduled by the shipped-notice queue (#502) |

The split is the whole design. PostHog is better at *deciding who to ask and when*; LEM's own
`feedback` table is the only thing that can turn an answer into work.

## One answer, two destinations, counted once

A PostHog survey response takes both paths deliberately:

1. The browser emits PostHog's own **`survey sent`**, with `$survey_id` / `$survey_response` /
   `$survey_questions` in PostHog's native shape. That is what makes it a survey response in the
   Surveys product — summarizable, chartable, countable there.
2. The same answer is `POST`ed to **`/api/survey/posthog`**, where it becomes an ordinary `feedback`
   row and enters the classifier → cluster → GitHub-issue pipeline like any bug report.

`track_survey_response` — the homegrown `survey_response` event — is **not** emitted on this path.
Two events for one answer would double every response-rate number on the dashboards. The `feedback`
row carries `context_json.origin = "posthog_survey"` so anything counting rows out of MySQL can tell
the two streams apart.

### What reaches the auto-work loop

| Answer | Feedback status | Effect |
|---|---|---|
| NPS ≤ 6, or CSAT ≤ 2 | `new` | Auto-filing pass classifies it and opens/`+1`s a feedback issue |
| Any score **with** free text | `new` | Same — the text is the actionable part |
| Happy score, no comment | `resolved` | Nothing to classify; must not burn an LLM call per promoter |

## Why headless, not the popover

Both surveys are created as PostHog **`api`** type, so posthog-js draws nothing and
`PostHogSurveyModal.tsx` renders them in LEM's own modal. Two reasons, both load-bearing:

- A popover answer is a PostHog event and **nothing else**. It would never reach the feedback loop,
  which is the reason LEM asks for scores at all.
- The popover renders bottom-right, exactly where the feedback widget already sits.

The cost of rendering ourselves is that posthog-js never sees the ask, so its "already seen" and
wait-period bookkeeping would never advance — `markSurveySeen()` is what closes that gap, and
removing it turns the throttle off silently.

## Targeting

Expressed the PostHog way, against person properties the SPA sets at `$identify` (supplied by
`GET /auth/session`):

| Survey | Rule |
|---|---|
| NPS | `onboarding_completed_at` is before `-30d` |
| CSAT | activation event `post_approved`, **and** person `posts_approved >= 5` |

`onboarding_completed_at` is `onboarding_state.activated_at` — the activation "aha", **not** signup.
A user who signed up and stalled has no opinion worth a score, and their detractor answer would be
about onboarding rather than about the product.

`posts_approved` counts posts in `approved`/`scheduled`/`posted`: an approved post moves on as
automation runs, so counting the current status alone would reset the tally. The SPA advances the
property itself on each approval (`recordPostApproval`), so the ask can fire on the approval that
crosses the threshold rather than one session later. With no server baseline yet, only the event is
sent — a made-up count would target the survey at the wrong people.

Both surveys carry `seenSurveyWaitPeriodInDays = 30`, PostHog's cross-survey throttle: answering or
dismissing **either** buys 30 days of silence from **both**.

## No double-prompting

`posthog-surveys-enabled` (flag registry, `POSTHOG_SURVEYS_ENABLED`) is the switch. When it is on,
`next_survey_for_user` passes `nps_enabled=False` into `select_survey`, which retires
`nps_day3` and `nps_trial_end` from **both** the in-app modal and the daily email beat. The review
offer is untouched — it is the extended-trial gate.

Answering in PostHog also closes the homegrown ask on its own, without any flag: the `feedback` row
is what `next_survey_for_user` reads through `get_latest_feedback_at(user_id, NPS)`.

The flag fails open to the env var like every other registered flag (`docs/feature-flags.md`), and
a flag-lookup exception keeps the homegrown asks — the failure mode is "asked the old way", never
"asked twice".

## Provisioning

`scripts/posthog_surveys.py` is the ONE place the two surveys are defined.

```bash
python scripts/posthog_surveys.py --print-spec       # both specs as JSON, no network
python scripts/posthog_surveys.py --dry-run          # diff against the live project (exit 2 = pending)
python scripts/posthog_surveys.py --apply            # create missing / update drifted
python scripts/posthog_surveys.py --apply --launch   # ...and start one that has never run
```

`--apply` creates surveys as **drafts**. Launching is a separate opt-in so an apply can never start
collecting responses from a definition nobody has read. Drift is measured on a narrow set of managed
fields (`description`, `type`, `questions`, `conditions`, `schedule`, targeting groups) so an
appearance tweak in the PostHog UI does not read as permanent drift, and property values are
compared by value — PostHog echoes numbers back as strings.

Needs `POSTHOG_PERSONAL_API_KEY` with survey read+write and feature-flag write (a targeting flag is
created alongside each survey).

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `POSTHOG_SURVEYS_ENABLED` | `false` | Flag fallback — PostHog owns NPS/CSAT and the homegrown NPS asks stand down |
| `VITE_POSTHOG_KEY` | *(unset)* | No key means no posthog-js, so no PostHog survey is ever shown |

## Verifying in a test account

1. `python scripts/posthog_surveys.py --apply --launch`, then confirm both surveys are **Running**.
2. Flip `posthog-surveys-enabled` on for your own distinct ID (`str(user_id)`).
3. Sign in and confirm `onboarding_completed_at` / `posts_approved` are on your PostHog person.
4. NPS: the modal appears on load once activation is 30+ days old. CSAT: approve a post in Content
   Studio with 5+ approvals on file.
5. Submit. Check three things: a `survey sent` event on the person, the response in PostHog →
   Surveys, and a `feedback` row with `context_json.origin = "posthog_survey"`.
6. Score it low and confirm the next `file-feedback-issues` pass (every 30 min) opens the issue.
7. Reload — the survey must not reappear (`markSurveySeen` + the 30-day wait period), and
   `GET /api/user/survey` must not be offering an NPS ask.
