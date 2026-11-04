# Event-driven reply notifications

Reply/comment follow-up on a user's own posts can run **event-driven** (recommended) instead of
polling LinkedIn on a timer. Polling logs into LinkedIn in a real browser through the user's
residential proxy every few minutes for hours, which is a primary cause of HTTP 429 rate-limiting.
Event mode replies only when a comment actually happens — no browser polling, no 429.

## How it works

1. LinkedIn emails the user when someone comments on their post.
2. The user sets a mail filter to **auto-forward** those emails to a personal tokenized address:
   `reply+<token>@parse.<domain>` (shown in the account settings, "Reply follow-up mode → Event-driven").
3. **SendGrid Inbound Parse** (already configured for the parse domain, same as the login-PIN flow)
   delivers the forwarded email to the app. NOTE: SendGrid Inbound Parse posts ALL mail for the
   parse host to a SINGLE URL — the login-PIN endpoint `POST /api/linkedin/verification-pin/inbound`.
   That endpoint dispatches on the address prefix: `pin+<token>` → PIN flow, `reply+<token>` →
   `_process_reply_inbound` (Gmail forwarding confirmation + comment notifications). The dedicated
   `POST /api/linkedin/comment-notification/inbound` path also exists and runs the same handler, but
   SendGrid does not need to be reconfigured — no second Inbound Parse URL is required.
4. The webhook resolves the token → user, confirms it's a *comment* (not a reaction), and triggers a
   **debounced** recent-posts reply sweep (`sweep_reply_comments`). A burst of notifications collapses
   into one sweep (120s window).

Event mode also schedules **one** golden-hour safety sweep ~35 min after each post publishes, in case
a notification isn't forwarded.

Both of those only ever reach a comment inside the first hour after publish, or when LinkedIn
actually emails the notification — which an always-active account rarely does in practice (observed
near-permanently absent, issue #1899), so a comment landing later got no follow-up at all. A
`dispatch_scheduled_reply_sweeps` backstop (once/day, `golden_hour.event_mode_backstop_seconds`) now
covers event-mode users too — far below `scheduled` mode's cadence, so it doesn't reintroduce the
429 exposure event mode exists to avoid.

## User setup

1. **Enable LinkedIn email notifications** for comments: LinkedIn → Settings & Privacy →
   Notifications → *Comments on your posts* (and *replies*) → ensure **Email** is on.
2. **Gmail auto-forward filter**: Settings → Filters and Blocked Addresses → *Create a new filter* →
   From `linkedin.com`, Subject `commented OR "replied to your comment"` → *Forward it to*
   `reply+<token>@parse.<domain>` (add/verify the forwarding address first under Settings →
   Forwarding). Copy the exact address from the account page. Do not use a broad `replied` subject
   term: it also matches LinkedIn direct-message alerts such as “Message replied”. Editing the filter
   does not require adding the forwarding address again.

## Modes (per user, `engagement_preferences.reply_check_mode`)

| Mode | Behavior |
|---|---|
| `event` (default) | Forwarded-email webhook + one golden-hour safety sweep per post + a once/day backstop sweep. |
| `scheduled` | A beat dispatcher runs a recent-posts sweep `reply_sweeps_per_day` times/day (2–12). Shows a 429 warning in the UI. |
| `off` | No reply automation. |

## Config / env

- `LINKEDIN_PARSE_DOMAIN` — inbound parse host (e.g. `parse.christopherqueenconsulting.com`); else
  derived from `SENDGRID_FROM_EMAIL` / `PUBLIC_BASE_URL` (see `_default_parse_domain`).
- The per-user token is minted lazily and stored on `users.reply_inbound_token` (unique).
- The webhook is public (`_PUBLIC_API_PREFIXES`) and always returns 200.

## Observability — how to tell the chain is working

Every inbound parse POST (both endpoints) logs one redacted decision and emits one
`inbound_parse_email` PostHog event carrying `verdict`, `classification`, and `user_id`. Verdicts
include `comment_accepted`, `debounced`, `gmail_confirmation`, `linkedin_not_comment`, `unrelated`,
`unknown_reply_token`, `no_reply_token`, `no_pin_token`, `no_pin_in_text`, `pin_accepted`, and
`pin_ignored`. The per-email verdict log never includes forwarding addresses, tokens, subjects, or
message bodies. The Gmail forwarding-confirmation path is the exception: it still logs the first 120 characters of the
subject and the first 500 characters of the body (which can contain the verify link and code), so treat those log lines as sensitive.

For temporary SendGrid retry correlation, set `LI_REPLY_INBOUND_TRACE_ENABLED=true`; this adds only
truncated SHA-256 fingerprints of the token and RFC Message-ID. Turn it off after diagnosis. A
forwarding-confirmed badge proves at least one LinkedIn notification arrived, not that comment
notifications are being forwarded. Healthy comment flow includes `comment_accepted`; repeated
`linkedin_not_comment` means messages arrived but were correctly ignored as non-comment mail.

## Related code

- `src/cqc_lem/utilities/linkedin/notification_email.py` — address + comment-vs-reaction classifier.
- `src/cqc_lem/api/main.py` — `linkedin_comment_notification_inbound` (+ debounce).
- `src/cqc_lem/app/engagement/posting.py` — `sweep_reply_comments`, `post_to_linkedin` mode
  branch (moved out of `run_automation.py` in #1154; both still answer to their
  `cqc_lem.app.run_automation.<fn>` wire names).
- `src/cqc_lem/app/run_scheduler.py` — `dispatch_scheduled_reply_sweeps`, the beat that also carries
  the once/day event-mode backstop.
- `src/cqc_lem/utilities/golden_hour.py` — `event_mode_backstop_seconds`.
- `src/cqc_lem/utilities/linkedin/verification_pin.py` — the PIN inbound flow this mirrors.
