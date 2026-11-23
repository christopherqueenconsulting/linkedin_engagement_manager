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

### The proxy forwarder (#2620) — the live path

**`utilities/proxy_forwarder.py` is the ONE place credentials are attached**, and Chrome never sees
them. It runs as the `proxy-forwarder` compose service and is switched on by
`PROXY_FORWARDER_HOST=proxy-forwarder` in `.env`.

- **One listener per user, and the port is the identity.** User N's Chrome is started with
  `--proxy-server=http://proxy-forwarder:<PROXY_FORWARDER_BASE_PORT + N>` (base 20000). A
  connection on that port is forwarded only to user N's own upstream, resolved by the same
  `resolve_proxy` rule as everything else (own `users.proxy_url`, else region, else `PROXY_URL`).
  One user's session can never leave through another user's proxy.
- **The forwarder adds `Proxy-Authorization` upstream** and drops any the client sent. Chrome
  connects with no credentials, so nothing depends on an extension or an IP allowlist.
- **Fails closed.** It never connects to a destination itself. No route for a port, an unreachable
  upstream or a refused tunnel is an error to Chrome (502, 504, or the upstream's own status, a 407
  included, minus its `Proxy-Authenticate`). A database it cannot read keeps the last routes.
- **Internal only.** The service publishes no port; only containers on the compose network reach
  it. It logs host:port and user ids, never credentials. The base port answers
  `GET /healthz` with counts only, and returns 502 to any proxy request.
- **Routes refresh every `PROXY_FORWARDER_REFRESH_SECONDS` (30).** An edit to `users.proxy_url` is
  live within one refresh, with no restart. A brand-new user waits up to one refresh for a listener
  and fails closed until then.
- **HTTP and HTTPS upstreams only.** A SOCKS proxy keeps the direct `--proxy-server` path (Chrome
  cannot authenticate to SOCKS at all).
- **Same host only.** Chrome resolves `proxy-forwarder` through compose DNS, so a Grid node on a
  second box (`docker-compose.grid-node.yml`) cannot reach it. Do not enable the forwarder with
  second-box nodes registered until that is solved.
- `/health/deep`'s egress probe takes the same route (`egress_probe.chrome_path_target`): through
  the forwarder, with no credentials of its own.

### The auth extension (rollback path only)

Chrome 158 ignores `--load-extension`, and the encoded extension below never appeared in a node's
`chrome://extensions` (2026-11-23). It is kept, unchanged, for `PROXY_FORWARDER_HOST` empty, which
is the rollback. On that path a credentialed proxy works only if it also allowlists the host's IP,
and the egress probe now says so: it sends no credentials, because Chrome sends none.

`_build_proxy_auth_extension_b64` in `utilities/selenium_util.py`:

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

**What the proxy carries.** Only Selenium browser sessions — `get_docker_driver` is the one place a
proxy is applied. LinkedIn REST/OAuth API calls, LiteLLM, PostHog, MySQL and Redis all egress direct.
A 2026-11 census of prod logs found ~36 sessions/day, every one proxied, and about two thirds with
images ON: the `needs_images` fastboot lanes (messaging, groups, posting, newsletter) are exempt from
the image block (#1774).

**The image block alone was not enough.** It stops `<img>` loads only. Every proxied session —
the `needs_images` lanes included, since fastboot needs image load events, not video — now also:

- blocks video via CDP `Network.setBlockedURLs` (`_PROXY_BLOCKED_MEDIA_URLS`:
  `dms.licdn.com/playlist/` and any HLS manifest). Not a bare `*.mp4*`, which could catch the
  upload of our own video post. Fails open with a warning.
- passes `_LEAN_NETWORK_ARGS`, which turn off Chrome's own background traffic (component and
  safe-browsing updates, domain-reliability reports, sync, pings) and stop autoplay without a
  gesture. A page cannot see any of these, so none of them is a fingerprint.

Same flag, same rollback: `PROXY_BANDWIDTH_SAVER_ENABLED=false` turns off all three. The session log
line reports `images=` and `video=`. Still on the wire: LinkedIn's JS/CSS bundles, which each session
downloads again because every Grid session starts with an empty cache. A persistent per-user disk
cache is the next lever if IPRoyal usage stays high.

## Status / follow-ups

- App side (DB field, resolver, Selenium wiring, MV3 auth extension, tests) — **done,
  config-driven**.
- Provision the regional nodes (Terraform/CDK or cloud-init) + lock to the box IP.
- `GET/PUT /user/proxy` + Settings UI (only needed for the opt-in override).
