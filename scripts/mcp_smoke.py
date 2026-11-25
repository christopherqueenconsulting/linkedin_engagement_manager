"""End-to-end smoke test of the LEM MCP server over real HTTP (epic #2682, plan §9 "Scripted E2E").

Run by hand, never in CI. It speaks JSON-RPC to `POST <base>/api/mcp` exactly as an MCP client
does, and exits non-zero on the first mismatch it reports (it keeps going to report the rest):

1. `initialize`.
2. `tools/list` equals the registry for the token's scope (read from `lem_whoami`); the
   internal/sandbox-only `lem_approve_item` may be absent (owner ruling O1). `lem_add_story_draft`
   (#2804) is in the registry for BOTH scopes, so it must be listed on either token; it is a write,
   so step 3 never calls it (a smoke run must not leave an unconfirmed fact in a real story bank).
3. Every READ tool is called with minimal valid arguments and must not come back `isError`
   (`lem_get_post` uses the first post `lem_list_posts` returns, and is skipped when there is none;
   `lem_list_outreach` is called once per queue on an `mcp_outreach` token;
   `lem_get_setup_checklist` (#2802) is a read and is called once with no arguments).
4. A token in the query string gets 400 `token_in_url_refused`. With a throwaway token (minted by
   `--mint-local-for`), the token must ALSO be revoked by that request.
5. A bad bearer gets 401 `invalid_token`.
6. With `--write --recipient URL --account-kind KIND` on an `mcp_outreach` token (W2):
   `lem_queue_dm` lands pending with `source='mcp'`, the same `client_request_id` replays instead of
   writing twice, reusing it with other arguments is refused, and an `action` on an edit tool is
   refused. Then the approval rule: ONLY with `--account-kind sandbox` (where sends are a dry run)
   does it approve the DM through `lem_approve_item`. On any other kind it never approves: a
   standard account must refuse with `agent_may_not_approve`, an internal one is skipped (approving
   there is a REAL send), and the queued DM is then canceled (straight through the DB with
   `--mint-local-for`; otherwise the script prints the DM id to cancel in LEM).
7. With `--mint-local-for`, the minted token is revoked at the end and the next call must be 401.
8. With `--settings` (#2800, needs `mcp-settings-writes` on for the account): change `tone` to a
   marker value, check the diff, check a daily cap is refused, undo, and check `tone` is back. It
   writes the account's real setting and puts it back; skipped when the tools are not listed. It
   also checks the change-request path WITHOUT filing one (#2803): a gate change through
   `lem_update_settings` is refused naming `lem_request_settings_change`, and a non-gate field sent to
   `lem_request_settings_change` is refused. No agent tool can approve, so a smoke-filed request would
   sit in the user's queue; the smoke never files one.

Usage, against a deployed stack (token from the Agents tab, kept in the environment):

    PYTHONPATH=src python scripts/mcp_smoke.py --read-only --base https://<host> \
        --token "$LEM_MCP_TOKEN"
    PYTHONPATH=src python scripts/mcp_smoke.py --write --recipient https://www.linkedin.com/in/<test>/ \
        --account-kind sandbox --base https://<host> --token "$LEM_MCP_TOKEN"

Against a LOCAL stack only, `--mint-local-for EMAIL` mints the tokens itself straight through the
database facade (no step-up ceremony), and is refused for any `--base` that is not localhost: it is
a test-stack convenience, never a way around the mint's step-up on a real deployment.

The token is never printed. Exit codes: 0 every check passed, 1 a check failed, 2 bad usage.
"""

import argparse
import json
import logging
import os
import secrets
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional, Sequence, TextIO
from urllib.parse import urlparse

import httpx2

os.environ.setdefault("LEM_TELEMETRY_MUTED", "1")

_ACCEPT = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
_OUTREACH_KINDS = ("dms", "connection_requests", "leads", "lead_signals", "catchup_touches",
                   "outreach_targets")

# Minimal valid arguments per read tool; anything not listed takes none.
_ARGS: dict[str, dict[str, Any]] = {
    "lem_explain_lem": {"topic": "overview"},
    "lem_open_in_lem": {"destination": "dashboard"},
    "lem_list_posts": {"limit": 5},
}


