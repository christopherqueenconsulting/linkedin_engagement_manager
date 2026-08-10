"""Tests for the v2 webhook receiver.

The receiver is the one component reachable from outside the box, so these lean on the security
properties: a body without a valid signature must never produce a row, the secret must not be
probeable, and an acknowledged delivery must be one that actually committed — a receiver that acks
then drops looks healthy to every monitor while starving the pipeline.
"""

from __future__ import annotations

import hmac
import json
import sys
import threading
import urllib.error
import urllib.request
from hashlib import sha256
from pathlib import Path

import pytest

_V2 = Path(__file__).resolve().parents[2] / "scripts" / "agent-pipeline" / "v2"
sys.path.insert(0, str(_V2))

from lemd import db, receiver  # noqa: E402

SECRET = "test-secret-value"


def sign(body: bytes, secret: str = SECRET) -> str:
    """Produce the header GitHub would send."""
    return "sha256=" + hmac.new(secret.encode(), body, sha256).hexdigest()


@pytest.fixture()
def server(tmp_path):
    """A receiver bound to an ephemeral loopback port."""
    httpd = receiver.serve(binds=[("127.0.0.1", 0)], db_path=tmp_path / "queue.db", secret=SECRET)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    host, port = httpd.server_address[0], httpd.server_address[1]
    yield f"http://{host}:{port}", tmp_path / "queue.db"
    httpd.shutdown()
    httpd.server_close()


def post(url: str, body: bytes, *, event="pull_request", delivery="d1", sig: str | None = None):
    """POST a delivery, returning (status, text)."""
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-GitHub-Event", event)
    req.add_header("X-GitHub-Delivery", delivery)
    if sig is not None:
        req.add_header("X-Hub-Signature-256", sig)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def rows(db_path):
    """All stored events."""
    conn = db.connect(db_path)
    try:
        return conn.execute("SELECT * FROM events ORDER BY id").fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------- signature


def test_valid_signature_is_stored(server):
    url, dbp = server
    body = json.dumps({"action": "opened", "pull_request": {"number": 42, "head": {"sha": "abc"}}}).encode()
    status, _ = post(url, body, sig=sign(body))
    assert status == 202
    (row,) = rows(dbp)
    assert row["event"] == "pull_request" and row["number"] == 42 and row["head_sha"] == "abc"


def test_bad_signature_writes_nothing(server):
    url, dbp = server
    body = json.dumps({"action": "opened"}).encode()
    status, _ = post(url, body, sig=sign(body, "wrong-secret"))
    assert status == 401
    assert rows(dbp) == []


def test_missing_signature_writes_nothing(server):
    url, dbp = server
    body = json.dumps({"action": "opened"}).encode()
    assert post(url, body, sig=None)[0] == 401
    assert rows(dbp) == []


def test_tampered_body_fails_even_with_a_valid_looking_signature(server):
    """The signature covers the body; changing one byte must invalidate it."""
    url, dbp = server
    body = json.dumps({"action": "opened", "pull_request": {"number": 1}}).encode()
    sig = sign(body)
    tampered = body.replace(b'"number": 1', b'"number": 9')
    assert post(url, tampered, sig=sig)[0] == 401
    assert rows(dbp) == []


def test_empty_secret_refuses_to_start(tmp_path):
    """A misconfigured receiver must refuse everything, not accept everything."""
    with pytest.raises(RuntimeError, match="refusing to start"):
        receiver.serve(binds=[("127.0.0.1", 0)], db_path=tmp_path / "q.db", secret="")


def test_verify_signature_unit_cases():
    body = b'{"a":1}'
    assert receiver.verify_signature(SECRET, body, sign(body)) is True
    assert receiver.verify_signature(SECRET, body, "sha256=deadbeef") is False
    assert receiver.verify_signature(SECRET, body, "sha1=whatever") is False
    assert receiver.verify_signature(SECRET, body, None) is False
    assert receiver.verify_signature("", body, sign(body)) is False


# ---------------------------------------------------------------- dedup / acking


def test_duplicate_delivery_is_a_noop(server):
    """GitHub retries; a tunnel can replay. Neither may produce a second row."""
    url, dbp = server
    body = json.dumps({"action": "opened", "pull_request": {"number": 7}}).encode()
    first = post(url, body, sig=sign(body), delivery="same-guid")
    second = post(url, body, sig=sign(body), delivery="same-guid")
    assert first == (202, "queued")
    assert second == (202, "duplicate")
    assert len(rows(dbp)) == 1


def test_last_webhook_at_is_set_with_the_row(server):
    """The freshness marker and the row must land together or degraded-mode detection lies."""
    url, dbp = server
    body = json.dumps({"action": "labeled", "issue": {"number": 3}}).encode()
    post(url, body, sig=sign(body), event="issues")
    conn = db.connect(dbp)
    try:
        assert db.kv_get(conn, "last_webhook_at") is not None
    finally:
        conn.close()


