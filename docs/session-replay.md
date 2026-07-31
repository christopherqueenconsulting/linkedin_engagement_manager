# Session replay — error-triggered + sampled SPA recording

Issue #649 (PH4), built on the SPA's PostHog surface (#646) and error tracking (#648).

Debugging an SPA report used to start with "can you tell me the steps?". A replay is those steps,
recorded. The point of this build is that the recordings we keep are the ones worth watching: every
session that produced an `$exception`, plus a small random sample of ordinary sessions for the
"nothing errored, it just didn't do what I expected" reports.

## Who is recorded

**The app's own signed-in and signing-in users, in LEM's own SPA.** Nobody else. Replay records the
DOM of pages this app serves — it is not a browser extension, it cannot see other tabs, other sites,
or LinkedIn itself. LEM's Selenium automation drives a headless Chrome that never loads the SPA, so
none of the LinkedIn work is or can be recorded here.

Recording is disabled outright in any build with no `VITE_POSTHOG_KEY` — `posthog-js` is a lazy
chunk that browser never even fetches.

## What is masked

| Surface | Rule |
|---|---|
| Every `<input>`, `<textarea>`, `<select>` value | `maskAllInputs: true` — replaced with `*` before anything leaves the browser |
| Any element carrying `data-ph-mask` | `maskTextSelector` — its TEXT is masked too, for content that isn't an input (rendered drafts, DM previews) |
| Element attributes on autocapture | `mask_all_element_attributes: true` (from #646) |
| Network requests | timings only (`network_timing`) — `recordHeaders: false`, `recordBody: false`, so no session token and no post/DM payloads |
| Canvas | `captureCanvas.recordCanvas: false` — carousel slide previews are expensive to record and never the bug |
| Console | captured INTO the replay timeline (`enable_recording_console_log`), which is masked and access-controlled. Note this is separate from `capture_console_errors`, which stays OFF: a console line must not become a fingerprinted `$exception` |

Use `maskProps()` from `ui/src/utils/analytics.ts` on every new content editor — it applies BOTH the
autocapture opt-out class and the replay mask attribute:

```tsx
<textarea {...maskProps('w-full border rounded-lg px-3 py-2 text-sm')} />
```

Masking is client-side: the masked value is what gets SENT, so unmasked content never reaches
PostHog at all.

## Recording rules

The SDK owns the decision, not the project settings — one place, in code, testable:

| Rule | Where | Default |
|---|---|---|
| Sample of ordinary sessions | `session_recording.sampleRate` | `0.1` (`VITE_POSTHOG_REPLAY_SAMPLE`) |
| Every session that throws | `posthog.on('eventCaptured')` → `ensureSessionRecorded()` | always on |
| Every session that files feedback | `FeedbackWidget` open → `ensureSessionRecorded()` | always on |
| Skip bounces | PostHog project setting **minimum duration** + `strictMinimumDuration: true` | 5s (see below) |
| Kill switch | `VITE_POSTHOG_REPLAY=false` | replay on |

The error trigger fires on the `$exception` event itself, so it covers BOTH posthog's own unhandled
error/rejection autocapture and anything the app catches and reports through `captureException()`.
An errored session is recorded **even when sampling left it out** — that override is the whole point.

`ensureSessionRecorded()` is that override, and it deliberately does NOT consult
`posthog.sessionRecordingStarted()`. That method reports whether rrweb is attached, and posthog
attaches rrweb for *every* session — the sampling decision only governs whether the buffer is ever
sent — so it reads `true` in exactly the sampled-out case the override exists for. Instead the
module remembers the session id it already forced, so a page throwing in a loop costs one override
rather than a full snapshot per exception, and a new session id re-arms it.

Two consequences worth knowing before you watch one:

- For a sampled session the replay starts at page load. For a forced one it starts **at the trigger**
  — the exception, or the moment the feedback panel opened. The lead-up isn't there: while a session
  is sampled out posthog discards its buffer on every emit, so there is nothing to backfill. If you
  need the lead-up for a specific flow, raise the sample rate for a while.
- `VITE_POSTHOG_REPLAY*` are read by Vite at BUILD time (docker build-args / CI vars
  `UI_POSTHOG_REPLAY`, `UI_POSTHOG_REPLAY_SAMPLE`), exactly like `VITE_POSTHOG_KEY`. Changing them
  needs a rebuild; setting them in the running container's `.env` does nothing.

### What still has to be set in PostHog, not in code

**Record user sessions must be ON for the project.** `disable_session_recording: false` in the SDK
is a veto, not a switch: posthog-js only starts the recorder when the project's remote config comes
back `enabled`. With the project toggle off, every rule above is inert and nothing is recorded — so
turn it on under [Replay → settings](https://us.posthog.com/project/475262/settings/project-replay)
first, then verify with step 1 below.

**Minimum duration** is remote config — set it under
[Replay → settings](https://us.posthog.com/project/475262/settings/project-replay) to **5000 ms**.
`strictMinimumDuration: true` (in code) makes PostHog measure it against the recorded buffer rather
than wall-clock session age, so a bounce split across two page loads is still dropped as a bounce.

Leave the project's own **sampling** at 100% / off. The SDK's local `sampleRate` takes precedence
over remote config, so configuring both would just multiply into a rate nobody intended.

## Every report links its replay

`ui/src/components/FeedbackWidget.tsx` already stamped `posthog_session_id` onto every report. That
id now becomes a link, in three places — and opening the widget forces the recording, so the link
resolves for every report instead of only the ~10% the sample happened to cover:

| Surface | Link |
|---|---|
| Auto-filed feedback issue (`utilities/feedback/issue_service.py`) | "Watch the session replay" above `## Scope` |
| The `+1` comment a repeat report leaves | that reporter's OWN session — often the one that shows what the first report couldn't |
| Error-tracking GitHub issue (`scripts/posthog_error_issues.py`) | the browser session one of the grouped exceptions was thrown in |

The URL is built by `observability.session_replay_url()` from `POSTHOG_PROJECT_ID` +
`POSTHOG_APP_HOST`. With no project configured, or a `posthog_session_id` that isn't the SDK's
uuid-ish shape, **the line is simply omitted** — a guessed link is worse than none, and the value is
pasted into GitHub markdown.

Privacy note for the GitHub side: what reaches an issue is a LINK, never recorded content. PostHog
is access-controlled and masks the same fields the SPA does.

## Verifying it

1. **A forced error is watchable.** In the SPA console: `posthog.captureException(new Error('replay
   smoke test'))`. Within a minute the session appears under
   [Replay](https://us.posthog.com/project/475262/replay/recent), and the `$exception` lands on an
   error-tracking issue whose event carries the same `$session_id`.
2. **Masking holds.** Watch that recording and open the Account page's DM template editor or a
   post draft — every input reads as `*`, and any `data-ph-mask` block's text is masked.
3. **Feedback links through.** File a report from the widget; the auto-filed issue body carries a
   "Watch the session replay" link that opens that session.

Run all three from an ordinary browser. In LEM's Selenium grid Chrome the SDK silently sends nothing
while every local config check still reads healthy — a false negative nobody has root-caused — see
`docs/posthog-advanced-surface.md` § Verifying the SPA surface (issue #834).

Confirmed end to end on 2026-07-31 against the production build: two recordings exist, each starting
within ~150 ms of the `feedback_opened` event that triggered it — i.e. `ensureSessionRecorded()`
forced a session that the 10% sample would otherwise have dropped, which is the one rule here that
config alone can't demonstrate.

## Quota

The free tier is 5,000 recordings/month. At a 10% sample plus every errored session and every
feedback report, a spike in errors spends quota — that is the intended trade (an error spike is
exactly when you want the recordings). If the month runs hot, lower `UI_POSTHOG_REPLAY_SAMPLE` and
rebuild; the two forced triggers are deliberately not sampled, and feedback reports are rate-limited
per user upstream anyway.