class Smoke:
    """One smoke run: the HTTP client, the running tally, and where results are written."""

    def __init__(self, base: str, out: TextIO, client: Optional[httpx2.Client] = None) -> None:
        """Prepare a run against `base`.

        Args:
            base: The deployment's base URL, e.g. `https://lem.example.com`.
            out: Where each check's line is written.
            client: An HTTP client to use instead of a fresh one (tests pass an in-process one).
        """
        self.base = base.rstrip("/")
        self.out = out
        self.client = client or httpx2.Client(timeout=30)
        self.failures = 0
        self._ids = 0

    def check(self, ok: bool, label: str, detail: str = "") -> bool:
        """Record one check and write its line."""
        if not ok:
            self.failures += 1
        self.out.write(f"[{'PASS' if ok else 'FAIL'}] {label}{(' - ' + detail) if detail else ''}\n")
        return ok

    def post(self, token: Optional[str], method: str, params: Optional[dict] = None,
             query: str = "") -> httpx2.Response:
        """POST one JSON-RPC message to the MCP endpoint."""
        self._ids += 1
        body: dict[str, Any] = {"jsonrpc": "2.0", "id": self._ids, "method": method}
        if params is not None:
            body["params"] = params
        headers = dict(_ACCEPT)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return self.client.post(f"{self.base}/api/mcp{query}", content=json.dumps(body),
                                headers=headers)

    def result(self, token: str, method: str, params: Optional[dict] = None) -> Optional[dict]:
        """The JSON-RPC `result`, or None (with a failed check) on anything else."""
        resp = self.post(token, method, params)
        try:
            payload = resp.json()
        except ValueError:
            payload = {}
        if resp.status_code != 200 or "result" not in payload:
            self.check(False, method, f"HTTP {resp.status_code} {str(payload)[:200]}")
            return None
        return payload["result"]

    def tool(self, token: str, name: str, arguments: Optional[dict] = None) -> Optional[dict]:
        """Call one tool; a failed check when it errors. Returns its structured content."""
        result = self.result(token, "tools/call", {"name": name, "arguments": arguments or {}})
        if result is None:
            return None
        structured = result.get("structuredContent") or {}
        error = structured.get("error") if result.get("isError") else None
        self.check(error is None, f"tool {name}", f"{error.get('code')}" if error else "")
        return None if error else structured

    def refusal(self, token: str, name: str, arguments: dict) -> Optional[str]:
        """Call a tool that is EXPECTED to refuse; return its error code (None when it did not)."""
        result = self.result(token, "tools/call", {"name": name, "arguments": arguments}) or {}
        if not result.get("isError"):
            return None
        return ((result.get("structuredContent") or {}).get("error") or {}).get("code")


def _registry(scope: str) -> Optional[list[Any]]:
    try:
        from cqc_lem.api.mcp.tools import tools_for_scope
    except Exception:  # noqa: BLE001 - run outside the repo: the comparison is skipped, not failed
        return None
    return list(tools_for_scope(scope))


def _write_tool_names() -> set[str]:
    try:
        from cqc_lem.api.mcp.tools import TOOLS
    except Exception:  # noqa: BLE001 - outside the repo: every tool not named lem_get/list is a write
        return set()
    return {spec.name for spec in TOOLS if spec.write}


