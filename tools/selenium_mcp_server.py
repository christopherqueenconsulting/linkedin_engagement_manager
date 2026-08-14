"""Selenium MCP server for LEM.

Drives the *live* browser over the WebDriver hub so you can inspect/debug pages (e.g. LinkedIn
login) interactively while watching the same session in lemvnc. It deliberately attaches to the
remote grid rather than spawning a local browser, so what the tools touch is exactly what VNC shows.

Every session it opens is pinned to the Grid's **watchable debug node** and it will not start one
otherwise (#1301) — see `start_browser`. Nothing needs to be configured for that: it is the
server's only mode, so `.mcp.json` needs no capability env var.

Run (registered in .mcp.json):
    poetry install --with mcp
    SE_REMOTE_URL=http://127.0.0.1:4444 poetry run python tools/selenium_mcp_server.py

The hub (4444) is bound to host loopback by docker-compose.grid.yml (SELENIUM_GRID_HUB_BIND);
on the standalone topology the same port is bound by docker-compose.yml
(SELENIUM_STANDALONE_BIND). When working
from your laptop instead of the VPS, tunnel it first:
    ssh -L 4444:localhost:4444 -L 7900:localhost:7900 <vps>
then open http://localhost:7900/?autoconnect=1&password=secret to watch.
"""

import json
import os

from mcp.server.fastmcp import FastMCP
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By

from cqc_lem.utilities.selenium_util import DebugNodeUnavailable, apply_debug_node

mcp = FastMCP("selenium-lem")

_BY = {
    "css": By.CSS_SELECTOR,
    "xpath": By.XPATH,
    "id": By.ID,
    "name": By.NAME,
    "tag": By.TAG_NAME,
}

_state: dict = {"driver": None}


def _remote_url() -> str:
    url = os.getenv("SE_REMOTE_URL", "http://127.0.0.1:4444").rstrip("/")
    return url if url.endswith("/wd/hub") else f"{url}/wd/hub"


def _driver():
    if _state["driver"] is None:
        raise RuntimeError("No browser session. Call start_browser first.")
    return _state["driver"]


@mcp.tool()
def start_browser() -> str:
    """Attach to the live Grid's WATCHABLE debug node (the browser lemvnc displays).

    The debug node is **required**, not preferred (#1301). This server points at the shared hub, so
    a pooled session here would be one of the eight Chrome slots the engagement lanes are sized
    for — an agent opening a browser to look at a page would be competing with the commenting it
    is there to fix. There is no fall-back and no opt-out: when the node is full or absent this
    raises rather than borrowing a slot, and the caller should WAIT and retry, exactly as it does
    for the probe's breaker gate.

    Isolation is about the POOL, not the ACCOUNT. This node reaches the same live LinkedIn as
    everything else. The probe's read-only guard is NOT installed here: this browser is for
    interactive inspection and login, so it CAN type and click. Do not use it to post, comment,
    send invites/DMs, or change settings — a write from this session hits the owner's real account.
    """
    if _state["driver"] is not None:
        return f"Session already open at {_driver().current_url}"
    options = Options()
    options.set_capability("se:timeZone", os.getenv("SE_TIMEZONE", "America/New_York"))
    try:
        apply_debug_node(options, debug=True, required=True)
    except DebugNodeUnavailable as e:
        raise RuntimeError(
            f"No debug browser slot right now — WAIT and retry, do not escalate. {e}. "
            "The watchable node offers a fixed, small number of sessions and this server will "
            "never fall back to the pool the engagement lanes run on."
        ) from e
    _state["driver"] = webdriver.Remote(command_executor=_remote_url(), options=options)
    _state["driver"].set_window_size(1920, 1080)
    return f"Connected to {_remote_url()} on the watchable debug node"


@mcp.tool()
def navigate(url: str) -> str:
    """Navigate the live browser to a URL and report the resulting URL + title."""
    d = _driver()
    d.get(url)
    return json.dumps({"url": d.current_url, "title": d.title})


@mcp.tool()
def current_state() -> str:
    """Return the current URL and page title."""
    d = _driver()
    return json.dumps({"url": d.current_url, "title": d.title})


@mcp.tool()
def list_inputs() -> str:
    """List every <input> with its id/name/type/autocomplete/displayed.

    This is the fastest way to see which selectors a redesigned login/form actually exposes.
    """
    out = []
    for el in _driver().find_elements(By.TAG_NAME, "input"):
        try:
            out.append({
                "id": el.get_attribute("id"),
                "name": el.get_attribute("name"),
                "type": el.get_attribute("type"),
                "autocomplete": el.get_attribute("autocomplete"),
                "aria_label": el.get_attribute("aria-label"),
                "displayed": el.is_displayed(),
            })
        except Exception as e:  # noqa: BLE001 — surface stale elements, don't abort
            out.append({"error": str(e)})
    return json.dumps(out, ensure_ascii=False, indent=2)


@mcp.tool()
def list_buttons() -> str:
    """List visible <button> elements by text/aria-label (e.g. to find 'Sign in')."""
    out = []
    for el in _driver().find_elements(By.TAG_NAME, "button"):
        try:
            if el.is_displayed():
                out.append({
                    "text": (el.text or "").strip(),
                    "aria_label": el.get_attribute("aria-label"),
                    "type": el.get_attribute("type"),
                })
        except Exception:  # noqa: BLE001
            continue
    return json.dumps(out, ensure_ascii=False, indent=2)


@mcp.tool()
def type_into(strategy: str, selector: str, text: str, only_visible: bool = True) -> str:
    """Type text into the first (visible) element matched by strategy/selector.

    strategy: one of css, xpath, id, name, tag.
    """
    by = _BY[strategy]
    for el in _driver().find_elements(by, selector):
        if not only_visible or el.is_displayed():
            el.clear()
            el.send_keys(text)
            return f"Typed into ({strategy}, {selector!r})"
    return f"No matching {'visible ' if only_visible else ''}element for ({strategy}, {selector!r})"


@mcp.tool()
def click(strategy: str, selector: str, only_visible: bool = True) -> str:
    """Click the first (visible) element matched by strategy/selector."""
    by = _BY[strategy]
    for el in _driver().find_elements(by, selector):
        if not only_visible or el.is_displayed():
            try:
                el.click()
            except Exception:  # noqa: BLE001 — fall back to a JS click
                _driver().execute_script("arguments[0].click();", el)
            return f"Clicked ({strategy}, {selector!r})"
    return f"No matching {'visible ' if only_visible else ''}element for ({strategy}, {selector!r})"


@mcp.tool()
def screenshot(path: str = "/tmp/selenium_mcp.png") -> str:
    """Save a PNG screenshot of the live browser to `path`."""
    _driver().save_screenshot(path)
    return f"Saved screenshot to {path}"


@mcp.tool()
def page_source(limit: int = 4000) -> str:
    """Return the page HTML (truncated to `limit` chars)."""
    src = _driver().page_source
    return src[:limit]


@mcp.tool()
def execute_js(script: str) -> str:
    """Run JavaScript in the page and return the JSON-serialized result."""
    try:
        return json.dumps(_driver().execute_script(script))
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": str(e)})


@mcp.tool()
def quit_browser() -> str:
    """Close the WebDriver session."""
    if _state["driver"] is not None:
        try:
            _state["driver"].quit()
        finally:
            _state["driver"] = None
        return "Session closed"
    return "No session to close"


if __name__ == "__main__":
    mcp.run()