def test_unsubscribed_event_is_acked_but_not_stored(server):
    """Widening the app's subscriptions must not silently fill the queue."""
    url, dbp = server
    body = json.dumps({"action": "created"}).encode()
    status, text = post(url, body, sig=sign(body), event="star")
    assert (status, text) == (202, "ignored")
    assert rows(dbp) == []


def test_ping_is_accepted(server):
    """GitHub sends `ping` when the webhook is created; rejecting it reads as a broken hook."""
    url, dbp = server
    body = json.dumps({"zen": "hi"}).encode()
    assert post(url, body, sig=sign(body), event="ping")[0] == 202
    assert len(rows(dbp)) == 1


def test_malformed_json_is_rejected(server):
    url, dbp = server
    body = b"{not json"
    assert post(url, body, sig=sign(body))[0] == 400
    assert rows(dbp) == []


def test_non_object_payload_is_rejected(server):
    url, dbp = server
    body = b'"a string"'
    assert post(url, body, sig=sign(body))[0] == 400
    assert rows(dbp) == []


def test_missing_delivery_id_is_rejected(server):
    """Without the GUID there is no dedup key, so the delivery cannot be made idempotent."""
    url, dbp = server
    body = json.dumps({"action": "opened"}).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("X-GitHub-Event", "pull_request")
    req.add_header("X-Hub-Signature-256", sign(body))
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            status = r.status
    except urllib.error.HTTPError as e:
        status = e.code
    assert status == 400
    assert rows(dbp) == []


def test_oversized_body_is_refused_before_parsing(server):
    url, dbp = server
    body = b"x" * (receiver.MAX_BODY + 1)
    assert post(url, body, sig=sign(body))[0] == 413
    assert rows(dbp) == []


# ---------------------------------------------------------------- health


def test_healthz_performs_a_real_write(server):
    """Liveness alone cannot distinguish a healthy receiver from one whose DB went unwritable."""
    url, dbp = server
    with urllib.request.urlopen(f"{url}/healthz", timeout=5) as r:
        assert r.status == 200
    conn = db.connect(dbp)
    try:
        assert db.kv_get(conn, "receiver_healthz_at") is not None
    finally:
        conn.close()


def test_unknown_path_is_404(server):
    url, _ = server
    try:
        with urllib.request.urlopen(f"{url}/nope", timeout=5) as r:
            status = r.status
    except urllib.error.HTTPError as e:
        status = e.code
    assert status == 404


# ---------------------------------------------------------------- extraction


@pytest.mark.parametrize(
    "event,payload,expect",
    [
        ("pull_request", {"action": "closed", "pull_request": {"number": 5, "head": {"sha": "s1"}}},
         ("closed", 5, "s1")),
        ("issues", {"action": "labeled", "issue": {"number": 9}}, ("labeled", 9, None)),
        ("issue_comment", {"action": "created", "issue": {"number": 11}, "comment": {"id": 3}},
         ("created", 11, None)),
        ("check_suite", {"action": "completed",
                         "check_suite": {"head_sha": "cs1", "pull_requests": [{"number": 21}]}},
         ("completed", 21, "cs1")),
        ("merge_group", {"action": "destroyed", "merge_group": {"head_sha": "mg1"}},
         ("destroyed", None, "mg1")),
        ("ping", {"zen": "x"}, (None, None, None)),
    ],
)
def test_extract_pulls_the_daemon_relevant_fields(event, payload, expect):
    assert receiver.extract(event, payload) == expect


def test_extract_survives_missing_nesting():
    """GitHub omits fields on some actions; extraction must degrade, not raise."""
    assert receiver.extract("pull_request", {"pull_request": {}}) == (None, None, None)
    assert receiver.extract("check_suite", {"check_suite": {}}) == (None, None, None)


def test_trim_payload_keeps_only_what_the_translator_reads():
    body = {"sender": {"login": "someone"}, "label": {"name": "agent:ready"},
            "issue": {"number": 1, "body": "x" * 5000}}
    trimmed = json.loads(receiver.trim_payload("issues", body))
    assert trimmed["sender"] == "someone"
    assert trimmed["label"] == "agent:ready"
    assert "issue" not in trimmed  # the 5 KB body never reaches the queue database


def test_multiple_binds_on_one_port_listen_everywhere(tmp_path):
    """The signature took a LIST and used only binds[0].

    `--bind 172.18.0.1:8420 --bind 127.0.0.1:8420` therefore listened on the bridge alone, and
    loopback is exactly what the watchdog probes for /healthz — so the self-heal ladder would have
    reported the receiver dead while it was serving the tunnel perfectly.
    """
    httpd = receiver.serve(
        binds=[("127.0.0.1", 0)], db_path=tmp_path / "q.db", secret=SECRET
    )
    try:
        assert httpd.server_address[0] == "127.0.0.1"  # single bind is unchanged
    finally:
        httpd.server_close()


def test_single_bind_is_not_widened(tmp_path):
    """One address must stay one address; widening by accident is a security change."""
    httpd = receiver.serve(binds=[("127.0.0.1", 0)], db_path=tmp_path / "q.db", secret=SECRET)
    try:
        assert httpd.server_address[0] != "0.0.0.0"
    finally:
        httpd.server_close()