def run_read_checks(smoke: Smoke, token: str) -> Optional[str]:
    """Steps 1-3. Returns the token's scope, or None when it could not be read."""
    init = smoke.result(token, "initialize", {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "lem-mcp-smoke", "version": "1"}})
    smoke.check(bool(init) and (init.get("serverInfo") or {}).get("name") == "lem", "initialize")

    whoami = smoke.tool(token, "lem_whoami") or {}
    scope = whoami.get("scope")
    smoke.check(scope in ("mcp", "mcp_outreach"), "token scope", str(scope))
    smoke.out.write(f"       account: {whoami.get('account_name')}\n")

    listed = smoke.result(token, "tools/list") or {}
    names = [t.get("name") for t in listed.get("tools") or []]
    specs = _registry(scope) if scope else None
    if specs is None:
        smoke.check(bool(names), "tools/list (registry not importable: non-empty only)")
    else:
        full = [spec.name for spec in specs]
        # `lem_approve_item` is listed only on an internal or sandbox account (owner ruling O1), and a
        # tool behind its own flag (the settings tools, #2800) only while that flag is on.
        accepted = [full, [spec.name for spec in specs if not spec.approval],
                    [spec.name for spec in specs if not getattr(spec, "flag", None)],
                    [spec.name for spec in specs
                     if not spec.approval and not getattr(spec, "flag", None)]]
        smoke.check(names in accepted, "tools/list equals the registry",
                    "" if names in accepted else f"got {sorted(set(names) ^ set(full))}")

    posts = smoke.tool(token, "lem_list_posts", _ARGS["lem_list_posts"]) or {}
    first = next((p.get("post_id") for p in posts.get("posts") or []), None)
    writes = _write_tool_names()
    for name in names:
        if name in ("lem_whoami", "lem_list_posts") or name in writes:
            continue
        if name == "lem_get_post":
            if first is None:
                smoke.out.write("[SKIP] tool lem_get_post - the account has no posts\n")
            else:
                smoke.tool(token, name, {"post_id": first})
        elif name == "lem_list_outreach":
            for kind in _OUTREACH_KINDS:
                smoke.tool(token, name, {"kind": kind, "limit": 5})
        else:
            smoke.tool(token, name, _ARGS.get(name))
    return scope


def run_write_checks(smoke: Smoke, token: str, recipient: str, account_kind: str,
                     cancel_dm: Optional[Callable[[int], bool]] = None) -> None:
    """Step 6 (W2): one pending DM, its replay, the reuse refusal, and the approval rule.

    Approves only on a declared SANDBOX account; on every other kind the DM is canceled afterwards
    (through `cancel_dm` when the script has the local DB, else by the operator in LEM).
    """
    listed = smoke.result(token, "tools/list") or {}
    names = {t.get("name") for t in listed.get("tools") or []}
    if "lem_queue_dm" not in names:
        smoke.out.write("[SKIP] write checks - they need an mcp_outreach token with writes on\n")
        return
    when = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    args = {"recipient_profile_url": recipient, "message": "LEM MCP smoke test: please reject me.",
            "scheduled_datetime": when, "client_request_id": "smoke-" + secrets.token_hex(6)}
    queued = smoke.tool(token, "lem_queue_dm", args) or {}
    smoke.check(queued.get("status") == "pending" and queued.get("source") == "mcp",
                "lem_queue_dm lands pending with source=mcp")
    again = smoke.tool(token, "lem_queue_dm", args) or {}
    smoke.check(again.get("replayed") is True and again.get("id") == queued.get("id"),
                "a retried write replays instead of writing twice")
    reused = smoke.refusal(token, "lem_queue_dm", {**args, "message": "different text"})
    smoke.check(reused == "client_request_id_reused", "a reused request id is refused", str(reused))
    edit = smoke.refusal(token, "lem_edit_lead_signal_draft",
                         {"signal_id": 1, "draft_response": "x", "action": "approve"})
    smoke.check(edit == "invalid_arguments", "an approve through an edit tool is refused", str(edit))
    if queued.get("id") is None:
        return
    dm_id = queued["id"]
    approve = {"kind": "dm", "id": dm_id}
    listed = "lem_approve_item" in names
    if account_kind == "sandbox":
        smoke.check(listed, "lem_approve_item is listed on the sandbox account")
        if listed:
            approved = smoke.tool(token, "lem_approve_item", approve) or {}
            smoke.check(approved.get("status") == "approved", "lem_approve_item approves (sandbox)")
        return
    if account_kind == "standard":
        smoke.check(not listed, "lem_approve_item is not listed on a standard account")
        code = smoke.refusal(token, "lem_approve_item", approve)
        smoke.check(code == "agent_may_not_approve", "approve is refused on a standard account",
                    str(code))
    else:
        smoke.out.write("[SKIP] lem_approve_item - not on an internal account: approving there is "
                        "a REAL send\n")
    if cancel_dm is not None:
        smoke.check(cancel_dm(int(dm_id)), "the smoke DM was canceled")
    else:
        smoke.out.write(f"[ACTION] cancel the smoke DM #{dm_id} in LEM -> Content -> DMs\n")


