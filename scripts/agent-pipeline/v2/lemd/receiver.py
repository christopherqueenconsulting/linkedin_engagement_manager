"""GitHub webhook receiver — the daemon's event source.

Runs as its own process, holding NO GitHub credential and doing NO GitHub work. Its entire job is:
verify the signature, dedupe the delivery, append a trimmed row to SQLite, answer 202. The daemon
and this process meet only in the database, so either can restart without the other noticing —
which matters because the daemon restarts on every deploy of the pipeline and deliveries must keep
landing across that window.

Why the separation is worth a second process: a receiver that also acted on events would need the
app credential, and this is the one component reachable (via the tunnel) from outside the box.
Keeping it credential-free means compromising it yields an attacker the ability to write rows into
a queue whose every entry is re-verified against GitHub before anything acts on it.

Three properties this file exists to guarantee:

* **Signature or nothing.** HMAC-SHA256 with `compare_digest`; an unsigned or wrongly-signed body
  is a 401 with no row written and no work done. The secret never appears in a log line.
* **202 means committed.** The response is sent only after the transaction commits. A receiver that
  acks then fails its write looks healthy to every monitor while silently dropping the events the
  pipeline runs on — the exact false-negative the self-heal design is built to catch, so it must
  not be manufacturable here.
* **Duplicates are free.** GitHub retries deliveries and a tunnel can replay them; `delivery_id` is
  unique, so re-delivery is a no-op rather than a second dispatch.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import sqlite3
import time
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

# stdlib logging, NOT cqc_lem.utilities.logger. The repo convention says to use the structured
# logger, and that convention is right for everything that runs INSIDE the application — but this
# process runs on the HOST, as its own systemd unit, outside the app's venv and outside its Docker
# image. Importing cqc_lem here makes the receiver unstartable (ModuleNotFoundError) and would drag
# the app's dependency tree into the one component that is reachable from the internet.
# Keep this file stdlib-only.

from . import db

LOG = logging.getLogger("lemd.receiver")

#: Only these events are recorded. Anything else GitHub sends is acknowledged and dropped, so
#: widening the app's subscription list cannot silently fill the queue with rows nothing reads.
SUBSCRIBED = frozenset(
    {
        "ping",
        "issues",
        "issue_comment",
        "pull_request",
        "pull_request_review",
        "pull_request_review_thread",
        "check_suite",
        "merge_group",
    }
)

#: Bodies larger than this are refused before parsing. GitHub payloads run well under 1 MB; this is
#: a memory guard against a malformed or hostile sender, not a functional limit.
MAX_BODY = 2 * 1024 * 1024


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    """Constant-time HMAC-SHA256 check of GitHub's `X-Hub-Signature-256`.

    Args:
        secret: Shared secret configured on the GitHub App's webhook.
        body: Raw request body, exactly as received — re-serialising before hashing would change
            the bytes and fail every legitimate delivery.
        header: The `sha256=<hex>` header value, or None when absent.

    Returns:
        True only for a well-formed header whose digest matches. An empty secret returns False:
        a misconfigured receiver must refuse everything, never accept everything.
    """
    if not secret or not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, sha256).hexdigest()
    return hmac.compare_digest(expected, header)


def extract(event: str, payload: dict[str, Any]) -> tuple[str | None, int | None, str | None]:
    """Pull the (action, number, head_sha) the daemon needs, ignoring the rest of the payload.

    Stored rows are trimmed to these fields plus a small subset because a full GitHub payload is
    tens of kilobytes of mostly-irrelevant nesting, and the queue database is meant to stay small
    enough to be disposable.
    """
    action = payload.get("action")
    number: int | None = None
    head_sha: str | None = None

    if "pull_request" in payload and isinstance(payload["pull_request"], dict):
        pr = payload["pull_request"]
        number = pr.get("number")
        head_sha = (pr.get("head") or {}).get("sha")
    elif "issue" in payload and isinstance(payload["issue"], dict):
        number = payload["issue"].get("number")
    if event == "check_suite":
        cs = payload.get("check_suite") or {}
        head_sha = cs.get("head_sha")
        prs = cs.get("pull_requests") or []
        if prs and isinstance(prs[0], dict):
            number = prs[0].get("number")
    elif event == "merge_group":
        mg = payload.get("merge_group") or {}
        head_sha = mg.get("head_sha")
    return action, (int(number) if isinstance(number, int) else None), head_sha


def trim_payload(event: str, payload: dict[str, Any]) -> str:
    """Keep only the fields the event translator reads.

    Deliberately includes `sender.login` for label/comment events: the trust boundary is re-verified
    against GitHub's timeline before any dispatch, but the sender is useful context in the audit
    trail when reconstructing why an item moved.
    """
    keep: dict[str, Any] = {"sender": (payload.get("sender") or {}).get("login")}
    if event == "issues" or event == "pull_request":
        keep["label"] = (payload.get("label") or {}).get("name")
        keep["draft"] = (payload.get("pull_request") or {}).get("draft")
        keep["merged"] = (payload.get("pull_request") or {}).get("merged")
    if event == "issue_comment":
        comment = payload.get("comment") or {}
        keep["comment_id"] = comment.get("id")
    if event == "check_suite":
        keep["conclusion"] = (payload.get("check_suite") or {}).get("conclusion")
    if event == "merge_group":
        keep["reason"] = (payload.get("merge_group") or {}).get("reason")
    return json.dumps(keep, separators=(",", ":"))


class Handler(BaseHTTPRequestHandler):
    """One request handler per delivery. Wired by `serve()`; not constructed directly."""

    server_version = "lemd-receiver/1"
    #: Set by serve() on the server instance.
    secret: str = ""
    db_path: str = ""

    def log_message(self, fmt: str, *args: Any) -> None:
        """Route access logging through our logger instead of stderr."""
        LOG.info("%s - %s", self.address_string(), fmt % args)

    def _reply(self, code: int, body: str = "") -> None:
        """Send a short plain-text response."""
        raw = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        if raw:
            self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's naming
        """Health probe.

        Performs a real write, not just a connection check: the failure this must catch is a
        receiver that answers requests while its database has become unwritable, which no
        liveness-only probe can distinguish from health.
        """
        if self.path.rstrip("/") != "/healthz":
            self._reply(404, "not found")
            return
        try:
            conn = db.connect(self.server.db_path)  # type: ignore[attr-defined]
            try:
                db.kv_set(conn, "receiver_healthz_at", str(int(time.time())))
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001 - any failure means unhealthy
            LOG.error("healthz write failed: %s", exc)
            self._reply(500, "db write failed")
            return
        self._reply(200, "ok")

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's naming
        """Verify, dedupe, persist, acknowledge — in that order."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._reply(400, "bad length")
            return
        if length <= 0 or length > MAX_BODY:
            # Drain (bounded, discarded) before answering. Replying while the client is still
            # sending closes the socket mid-stream, and the sender sees a connection reset instead
            # of the status — GitHub would log a delivery failure and retry a body that can never
            # succeed. Reading into a discard buffer costs nothing and still refuses the payload
            # before the expensive part, which is JSON parsing.
            remaining = min(length, MAX_BODY * 2) if length > 0 else 0
            while remaining > 0:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
            self._reply(413, "bad body size")
            return

        body = self.rfile.read(length)
        if not verify_signature(self.server.secret, body, self.headers.get("X-Hub-Signature-256")):  # type: ignore[attr-defined]
            # No detail in the response and no body in the log: a signature oracle would let a
            # caller probe the secret one guess at a time.
            LOG.warning("rejected delivery: bad or missing signature")
            self._reply(401, "bad signature")
            return

        event = self.headers.get("X-GitHub-Event") or ""
        delivery = self.headers.get("X-GitHub-Delivery") or ""
        if not delivery:
            self._reply(400, "missing delivery id")
            return
        if event not in SUBSCRIBED:
            # Acknowledged so GitHub does not retry something we will never want.
            self._reply(202, "ignored")
            return

        try:
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise ValueError("payload is not an object")
        except ValueError as exc:
            LOG.warning("rejected delivery %s: unparseable payload (%s)", delivery, exc)
            self._reply(400, "bad json")
            return

        action, number, head_sha = extract(event, payload)
        try:
            conn = db.connect(self.server.db_path)  # type: ignore[attr-defined]
            try:
                # One transaction: the event row and the freshness marker the degraded-mode
                # detector reads must land together, or a crash between them makes the pipeline
                # believe deliveries stopped while they were in fact arriving.
                with db.transaction(conn):
                    fresh = db.record_event(
                        conn,
                        delivery_id=delivery,
                        event=event,
                        action=action,
                        number=number,
                        head_sha=head_sha,
                        payload=trim_payload(event, payload),
                    )
                    db.kv_set(conn, "last_webhook_at", str(int(time.time())))
            finally:
                conn.close()
        except sqlite3.Error as exc:
            # 500 (not 202) so GitHub retries and the delivery is not lost. This is the branch that
            # makes "202 means committed" true.
            LOG.error("delivery %s NOT stored: %s", delivery, exc)
            self._reply(500, "storage failed")
            return

        LOG.info("stored %s/%s delivery=%s number=%s new=%s", event, action, delivery, number, fresh)
        self._reply(202, "queued" if fresh else "duplicate")


