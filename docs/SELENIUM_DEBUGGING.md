# Debugging the live browser (Selenium MCP + lemvnc)

LEM's automation logs into LinkedIn and engages through a headful Chrome running in
the `selenium-chrome` container. When LinkedIn changes its DOM (login fields, buttons)
the automation starts failing — this guide is how to drive and watch that exact
browser to find out why.

Two cooperating surfaces:

| Port | What | Exposure |
|---|---|---|
| `4444` | Selenium WebDriver hub — programmatic control | loopback (`127.0.0.1:4444`) |
| `7900` | noVNC — *watch* the browser | loopback (`127.0.0.1:7900`) + [`lemvnc.christopherqueenconsulting.com`](https://lemvnc.christopherqueenconsulting.com/?autoconnect=1&password=secret) |

You **drive** via 4444 and **watch** via 7900 at the same time.

## Watch: lemvnc

Open [`https://lemvnc.christopherqueenconsulting.com/?autoconnect=1&password=secret`](https://lemvnc.christopherqueenconsulting.com/?autoconnect=1&password=secret)
(behind Cloudflare Access). No Cloudflare hostname yet? Tunnel and use the local URL:

```bash
ssh -L 7900:localhost:7900 -L 4444:localhost:4444 <vps>
# then open http://localhost:7900/?autoconnect=1&password=secret
```

## Drive: the Selenium MCP server

Defined by [`tools/selenium_mcp_server.py`](../tools/selenium_mcp_server.py), which
*attaches to the live grid* (not a throwaway local browser), so its actions appear in
lemvnc. `.mcp.json` is gitignored, so enable it from the tracked template:

```bash
cp .mcp.json.example .mcp.json   # registers the `selenium-lem` server
poetry install --with mcp        # one-time: pulls the `mcp` SDK
```

It picks up `SE_REMOTE_URL` (default `http://127.0.0.1:4444`). On the VPS that's the
loopback-mapped hub; from a laptop, the SSH tunnel above makes it reach the box.
Restart Claude Code after editing `.mcp.json` — MCP servers load at session start.

Tools: `start_browser`, `navigate`, `current_state`, `list_inputs`, `list_buttons`,
`type_into`, `click`, `screenshot`, `page_source`, `execute_js`, `quit_browser`.
`list_inputs` is the workhorse — it prints every input's `id`/`type`/`autocomplete`/
`displayed`, which is how you spot that LinkedIn dropped `id="username"`.

## Drive (no MCP): the in-container harness

Same browser, scriptable, runs inside the worker that already reaches the grid:

```bash
sudo docker cp tools/debug_linkedin_login.py celery_worker_selenium:/tmp/dbg.py
sudo docker exec celery_worker_selenium python /tmp/dbg.py --inspect            # dump DOM + screenshot
sudo docker exec celery_worker_selenium python /tmp/dbg.py --login --user-id 1  # real login_to_linkedin
```

## What the login selectors look for now

LinkedIn's redesigned login page uses **ephemeral React ids** (e.g. `«Refvd3ksopa55j6»`)
and a `<button type="button"><span>Sign in</span></button>` — so the old
`id="username"` / `id="password"` / `[type=submit]` selectors all match nothing, and it
renders **duplicate hidden+visible** copies of every field. `login_to_linkedin` therefore
matches on stable `type`/`autocomplete` attributes (+ the button's visible text) and
picks the *displayed* copy via `get_visible_element_wait_retry`.

After a successful credential submit LinkedIn often shows **"Check your LinkedIn app →
tap Yes"** — a device-approval 2FA challenge (not Arkose, so it can't be auto-solved).
`login_to_linkedin` waits up to `LINKEDIN_APPROVAL_WAIT_SECONDS` (default 120) for you to
approve from your phone while watching via lemvnc; "Recognize this device" is pre-checked,
so once approved the stored cookies skip the challenge next time. The moment that challenge
appears the user is sent a **high-priority email** (`LINKEDIN_APPROVAL_EMAIL_ENABLED`,
`LEMVNC_URL`) so a human can act instead of the run stalling silently.

## Reducing bot/anomaly flags

`get_docker_driver` applies a stealth profile so LinkedIn is less likely to flag the
session as automation: `--disable-blink-features=AutomationControlled`,
`excludeSwitches=[enable-automation]` + `useAutomationExtension=false`, a realistic
desktop user-agent, and a CDP init script that sets `navigator.webdriver=undefined`,
aligns `navigator.languages` to the user's locale, and ensures `window.chrome` exists.
Combined with the per-user geo/timezone/locale overrides, the JS fingerprint is
consistent and non-automated-looking.

**Honest limitation:** the dominant "logging in from a new location" signal is the
**egress IP**, not the browser fingerprint. Stealth removes the *automation* tells but a
datacenter/VPS IP geographically far from where the user normally logs in will still draw
device-approval challenges. The durable fix is a per-user **residential proxy / VPN egress**
matched to the user's location (schema groundwork is already in place via the geo fields).
