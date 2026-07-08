# LEM LinkedIn Connect — Privacy Policy

_Last updated: 2026-07-08_

**LEM LinkedIn Connect** is a browser extension for existing LinkedIn Engagement Manager
(LEM) users. It exists to do one thing: hand your own LinkedIn session to your own LEM
instance so automation can resume a trusted session instead of performing a fresh password
login (which triggers LinkedIn's new-device security challenge).

## What it accesses

- **Your LinkedIn session cookies** (`li_at` and, if present, `JSESSIONID`) for
  `linkedin.com`. `li_at` is an httpOnly cookie that ordinary page scripts cannot read; the
  extension uses Chrome's `cookies` API to read it only when you click **Connect**.
- **Local settings** (your LEM URL and LEM session token), stored with the browser's
  `storage` API on your own machine so you don't re-enter them.

## What it does with it

- On **Connect**, it sends your LinkedIn session cookie and your LEM session token over
  HTTPS to the LEM endpoint you configured (default
  `https://lem.christopherqueenconsulting.com`), which stores it against your account so the
  automation you already authorized can reuse it.

## What it does NOT do

- It does not sell or share your data with any third party.
- It does not transmit your data to anyone other than the LEM instance you configure.
- It does not track your browsing, read page content, or run in the background — it acts
  only when you open the popup and click **Connect**.
- It collects no analytics.

## Your control

- Your LinkedIn session is your own credential; you can revoke it any time by signing out
  of LinkedIn (which invalidates `li_at`).
- Remove the extension to delete its locally stored settings.
- To delete the session stored in LEM, use your LEM account settings or contact support.

## Contact

Questions about this policy: support@christopherqueenconsulting.com