def serve(
    *,
    binds: list[tuple[str, int]],
    db_path: str | Path,
    secret: str | None = None,
) -> ThreadingHTTPServer:
    """Start the receiver.

    Args:
        binds: Address/port pairs to listen on. The deployment binds the docker bridge gateway
            (reachable from the cloudflared container) and loopback (for `healthz`) — never a
            public interface; the tunnel is the only path in.
        db_path: Queue database. Created if absent so first boot needs no ordering with the daemon.
        secret: Webhook secret; defaults to `$LEM_WEBHOOK_SECRET`.

    Returns:
        The running server. The caller owns `serve_forever()`.

    Raises:
        RuntimeError: When no secret is configured — refusing to start is the only safe response,
            since a receiver with an empty secret would reject every real delivery anyway while
            looking healthy to systemd.
    """
    secret = secret if secret is not None else os.environ.get("LEM_WEBHOOK_SECRET", "")
    if not secret:
        raise RuntimeError("LEM_WEBHOOK_SECRET is not set — refusing to start unauthenticated")

    conn = db.connect(db_path)
    conn.close()

    host, port = binds[0]
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.secret = secret  # type: ignore[attr-defined]
    httpd.db_path = str(db_path)  # type: ignore[attr-defined]
    LOG.info("receiver listening on %s:%s db=%s", host, port, db_path)
    return httpd


