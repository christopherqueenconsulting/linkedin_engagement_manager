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
   delivers the forwarded email to `POST /api/linkedin/comment-notification/inbound`.
4. The webhook resolves the token → user, confirms it's a *comment* (not a reaction), and triggers a
   **debounced** recent-posts reply sweep (`sweep_reply_comments`). A burst of notifications collapses
   into one sweep (120s window).

Event mode also schedules **one** golden-hour safety sweep ~35 min after each post publishes, in case
a notification isn't forwarded.

## User setup

1. **Enable LinkedIn email notifications** for comments: LinkedIn → Settings & Privacy →
   Notifications → *Comments on your posts* (and *replies*) → ensure **Email** is on.
2. **Gmail auto-forward filter**: Settings → Filters and Blocked Addresses → *Create a new filter* →
   From `notifications-noreply@linkedin.com`, Subject `commented OR replied` → *Forward it to*
   `reply+<token>@parse.<domain>` (add/verify the forwarding address first under Settings →
   Forwarding). Copy the exact address from the account page.

## Modes (per user, `engagement_preferences.reply_check_mode`)

| Mode | Behavior |
|---|---|
| `event` (default) | Forwarded-email webhook + one golden-hour safety sweep per post. |
| `scheduled` | A beat dispatcher runs a recent-posts sweep `reply_sweeps_per_day` times/day (2–12). Shows a 429 warning in the UI. |
| `off` | No reply automation. |

## Config / env

- `LINKEDIN_PARSE_DOMAIN` — inbound parse host (e.g. `parse.christopherqueenconsulting.com`); else
  derived from `SENDGRID_FROM_EMAIL` / `PUBLIC_BASE_URL` (see `_default_parse_domain`).
- The per-user token is minted lazily and stored on `users.reply_inbound_token` (unique).
- The webhook is public (`_PUBLIC_API_PREFIXES`) and always returns 200.

## Related code

- `src/cqc_lem/utilities/linkedin/notification_email.py` — address + comment-vs-reaction classifier.
- `src/cqc_lem/api/main.py` — `linkedin_comment_notification_inbound` (+ debounce).
- `src/cqc_lem/app/run_automation.py` — `sweep_reply_comments`, `post_to_linkedin` mode branch.
- `src/cqc_lem/utilities/linkedin/verification_pin.py` — the PIN inbound flow this mirrors.
