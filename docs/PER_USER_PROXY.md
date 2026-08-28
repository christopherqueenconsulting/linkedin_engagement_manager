# Per-user egress proxy

Routes each user's automation browser through an egress IP near where they normally
sign in — the durable fix for LinkedIn's "new location" device-approval challenges.
**Zero user-side setup:** routing is derived automatically from the user's stored
location; the user installs/configures nothing.

## The problem

LinkedIn challenges a login when its **IP geolocates far from the user's usual
location**, and again when the IP keeps changing. Our automation egresses from the
VPS's single datacenter IP — wrong location for most users, and unstable across the
fleet. Browser-fingerprint stealth (shipped: `navigator.webdriver`, UA, timezone,
locale, geolocation overrides) makes the session look non-automated but **cannot
change the IP**, which is the dominant signal. So we need per-user egress from a
**stable IP near that user's location**.

## How it resolves (automatic, no user action)

`get_docker_driver(user_id=…)` calls `resolve_proxy(explicit, country)` —
`utilities/proxy.py` — which picks, first match wins:

1. **`users.proxy_url`** — explicit per-user override (power users / paid proxies).
2. **`REGION_PROXIES[country]`** — a regional proxy matched to the user's **stored
   country** (we already capture location, incl. IP auto-capture). **This is the
   zero-setup path:** the user just has a location (often auto-detected) and is routed
   automatically.
3. **`PROXY_URL`** — a global default.
4. **none** — direct egress (today's behavior).

`REGION_PROXIES` is a JSON map of ISO country codes (+ optional `"DEFAULT"`) → proxy
URL. Chrome gets `--proxy-server=scheme://host:port`.

## Options that need NO user setup

### 1. A few cheap regional egress nodes — recommended ✅

Stand up one tiny proxy (Squid/3proxy on a `t4g.nano` / Lightsail / cheap VPS, ~$3–4/mo)
in each region your users cluster in (e.g. `us-east`, `us-west`, `eu-west`,
`ap-southeast`). Put their URLs in `REGION_PROXIES`. Each user is auto-routed to the
node matching their country.

- **Zero user setup** — derived from location we already have.
- **Cost scales with *regions* (a handful), not users** — one node serves everyone in
  its region, so it stays cheap at SaaS scale.
- **Stable IP per region** — once a user's device is approved from that IP, cookies +
  "recognize this device" keep subsequent logins clean.
- Uses infra we already have (**AWS** account / the same VPS provider).
- Trade-off: these are *datacenter* IPs. The **geography matches** (kills the "new
  location" flag), but datacenter is a minor residual signal vs. residential. In
  practice geography-match + stable IP + one-time approval is enough for most users.
  Provision these as a small Terraform/CDK module or a per-region cloud-init script
  (follow-up — the app side is done and config-driven).

### 2. Cloudflare WARP on the box — free baseline 🟡

Run WARP (`warp-cli`, free) on the VPS / a sidecar and set it as `REGION_PROXIES`'
`DEFAULT` (or `PROXY_URL`). Free, zero setup, sheds the raw datacenter IP for a
cleaner Cloudflare egress — but it's **one shared location**, so it doesn't match
per-user geography. Good as the catch-all default beneath the regional nodes.

### 3. Commercial residential proxy — opt-in, paid 🟡

Point a user's `users.proxy_url` at a residential provider (Bright Data, Oxylabs,
Smartproxy…). Best stealth (real residential IPs), but **per-GB billing** that grows
with usage — so keep it opt-in for users who want turnkey and will pay, not the default.

## Not recommended here

- **BYO home exit node (Tailscale / `cloudflared`)** — gives a genuine residential IP
  at zero marginal cost, but **requires the user to install software at home**. Ruled
  out: we want no user-side setup. (Left reachable only as a `users.proxy_url` override
  for a technical user who opts in.)
- **Tor** — free but LinkedIn blocks exit nodes and you can't pin exit geography.

## Recommendation

Ship the resolver (this change) → run **a few regional egress nodes (#1)** keyed by
country via `REGION_PROXIES`, with **WARP (#2)** as the free `DEFAULT`. Users do
nothing. Combined with the existing one-time device approval + cookie persistence,
this removes the "new location" challenge without per-GB spend or user onboarding.

## Browser identity — how credentials actually reach the proxy

Chrome's `--proxy-server` **cannot carry inline `user:pass`**, and never will. The regional-node
options above sidestep that by authenticating on *source IP* (lock the nodes to the VPS's IP).
A credentialed commercial proxy — one whose sticky-session and geo target live in the *username*,
as DataImpulse's do — cannot.

`_build_proxy_auth_extension_b64` in `utilities/selenium_util.py` is the ONE place that gap is
closed, and it is the only sanctioned way credentials reach a browser session:

- It builds an **MV3 Chrome extension in memory** whose service worker answers
  `chrome.webRequest.onAuthRequired` with the username/password.
- **MV3, not MV2, is load-bearing.** MV2 background pages are disabled in current Chrome (149+),
  so the historical MV2-background-page recipe silently stops answering the auth challenge — the
  session then looks like a proxy failure, not an extension failure. The MV3 service worker needs
  the `webRequestAuthProvider` permission, which is what re-enables a *blocking*
  `onAuthRequired` listener for a normal extension.
- The credentials are `json.dumps`-escaped into the background script, so the `;` and `.`
  separators inside a sticky-session username survive intact.
- The result is a **base64 zip handed to `options.add_encoded_extension`** — never written to disk,
  so there is no temp file to leak or clean up.

**Never URL-embed proxy credentials.** A `http://user:pass@host:port` proxy URL is not carried by
Chrome, and putting one in a log line or an env dump is how the credential escapes.

## Bandwidth on metered proxies (issue #1728)

Several of the recommended residential providers (IPRoyal, Decodo…) bill **per GB**, not just per
IP — "unlimited traffic" only applies to some tiers. LinkedIn's feed/profile pages are image-heavy,
and none of LEM's selectors read pixels, only text/DOM, so every image byte a proxied session
fetched was pure waste against that cap.

`get_docker_driver` now blocks image loads (`--blink-settings=imagesEnabled=false`, applied in
`getBaseOptions`) whenever the session is **actually proxied** — `bandwidth_saver = bool(effective_proxy)
and PROXY_BANDWIDTH_SAVER_ENABLED` (default on). Direct/unproxied egress (dev, the tutorial-video
capture session) is never touched — that traffic isn't metered. Disable with
`PROXY_BANDWIDTH_SAVER_ENABLED=false` only to debug a session visually through the proxy.

## Status / follow-ups

- App side (DB field, resolver, Selenium wiring, MV3 auth extension, tests) — **done,
  config-driven**.
- Provision the regional nodes (Terraform/CDK or cloud-init) + lock to the box IP.
- `GET/PUT /user/proxy` + Settings UI (only needed for the opt-in override).