def run_settings_checks(smoke: Smoke, token: str) -> None:
    """Step 8 (#2800): change `tone`, see the diff, see a cap refused, undo, see `tone` restored."""
    listed = smoke.result(token, "tools/list") or {}
    names = {t.get("name") for t in listed.get("tools") or []}
    if "lem_update_settings" not in names:
        smoke.out.write("[SKIP] settings checks - lem_update_settings is not listed "
                        "(mcp-settings-writes is off for this account)\n")
        return
    marker = "smoke-" + secrets.token_hex(3)
    current = ((smoke.tool(token, "lem_get_engagement_settings") or {}).get("preferences") or {})
    original = current.get("tone")
    smoke.out.write(f"       settings round trip: original tone = {original!r}. If this run dies "
                    "before the undo, set tone back to that value in LEM -> Account.\n")
    changed = smoke.tool(token, "lem_update_settings",
                         {"changes": {"tone": marker}, "client_request_id": "smoke-" + marker}) or {}
    diff = (changed.get("changed") or {}).get("tone") or {}
    smoke.check(diff.get("after") == marker and diff.get("before") == original,
                "lem_update_settings changes tone and reports before/after")
    capped = smoke.refusal(token, "lem_update_settings", {"changes": {"max_comments_per_day": 1}})
    smoke.check(capped == "cap_not_editable", "a daily cap is refused", str(capped))
    gated = smoke.refusal(token, "lem_update_settings", {"changes": {"connection_request_mode": "auto_approve"}})
    smoke.check(gated == "needs_change_request", "a gate change is refused toward lem_request_settings_change",
                str(gated))
    not_gate = smoke.refusal(token, "lem_request_settings_change",
                             {"changes": {"tone": "x"}, "reason": "smoke check, never filed"})
    smoke.check(not_gate == "not_a_gate_field", "a non-gate field cannot be requested", str(not_gate))
    undo_id = changed.get("undo_id")
    smoke.out.write(f"       undo_id = {undo_id} (lem_undo_settings_change restores tone)\n")
    if not undo_id:
        smoke.check(False, "lem_update_settings returns an undo_id")
        return
    restored = smoke.tool(token, "lem_undo_settings_change", {"undo_id": undo_id}) or {}
    smoke.check("tone" in (restored.get("restored") or {}), "lem_undo_settings_change restores tone")
    after = ((smoke.tool(token, "lem_get_engagement_settings") or {}).get("preferences") or {})
    smoke.check((after.get("tone") or None) == (original or None), "tone is back to its old value")


def run_refusal_checks(smoke: Smoke, throwaway: Optional[str],
                       still_live: Optional[Callable[[str], bool]]) -> None:
    """Steps 4-5: a query-string token is refused (and revoked), a bad bearer is 401."""
    value = throwaway or ("lem_mcp_" + secrets.token_urlsafe(24))
    resp = smoke.post(None, "tools/list", query=f"?token={value}")
    reason = _reason(resp)
    smoke.check(resp.status_code == 400 and reason == "token_in_url_refused",
                "a query-string token is refused", f"HTTP {resp.status_code} {reason}")
    if throwaway and still_live is not None:
        smoke.check(not still_live(throwaway), "the exposed token was revoked")

    resp = smoke.post("lem_mcp_" + secrets.token_urlsafe(24), "tools/list")
    smoke.check(resp.status_code == 401 and _reason(resp) == "invalid_token",
                "a bad bearer is 401", f"HTTP {resp.status_code}")


def _reason(resp: httpx2.Response) -> Optional[str]:
    try:
        return resp.json().get("reason")
    except ValueError:
        return None


class _LocalMint:
    """Mint and revoke tokens straight through the DB facade. LOCAL stacks only."""

    def __init__(self, email: str, scope: str) -> None:
        from cqc_lem.utilities import db

        self.db = db
        self.user_id = db.get_user_id(email)
        if not self.user_id:
            raise SystemExit(f"--mint-local-for: no user with that email on this stack ({email})")
        self.scope = scope

    def mint(self, label: str) -> str:
        token = self.db.create_session(self.user_id, label=label, scope=self.scope, ttl_hours=1,
                                       token_prefix=self.db.MCP_TOKEN_PREFIX)
        if not token:
            raise SystemExit("--mint-local-for: the session could not be created")
        return token

    def live(self, token: str) -> bool:
        return bool(self.db.resolve_session_checked(token))

    def revoke(self, token: str) -> bool:
        session_id = self.db.get_session_id(token)
        return bool(session_id) and bool(self.db.revoke_session(self.user_id, session_id))

    def cancel_dm(self, dm_id: int) -> bool:
        if self.db.get_scheduled_dm_user_id(dm_id) != self.user_id:
            return False
        return bool(self.db.update_scheduled_dm_status(dm_id, self.db.ScheduledDmStatus.CANCELED))