def main() -> None:
    """Entry point for `python -m lemd.receiver`."""
    import argparse

    from .config import _str, load

    ap = argparse.ArgumentParser(description="LEM agent-pipeline v2 webhook receiver")
    ap.add_argument("--bind", action="append", default=[], metavar="HOST:PORT")
    ap.add_argument("--db", default=None)
    args = ap.parse_args()

    cfg = load()
    # Default to the gateway of the network cloudflared actually runs on. This box has TWO bridges
    # — docker0 (172.17.0.1) and lem_default (172.18.0.1) — and the tunnel container is on the
    # latter, so binding the wrong one yields a receiver that is up, healthy, and unreachable.
    # Overridable because the bridge address is assigned by compose and can change if the stack is
    # recreated; the deployment checks it (`ip -4 -o addr show`) rather than trusting this default.
    binds = []
    for b in args.bind or [_str(cfg.raw, "LEM_WEBHOOK_BIND", "172.18.0.1:8420"), "127.0.0.1:8420"]:
        h, _, p = b.rpartition(":")
        binds.append((h or "127.0.0.1", int(p)))
    httpd = serve(binds=binds, db_path=args.db or cfg.db_path)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        LOG.info("receiver stopping")
        httpd.shutdown()


if __name__ == "__main__":
    main()
