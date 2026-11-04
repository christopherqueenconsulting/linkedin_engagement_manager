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

Every inbound parse POST that parses as a form (both endpoints) logs one redacted decision and
emits one `inbound_parse_email` PostHog event carrying `verdict`, `classification`, and `user_id`.
A POST whose form cannot be parsed returns `ignored` with no log line and no event. Verdicts
include `comment_accepted`, `debounced`, `gmail_confirmation`, `linkedin_not_comment`, `unrelated`,
`unknown_reply_token`, `no_reply_token`, `no_pin_token`, `no_pin_in_text`, `pin_accepted`, and
`pin_ignored`. Each decision line carries the verdict, a classifier label, `token_present=` and
`sender_domain=` (the lowercased From domain, limited to letters, digits, dots and hyphens;
`unknown` when the From header has no domain and `invalid` when the value is not a plain hostname;
it can belong to a third party when unrelated mail reaches the parse host).
These logs never include forwarding addresses, tokens, subjects, message bodies, verification
codes or confirmation URLs, and the exception logs written by the inbound handlers in
`api/main.py` carry only the exception type (database helpers they call may log their own
errors). The Gmail confirmation handler logs `url_present` and `code_present` when the mail is
received, and its result line records `url_found`, `confirmed`, `code=yes|no` and
`forwarded_to_user` (never the code or the URL).

For temporary SendGrid retry correlation, set `LI_REPLY_INBOUND_TRACE_ENABLED=true` in the `.env`
file the API containers load, then recreate both API colours (the flag is read once at process
start, and `docker compose restart` keeps the old environment, see `docs/demo-mode.md`):

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --no-deps --pull never \
  --force-recreate web_api_blue web_api_green
```

Check it with `docker exec web_api_blue printenv LI_REPLY_INBOUND_TRACE_ENABLED` and by searching
the API logs for `trace_token=` after a test POST. Each decision line then ends with `trace_token=` and `trace_message=`: the first 12 hex
characters of the SHA-256 of the reply token and of the full `<Message-ID>` string including the
angle brackets, or `none` when no `Message-ID: <...>` header is found in the first 8000 characters
of the headers. Keep the flag on only as long as the diagnosis needs: remove it from `.env`, run
the same recreate command, and no `trace_` fields appear. A forwarding-confirmed badge
proves a LinkedIn notification arrived only when its source is `forwarded_email`; a badge from
`auto_click` only proves Gmail's verify link was clicked. Neither proves that comment
notifications are being forwarded. Healthy comment flow includes `comment_accepted`; repeated
`linkedin_not_comment` means messages arrived but were correctly ignored as non-comment mail.

## Related code

- `src/cqc_lem/integrations/linkedin/notification_email.py` — address + comment-vs-reaction classifier.
- `src/cqc_lem/api/main.py` — `linkedin_comment_notification_inbound` (+ debounce).
- `src/cqc_lem/app/engagement/posting.py` — `sweep_reply_comments`, `post_to_linkedin` mode
  branch (moved out of `run_automation.py` in #1154; both still answer to their
  `cqc_lem.app.run_automation.<fn>` wire names).
- `src/cqc_lem/app/run_scheduler.py` — `dispatch_scheduled_reply_sweeps`, the beat that also carries
  the once/day event-mode backstop.
- `src/cqc_lem/utilities/golden_hour.py` — `event_mode_backstop_seconds`.
- `src/cqc_lem/utilities/linkedin/verification_pin.py` — the PIN inbound flow this mirrors.
