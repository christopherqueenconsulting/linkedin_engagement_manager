# Connect LinkedIn by session cookie (recommended)

The most reliable, lowest-friction way to keep a user's automation logged in **without
triggering LinkedIn's "Check your app" new-device challenge** is to reuse their existing
LinkedIn session instead of doing a fresh password login.

## Why this works

LinkedIn challenges a login when it sees a **fresh password sign-in from a new
device/IP** — exactly what our Selenium login does. But if we present the user's already
established session cookie (`li_at`), LinkedIn sees a continued, trusted session and does
**not** challenge it. `login_to_linkedin` already tries stored cookies first, so once a
valid `li_at` is saved, the password step (and its 2FA challenge) is skipped entirely.

`li_at` is an **httpOnly** cookie — page JavaScript can't read it, which is why the
one-click capture uses a browser extension (the `chrome.cookies` API can).

**Since issue #745, `li_at` is the DEFAULT engagement login, not a nicety.** Every Selenium
engagement lane logs in through `login_to_linkedin`, which tries stored cookies first; the stored
password is the fallback, and the fallback is the path that draws a challenge. An account with no
valid `li_at` is therefore an account whose engagement lanes are one challenge away from doing
nothing — which is exactly how automation can look "configured" while sending zero comments,
replies and DMs.

Where the two login paths meet is `_persist_session_cookies`, and that is also where a sign-in is
recorded for session-health reporting — see [LinkedIn session health](linkedin-session-health.md).
When LinkedIn does challenge, the PIN is answered without a human at the keyboard by
`linkedin/verification_pin.py` — see [Email-reply verification PIN](EMAIL_PIN_VERIFICATION.md).

## Option A — one-click browser extension (least steps)

The source lives at `src/cqc_lem/browser_extension/` (a minimal Chrome/Edge MV3 extension)
and ships inside the app image. Users don't need the repo — the account page's **LinkedIn
Session** card has a **Download extension** button, served by
`GET /api/extension/linkedin-connect.zip`.

1. On the account page, click **Download extension** and unzip it (or, for developers,
   use the `src/cqc_lem/browser_extension/` folder directly).
2. `chrome://extensions` → enable **Developer mode** → **Load unpacked** → select the
   unzipped folder.
3. Be signed in to `linkedin.com` in that browser.
4. Click the extension → set **LEM URL** (default prod) → paste your **LEM session
   token** once (the account card's **Copy my LEM token** button provides it) → **Connect**.

It reads `li_at` (+ `JSESSIONID`) and POSTs them to `POST /api/user/linkedin-cookie`.
After that, automation reuses the session; re-click **Connect** if the session ever
disconnects.

> A Chrome Web Store listing (true one-click install, no Developer mode) is being prepared
> separately and will replace the side-load steps once Google approves it.

## Option B — manual paste (no extension)

1. On `linkedin.com`: DevTools (F12) → **Application** → **Cookies** →
   `https://www.linkedin.com` → copy the **Value** of `li_at`.
2. Send it to the endpoint:

```bash
curl -X POST "$LEM_URL/api/user/linkedin-cookie" \
  -H "Content-Type: application/json" \
  -d '{"session_token":"<your LEM session token>","li_at":"<li_at value>"}'
```

## The endpoint

`POST /api/user/linkedin-cookie` — body `{ session_token, li_at, jsessionid? }`.
Authenticated by the user's LEM `session_token`. Validates `li_at` and stores it via the
standard cookie store (`store_linkedin_li_at` → `store_cookies`), so the existing
cookie-first `login_to_linkedin` path picks it up. Returns 422 on a malformed `li_at`,
401 on a bad session.

## Security

`li_at` is **as sensitive as a password** — anyone holding it can act as that LinkedIn
user. It is the user's own session, sent only to their own LEM instance over HTTPS and
stored in the same `cookies` table the automation already uses. Treat it accordingly;
rotate by signing out of LinkedIn (which invalidates it) and reconnecting.

## Combine with the proxy (optional)

Cookie reuse removes the *login* challenge. Pairing it with a stable per-user egress
(`REGION_PROXIES`, see [`PER_USER_PROXY.md`](PER_USER_PROXY.md)) keeps the session's IP
consistent too — but it's no longer required just to log in.
