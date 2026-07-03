# Email-Reply Verification-PIN for LinkedIn Login Challenges

When LinkedIn challenges an automated login (new IP / "suspicious login"), its mobile-app
"tap Yes" approval is unreliable — it often never prompts. This flow uses LinkedIn's
**email verification-code** path instead and lets the user hand the code back by simply
**replying to an email**.

## Flow

1. On a login challenge, `login_to_linkedin` → `_handle_challenge` calls
   `drive_email_pin_challenge` (`utilities/linkedin/helper.py`), which navigates LinkedIn's
   challenge to the "enter the code we emailed you" screen (this makes LinkedIn send the
   user a 6-digit code).
2. It mints a reply token (`verification_pin.create_pin_request`) and emails the user
   (`send_login_pin_request_email`) with a tokenized **Reply-To**: `pin+<token>@<parse-domain>`.
3. The user **replies to that email** with the code.
4. **SendGrid Inbound Parse** receives the reply and POSTs it to
   `POST /api/linkedin/verification-pin/inbound`. The webhook extracts the token (from the
   `to`/`envelope`) and the 6-digit code (from the body), and stores the PIN in Redis keyed
   by user (`submit_pin_by_token`).
5. The paused login is polling `get_pin(user_id)`; it enters the code, ticks "recognize this
   device", and submits. On success it stores fresh cookies — so subsequent logins from the
   same (sticky residential) IP reuse cookies and rarely re-challenge.

Fails open: if Redis or email is unavailable, the login falls back to the mobile-app
approval wait, exactly as before.

## One-time setup

### 1. Env vars (`.env`)
```
LINKEDIN_EMAIL_PIN_ENABLED=true
LINKEDIN_PIN_WAIT_SECONDS=300
LINKEDIN_PIN_TTL_SECONDS=900
LINKEDIN_PARSE_DOMAIN=parse.christopherqueenconsulting.com
```

### 2. DNS — MX record for the parse subdomain
Point the parse subdomain's MX at SendGrid:
```
parse.christopherqueenconsulting.com.  MX  10  mx.sendgrid.net.
```

### 3. SendGrid Inbound Parse
SendGrid → Settings → **Inbound Parse** → Add Host & URL:
- **Receiving domain:** `parse.christopherqueenconsulting.com`
- **Destination URL:** `https://lem.christopherqueenconsulting.com/api/linkedin/verification-pin/inbound`
- **⚠️ Leave "POST the raw, full MIME message" UNCHECKED.** The webhook reads SendGrid's *parsed*
  fields (`to` / `text` / `envelope`); if this box is checked, SendGrid sends one raw-MIME blob
  instead and the token/code can't be extracted (the reply silently never lands).

The webhook is under `/api/linkedin/verification-pin` (a public prefix — no bearer auth), and
always returns `200` so SendGrid doesn't retry-storm on unrelated mail.

### 4. Verify
- Send yourself a test to `pin+test@parse.christopherqueenconsulting.com` with a 6-digit code
  in the body; the webhook should log `Received LinkedIn verification PIN via email reply`
  (or `ignored` if the token is unknown — expected for a made-up token).

## Security notes
- The token is single-purpose, short-TTL (15 min), and only maps to a user id — a stray/spoofed
  reply with an unknown token is ignored.
- The stored PIN has the same short TTL and is cleared once consumed.
- The webhook is intentionally permissive (always `200`, ignores anything without a valid
  token + 6-digit code) to avoid leaking which tokens are live.
