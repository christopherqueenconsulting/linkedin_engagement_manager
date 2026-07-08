# Publishing "LEM LinkedIn Connect" to the Chrome Web Store

This is the true one-click install path (no Developer mode / Load unpacked). Until the
listing is **approved by Google**, users side-load the same bundle via the account page's
**Download extension** button — see [`LINKEDIN_COOKIE.md`](LINKEDIN_COOKIE.md).

> Do not release/announce the store listing until Google approves it. The extension source,
> icons, and packaging in this PR are ready; submission is a manual step.

## 1. Build the upload artifact

```bash
poetry run python scripts/package_extension.py
# → dist/lem-linkedin-connect-v<version>.zip
```

Bump `version` in `src/cqc_lem/browser_extension/manifest.json` for every resubmission
(the store rejects a re-upload of an existing version).

## 2. One-time developer setup

1. Create a [Chrome Web Store developer account](https://chrome.google.com/webstore/devconsole)
   ($5 one-time fee).
2. Create the item, upload the zip once manually to reserve the **extension ID**.
3. For automated re-uploads, create Google API OAuth credentials and a refresh token per
   [chrome-webstore-upload docs](https://github.com/fregante/chrome-webstore-upload/blob/main/How%20to%20generate%20Google%20API%20keys.md).
   Store them as repo secrets: `CWS_EXTENSION_ID`, `CWS_CLIENT_ID`, `CWS_CLIENT_SECRET`,
   `CWS_REFRESH_TOKEN`.

## 3. Listing content

- **Name:** LEM LinkedIn Connect
- **Summary:** Send your existing LinkedIn session to LinkedIn Engagement Manager in one
  click so automation resumes a trusted session — no password login, no new-device challenge.
- **Category:** Productivity
- **Privacy policy URL:** host [`PRIVACY_POLICY_EXTENSION.md`](PRIVACY_POLICY_EXTENSION.md)
  at a public URL (e.g. `https://lem.christopherqueenconsulting.com/extension-privacy`) and
  paste it into the listing.
- **Screenshots:** the popup (1280×800 or 640×400) plus the account-page card.

### Permission justifications (the store asks for these)

| Permission | Justification |
|---|---|
| `cookies` | Read the user's own `li_at` LinkedIn session cookie (httpOnly, unreadable by page JS) to hand it to their LEM instance so automation reuses a trusted session. |
| `storage` | Remember the user's LEM URL and session token locally so they don't re-enter them. |
| `host_permissions: *.linkedin.com` | Required to read the LinkedIn session cookie. |
| `host_permissions: lem.christopherqueenconsulting.com` | The endpoint the session is POSTed to. |

**Data use disclosure:** the extension transmits the user's own LinkedIn session cookie
**only** to the user's own LEM instance over HTTPS. No data is sold or sent to third parties.

## 4. Publish

Manual: upload `dist/…zip` in the dev console → submit for review.

Automated (after secrets are set): run the **Publish Browser Extension** workflow
(`.github/workflows/publish-extension.yml`) via *Actions → Run workflow*. It is
`workflow_dispatch`-only — it never runs on push/release, so nothing ships to Google until
someone explicitly triggers it.

## 5. After approval

Replace the account card's side-load steps with the store install link and update
[`LINKEDIN_COOKIE.md`](LINKEDIN_COOKIE.md).