def main(argv: Optional[Sequence[str]] = None, out: TextIO = sys.stdout,
         client: Optional[httpx2.Client] = None) -> int:
    """Run the smoke test.

    Args:
        argv: Arguments, defaulting to `sys.argv[1:]`.
        out: Where results are written.
        client: An HTTP client to use (tests pass an in-process one).

    Returns:
        The process exit code.
    """
    parser = argparse.ArgumentParser(description="Smoke-test the LEM MCP server.")
    parser.add_argument("--base", required=True, help="Deployment base URL.")
    parser.add_argument("--token", default=os.getenv("LEM_MCP_TOKEN"),
                        help="An MCP token (default: $LEM_MCP_TOKEN). Never printed.")
    parser.add_argument("--read-only", action="store_true",
                        help="Run the read checks only (the default).")
    parser.add_argument("--write", action="store_true",
                        help="Also run the W2 write checks (needs --recipient; queues ONE DM).")
    parser.add_argument("--settings", action="store_true",
                        help="Also run the settings round trip (changes tone, then undoes it; needs "
                             "mcp-settings-writes on).")
    parser.add_argument("--recipient", metavar="URL",
                        help="The fixed test recipient's LinkedIn profile URL for --write.")
    parser.add_argument("--account-kind", choices=("standard", "internal", "sandbox"),
                        help="The account's kind, stated by the operator, for --write. Only "
                             "'sandbox' approves; every other kind cancels the smoke DM.")
    parser.add_argument("--mint-local-for", metavar="EMAIL",
                        help="LOCAL stack only: mint the tokens for this user through the DB.")
    parser.add_argument("--scope", choices=("mcp", "mcp_outreach"), default="mcp",
                        help="The scope --mint-local-for mints (default mcp).")
    args = parser.parse_args(argv)
    # The HTTP client logs every request URL at INFO, and step 4 deliberately sends one with a token
    # in it. That token is revoked by the same request, but this script never prints a token.
    for name in ("httpx", "httpx2"):
        logging.getLogger(name).setLevel(logging.WARNING)

    host = (urlparse(args.base).hostname or "").lower()
    minter = None
    if args.mint_local_for:
        if host not in _LOCAL_HOSTS:
            out.write("Refusing --mint-local-for: --base is not localhost.\n")
            return 2
        minter = _LocalMint(args.mint_local_for, args.scope)
    token = minter.mint("mcp-smoke") if minter else args.token
    if not token:
        out.write("No token: pass --token, set LEM_MCP_TOKEN, or use --mint-local-for locally.\n")
        return 2
    if args.write and (args.read_only or not args.recipient or not args.account_kind):
        out.write("--write needs --recipient and --account-kind, and cannot be combined with "
                  "--read-only.\n")
        return 2

    smoke = Smoke(args.base, out, client)
    run_read_checks(smoke, token)
    if args.write:
        run_write_checks(smoke, token, args.recipient, args.account_kind,
                         minter.cancel_dm if minter else None)
    if args.settings:
        run_settings_checks(smoke, token)
    throwaway = minter.mint("mcp-smoke-exposed") if minter else None
    run_refusal_checks(smoke, throwaway, minter.live if minter else None)
    if minter:
        smoke.check(minter.revoke(token), "revoke the smoke token")
        resp = smoke.post(token, "tools/list")
        smoke.check(resp.status_code == 401, "a revoked token is 401", f"HTTP {resp.status_code}")
    else:
        out.write("[SKIP] revoke-then-401 - revoke the token in LEM -> Agents, then re-run step 5\n")

    out.write(f"{'OK' if smoke.failures == 0 else 'FAILED'}: {smoke.failures} failed check(s)\n")
    return 0 if smoke.failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
