# YouTube publishing — keeping the OAuth refresh token alive (issue #742)

The token is installed with `POST /admin/youtube-token` — a DB-first credential in `app_credentials`,
so rotating it needs no deploy. `YOUTUBE_REFRESH_TOKEN` only seeds it.

The marketing tutorial pipeline (#505, `utilities/marketing/video_tutorials.py`) publishes to
YouTube Data API v3 with an OAuth **refresh token**. A refresh token dies on somebody else's
schedule, and before #742 nothing would have noticed until a run that already cost real money
failed at the upload step — possibly months later, since the feature stays off until ~1.0.

Known expiry causes:

| Cause | What it looks like |
|---|---|
| Consent screen left in **Testing** | grants expire on a short timer (this is the ~24h lapse the owner hit) |
| Access revoked / Google password changed | `invalid_grant` |
| Token unused for **6 months** | `invalid_grant` — the real risk while the feature is off |
| >100 refresh tokens issued for the same account+client | the oldest are silently invalidated |
| Client id/secret rotated | `invalid_client` |

## The posture

`utilities/marketing/youtube_auth.py` is the ONE place the token's state is decided. Four states,
and the difference between the last two is the whole point:

- **`ok`** — the refresh token minted an access token, and the granted scopes still include
  `youtube.upload`.
- **`needs_reauth`** — Google PROVED the grant is gone or insufficient: a 4xx from the token
  endpoint (`invalid_grant`, `invalid_client`, …), no `access_token` in a 200, or a reported scope
  set that no longer carries `youtube.upload`. This is the only state that alerts.
- **`unknown`** — the probe could not decide (network failure, 5xx, 429). Alerts nobody. Crying
  wolf on a transient trains the owner to ignore the one alert that matters.
- **`not_configured`** — no OAuth credentials at all. The expected pre-1.0 state, logged at DEBUG:
  a warning here would file a defect for working behaviour (see `utilities/CLAUDE.md` on
  recurrence escalation).

### The weekly probe IS the keep-alive

Beat entry `youtube-token-check` (`auto_weekly_youtube_token_check`, Wednesdays 19:30 UTC — 30 min
before the tutorial run). One token exchange: no upload, no quota, no spend. It does two jobs:

1. leaves the dated audit line (`YouTube OAuth token OK — checked <iso>, scope=…, source=…`) plus a
   `youtube_token_check` PostHog event, so "when did publishing actually go bad?" is answerable;
2. **defeats the 6-month-disuse expiry simply by existing.**

Do NOT "optimize it away while the feature is off" — off is exactly when it earns its keep.

### Preflight before spend

`produce_tutorial` runs `youtube_auth.preflight()` before any Selenium capture, TTS or render. Only
`needs_reauth` aborts the run: an install with no OAuth credentials still produces a usable MP4
(publishing no-ops, as it always did), and `unknown` is not evidence of anything.

### DB-backed storage, env fallback

The token is read **DB-first, env-second**: `app_credentials.youtube_refresh_token` wins when set,
otherwise `YOUTUBE_REFRESH_TOKEN` from `.env` is the seed. That means a re-minted token installs
without a box edit + container recreate, and a token Google rotates during a refresh is persisted
instead of lost. Access posture matches the LinkedIn tokens already in `users`: DB-access
controlled, and **never returned by any API** — the status endpoint reports state only.

Install a new token (admin secret, no deploy):

```bash
curl -X POST https://<host>/api/admin/youtube-token \
  -H "x-admin-secret: $ADMIN_SECRET" -H 'Content-Type: application/json' \
  -d '{"refresh_token":"1//0g..."}'
```

It stores the value and probes it immediately, so the response says whether the new token works.

### Surfacing it

`GET /api/admin/youtube-status?session_token=…` (session + admin gated) backs
`YouTubePublishingCard.tsx` in **Settings → Setup & Connection**: "connected / needs re-auth
(reason)", last-checked time, which store the token came from, and a **Check now** button
(`&live=true`) that re-probes. Reads the last recorded probe by default so opening Settings never
spends a round trip on Google. The card is hidden entirely when YouTube isn't configured.

Alerts go to `YOUTUBE_ALERT_EMAIL`, falling back to `MARGIN_REPORT_EMAIL`.

## Runbook — re-minting the refresh token

The owner has Google Workspace on `christopherqueenconsulting.com`, so the consent screen's user
type can be **Internal**: no verification review, no "unverified app" warning, and Internal grants
are not subject to the Testing-mode expiry that caused the original lapse. Do NOT chase the
External/verification path — Google rejected it (app name not brand-identifying, SPA home page
doesn't explain the app, name mismatch), and none of that work is necessary.

1. **The Cloud project must belong to the Workspace org.** *Internal* only appears when the project
   is owned by the Workspace organization — if it was created under a personal Gmail, recreate it
   while signed in as the Workspace account or move it into the org. This is the most common reason
   "Internal" is greyed out.
2. OAuth consent screen → **User Type: Internal** → Save.
3. Google Admin console → **Apps → Additional Google services → YouTube → ON** for that user/OU
   (Workspace accounts have YouTube off in some configurations).
4. Confirm that Workspace account has at least **Manager** access to the target YouTube channel.
5. Re-mint via OAuth Playground using the **Web** OAuth client, signed in as the Workspace account,
   with scope `https://www.googleapis.com/auth/youtube.upload`.
6. Install it with `POST /api/admin/youtube-token` (above). Updating `YOUTUBE_REFRESH_TOKEN` in
   `/opt/lem/.env` still works as the seed, but needs a container recreate to take effect.

## Where the values live

| Value | Where |
|---|---|
| `YOUTUBE_CLIENT_ID` / `YOUTUBE_CLIENT_SECRET` | `/opt/lem/.env` (git-ignored) |
| Refresh token (in force) | `app_credentials.youtube_refresh_token`, seeded by `YOUTUBE_REFRESH_TOKEN` |
| `YOUTUBE_PRIVACY_STATUS` | `/opt/lem/.env` — `unlisted` by default, so nothing goes public unreviewed |
| `YOUTUBE_ALERT_EMAIL` | `/opt/lem/.env` — empty falls back to `MARGIN_REPORT_EMAIL` |
| Last probe state | Redis `youtube:token:last_probe` (no TTL — the last known state is the trail) |
