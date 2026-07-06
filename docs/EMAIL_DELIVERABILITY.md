# Email Deliverability — stop the login PIN email being flagged as Spam/Phishing

LEM sends its own **login verification (PIN) email** from `src/cqc_lem/utilities/email.py`
(`send_pin_email`). Gmail was flagging every message as *Spam and Phishing*. There are two
sides to the fix: **code** (already done in this repo) and **DNS/provider config** (you must
apply — code cannot set DNS).

> Note: `src/cqc_lem/utilities/linkedin/verification_pin.py` is a *different* thing — it READS
> a PIN out of the inbox for the LinkedIn login challenge. It does not send the LEM login email.

## What was fixed in code

- **multipart/alternative with a real `text/plain` part.** Previously the email was HTML-only
  (no plaintext alternative) — a strong spam signal. Both the SendGrid and SMTP paths now send
  text + HTML.
- **Trustworthy From identity.** A friendly display name (`EMAIL_FROM_NAME`) on the
  authenticated domain address (`SENDGRID_FROM_EMAIL`), instead of a bare or mismatched sender.
- **Reply-To** set to a real, monitored mailbox (`EMAIL_REPLY_TO`).
- **Clean, non-phishy copy + footer.** Clear reason for the message, the code, a 10-minute
  expiry, an explicit "we will never ask you for this code" line, and a footer identifying the
  company. **No links** in the PIN email (a login code needs none — removing links removes the
  link-text≠href phishing signal). No `List-Unsubscribe` (transactional login codes must not be
  unsubscribable).

None of that helps if the sending domain isn't authenticated. **You must do the DNS below.**

## USER ACTION REQUIRED — DNS + provider config

Confirm the exact sending domain first. The app defaults to
`SENDGRID_FROM_EMAIL=no-reply@lem.christopherqueenconsulting.com`, so the examples below use
`lem.christopherqueenconsulting.com`. If you send from the apex `christopherqueenconsulting.com`
instead, apply the records to that zone. **The From-domain and the authenticated domain must
match** (alignment) or Gmail treats it as spoofing.

### 1. SendGrid domain authentication (SPF + DKIM)

1. SendGrid → **Settings → Sender Authentication → Authenticate Your Domain**.
2. Enter the domain (`lem.christopherqueenconsulting.com` or the apex).
3. SendGrid generates **three CNAME records** (DKIM + return-path/SPF). They look like:

   ```
   Type   Host                                   Value
   CNAME  em1234.lem.christopherqueenconsulting.com        u1234567.wl.sendgrid.net
   CNAME  s1._domainkey.lem.christopherqueenconsulting.com s1.domainkey.u1234567.wl.sendgrid.net
   CNAME  s2._domainkey.lem.christopherqueenconsulting.com s2.domainkey.u1234567.wl.sendgrid.net
   ```
   (Your exact hosts/targets come from SendGrid — copy them verbatim.)
4. Add those CNAMEs in Cloudflare DNS. **Set them to "DNS only" (grey cloud), not proxied.**
5. Back in SendGrid, click **Verify**.

That covers DKIM and the SPF/return-path alignment for SendGrid.

### 2. DMARC record

Add ONE TXT record on the sending domain. Start in monitor mode, then tighten:

```
Type  Host                                    Value
TXT   _dmarc.lem.christopherqueenconsulting.com  "v=DMARC1; p=none; rua=mailto:dmarc@christopherqueenconsulting.com; fo=1; adkim=s; aspf=s"
```

- Start with `p=none` (monitor). After a week of clean SendGrid reports, move to
  `p=quarantine`, then `p=reject`.
- `adkim=s; aspf=s` require strict alignment — fine once SendGrid domain-auth is verified and
  the From is on the same domain.

### 3. If you also keep the classic SPF TXT record

If the domain has an existing SPF TXT record, make sure it includes SendGrid:

```
Type  Host                              Value
TXT   lem.christopherqueenconsulting.com  "v=spf1 include:sendgrid.net ~all"
```

(SendGrid's automated domain auth normally handles the return-path SPF via the CNAMEs above, so
this is only needed if you publish your own SPF on the From domain.)

### 4. Provider / env settings to apply

Set these in the deployed `.env` (see `.env.example`):

```
SENDGRID_API_KEY=<real key>
SENDGRID_FROM_EMAIL=no-reply@lem.christopherqueenconsulting.com   # on the authenticated domain
EMAIL_FROM_NAME=Christopher Queen Consulting
EMAIL_REPLY_TO=support@christopherqueenconsulting.com             # a real, monitored mailbox
```

Do **not** rely on the Gmail SMTP fallback for production login codes — a `gmail.com` From is
not aligned with the authenticated domain and Gmail may rewrite it.

## How to verify

1. **mail-tester.com** — send a PIN email to the address it gives you; aim for 9–10/10. It
   explicitly reports SPF, DKIM, DMARC, and whether a text part is present.
2. **Gmail "Show original"** — on a received PIN email, confirm `SPF: PASS`, `DKIM: PASS`,
   `DMARC: PASS`, and that DKIM/SPF domains align with the From domain.
3. **Google Postmaster Tools** (postmaster.google.com) — add the sending domain to watch
   spam-rate and authentication trends over time.
4. **MXToolbox** — `dkim:lem.christopherqueenconsulting.com` and the DMARC lookup to confirm the
   records resolve publicly.

## Could not confirm from code

- Whether the domain is *currently* SendGrid-authenticated and whether a DMARC record already
  exists — that lives in the SendGrid dashboard and DNS, not the repo. Verify via step 1/4 above.
- The exact production `SENDGRID_FROM_EMAIL` in use (the repo default/example is
  `lem.christopherqueenconsulting.com`; the runtime `.env` is authoritative).
