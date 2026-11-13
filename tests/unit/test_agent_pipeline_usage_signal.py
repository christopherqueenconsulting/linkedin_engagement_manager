"""Phase 1 of the model router: the provider quota poller, its accessor, per-run usage and pauses.

The `auth-files` fixture is SYNTHESIZED from the shape the issue (#2559) describes — Claude weekly
utilization 0.27 with status `allowed` and a reset epoch, Codex plan `team` at 0% — not captured from
the live proxy. Swap in a redacted real response when one is available; the accessor tests do not
depend on its exact keys.
"""

from __future__ import annotations

import json
import sys
import urllib.error
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_PIPELINE = _ROOT / "scripts" / "agent-pipeline"
sys.path.insert(0, str(_PIPELINE / "v2"))

from lemd import db, usage_signal  # noqa: E402
from lemd.config import load  # noqa: E402

NOW = 1_800_000_000.0
KEY = "mgmt-key-SECRET-123"
BASE = "https://proxy.internal.example:8317/v1"

AUTH_FILES = {
    "files": [
        {"name": "someone@example.com.json", "provider": "claude", "email": "someone@example.com",
         "quota": {"weekly_utilization": 0.27, "status": "allowed", "resets_at": NOW + 86400}},
        {"name": "codex.json", "provider": "codex", "plan": "team",
         "quota": {"five_hour_used_percent": 0, "weekly_used_percent": 12.5, "reset_at": NOW + 3600}},
    ]
}


def _fetcher(payload):
    return lambda: payload


def test_poll_writes_cache_with_both_providers(tmp_path):
    cache = tmp_path / "usage_signal.json"
    assert usage_signal.poll_once(cache, now=NOW, fetcher=_fetcher(AUTH_FILES)) is True
    doc = json.loads(cache.read_text())
    assert doc["fetched_at"] == NOW
    assert doc["claude"] == {"weekly_pct": 27.0, "status": "allowed", "reset_at": NOW + 86400}
    assert doc["codex"] == {"five_hour_pct": 0.0, "weekly_pct": 12.5, "reset_at": NOW + 3600}


def test_cache_carries_no_account_identity(tmp_path):
    cache = tmp_path / "usage_signal.json"
    usage_signal.poll_once(cache, now=NOW, fetcher=_fetcher(AUTH_FILES))
    text = cache.read_text()
    for leaked in ("someone@example.com", "codex.json", "team", "proxy.internal"):
        assert leaked not in text


def test_accessor_reads_fresh_cache(tmp_path):
    cache = tmp_path / "usage_signal.json"
    usage_signal.poll_once(cache, now=NOW, fetcher=_fetcher(AUTH_FILES))
    sig = usage_signal.read(cache, now=NOW + 30)
    assert sig.readable and sig.reason == "ok" and sig.age_s == 30
    assert (sig.claude_weekly_pct, sig.claude_status) == (27.0, "allowed")
    assert (sig.codex_5h_pct, sig.codex_weekly_pct) == (0.0, 12.5)


def test_accessor_missing_cache_is_unreadable(tmp_path):
    sig = usage_signal.read(tmp_path / "absent.json", now=NOW)
    assert sig.unreadable and sig.reason == "missing"
    assert sig.claude_weekly_pct is None and sig.codex_5h_pct is None


def test_accessor_stale_cache_is_unreadable_with_no_numbers(tmp_path):
    cache = tmp_path / "usage_signal.json"
    usage_signal.poll_once(cache, now=NOW, fetcher=_fetcher(AUTH_FILES))
    sig = usage_signal.read(cache, now=NOW + usage_signal.DEFAULT_MAX_AGE + 1)
    assert sig.unreadable and sig.reason == "stale"
    assert sig.age_s > usage_signal.DEFAULT_MAX_AGE
    assert sig.claude_weekly_pct is None


def test_accessor_future_dated_cache_is_not_trusted(tmp_path):
    cache = tmp_path / "usage_signal.json"
    usage_signal.poll_once(cache, now=NOW + 1000, fetcher=_fetcher(AUTH_FILES))
    assert usage_signal.read(cache, now=NOW).reason == "stale"


def test_accessor_malformed_cache_variants(tmp_path):
    cache = tmp_path / "usage_signal.json"
    for body in ("{not json", "null", "[]", json.dumps({"fetched_at": "x", "claude": {}, "codex": {}}),
                 json.dumps({"fetched_at": NOW}), json.dumps({"fetched_at": NOW, "claude": 1, "codex": {}})):
        cache.write_text(body)
        sig = usage_signal.read(cache, now=NOW)
        assert sig.unreadable and sig.reason == "malformed", body


def test_out_of_range_numbers_are_dropped_not_clamped(tmp_path):
    cache = tmp_path / "usage_signal.json"
    cache.write_text(json.dumps({"fetched_at": NOW, "claude": {"weekly_pct": 250},
                                 "codex": {"five_hour_pct": -3, "weekly_pct": 40}}))
    sig = usage_signal.read(cache, now=NOW)
    assert sig.claude_weekly_pct is None and sig.codex_5h_pct is None and sig.codex_weekly_pct == 40


def test_failed_or_empty_poll_keeps_old_cache(tmp_path):
    cache = tmp_path / "usage_signal.json"
    usage_signal.poll_once(cache, now=NOW, fetcher=_fetcher(AUTH_FILES))
    before = cache.read_text()
    assert usage_signal.poll_once(cache, now=NOW + 90, fetcher=_fetcher(None)) is False
    assert usage_signal.poll_once(cache, now=NOW + 90, fetcher=_fetcher({"files": []})) is False
    assert usage_signal.poll_once(cache, now=NOW + 90, fetcher=_fetcher("garbage")) is False
    assert cache.read_text() == before


def test_iso_reset_time_is_parsed():
    payload = [{"provider": "claude", "weekly_utilization": 0.5, "resets_at": "2027-01-01T00:00:00Z"}]
    doc = usage_signal.normalize(payload, now=NOW)
    assert doc["claude"]["reset_at"] == 1798761600.0


class _Resp:
    def __init__(self, body):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def test_fetch_sends_key_only_as_header_and_logs_neither_key_nor_host(monkeypatch, caplog, tmp_path):
    monkeypatch.setenv(usage_signal.KEY_VAR, KEY)
    monkeypatch.setenv(usage_signal.BASE_VAR, BASE)
    seen = {}

    def opener(req, timeout):
        seen["url"], seen["auth"] = req.full_url, req.get_header("Authorization")
        return _Resp(json.dumps(AUTH_FILES).encode())

    assert usage_signal.fetch(opener=opener) == AUTH_FILES
    assert seen["url"] == "https://proxy.internal.example:8317/v0/management/auth-files"
    assert seen["auth"] == f"Bearer {KEY}"

    def failing(req, timeout):
        raise urllib.error.URLError(f"cannot reach {req.full_url} with {KEY}")

    def http_error(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 401, "denied", None, None)

    with caplog.at_level("DEBUG", logger="lemd.usage_signal"):
        assert usage_signal.fetch(opener=failing) is None
        assert usage_signal.fetch(opener=http_error) is None
        cache = tmp_path / "usage_signal.json"
        assert usage_signal.poll_once(cache, now=NOW) is False
    assert "HTTP 401" in caplog.text
    for secret in (KEY, "proxy.internal"):
        assert secret not in caplog.text


def test_fetch_without_configuration_makes_no_request(monkeypatch):
    monkeypatch.delenv(usage_signal.KEY_VAR, raising=False)
    monkeypatch.delenv(usage_signal.BASE_VAR, raising=False)

    def boom(req, timeout):
        raise AssertionError("must not request without configuration")

    assert usage_signal.fetch(opener=boom) is None


def test_no_module_names_the_draining_queue_endpoint():
    """Reading it consumes records, so nothing in the pipeline tree may reference it."""
    needle = "usage" + "-queue"
    offenders = [
        str(p.relative_to(_ROOT))
        for p in _PIPELINE.rglob("*")
        if p.is_file() and p.suffix in {".py", ".sh", ".md", ".json", ".service"}
        and needle in p.read_text(errors="replace")
    ]
    assert offenders == []


def test_pauses_are_per_provider(tmp_path):
    conn = db.connect(tmp_path / "q.db")
    db.pause_provider(conn, "codex", NOW + 600)
    assert db.provider_paused_until(conn, "codex", now=NOW) == NOW + 600
    assert db.provider_paused_until(conn, "claude", now=NOW) == 0.0
    assert db.provider_paused_until(conn, "ollama", now=NOW) == 0.0
    assert db.provider_paused_until(conn, "codex", now=NOW + 601) == 0.0   # expired
    db.pause_provider(conn, "claude", NOW + 60)
    db.resume_provider(conn, "claude")
    assert db.provider_paused_until(conn, "codex", now=NOW) == NOW + 600
    assert db.provider_paused_until(conn, "claude", now=NOW) == 0.0


def test_unknown_provider_is_refused(tmp_path):
    conn = db.connect(tmp_path / "q.db")
    for fn in (lambda: db.pause_provider(conn, "gemini", NOW), lambda: db.provider_paused_until(conn, "x")):
        try:
            fn()
        except ValueError:
            continue
        raise AssertionError("an unknown provider must be refused")


def test_run_usage_is_recorded_and_zero_stays_unmeasured(tmp_path):
    conn = db.connect(tmp_path / "q.db")
    item = db.upsert_item(conn, kind="issue", number=7, state=db.STATE_READY)
    run_id = db.start_run(conn, item_id=item, mode="start", pid=1, log_path="/x")
    db.set_run_lane(conn, run_id, lane="claude", model="claude-sonnet-5-5", route_reason="r")
    db.set_run_usage(conn, run_id, tokens_in=1234, tokens_out=56)
    row = conn.execute("SELECT model, tokens_in, tokens_out FROM runs WHERE id=?", (run_id,)).fetchone()
    assert (row["model"], row["tokens_in"], row["tokens_out"]) == ("claude-sonnet-5-5", 1234, 56)
    other = db.start_run(conn, item_id=item, mode="fix", pid=2, log_path="/y")
    db.set_run_usage(conn, other, tokens_in=0, tokens_out=None)
    row = conn.execute("SELECT tokens_in, tokens_out FROM runs WHERE id=?", (other,)).fetchone()
    assert row["tokens_in"] is None and row["tokens_out"] is None


def test_older_queue_db_gains_the_token_columns(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY, item_id INTEGER, mode TEXT NOT NULL, "
                "lane TEXT, model TEXT, route_reason TEXT, pid INTEGER, pid_start TEXT, "
                "started_at INTEGER NOT NULL, ended_at INTEGER, rc INTEGER, log_path TEXT)")
    old.commit()
    old.close()
    conn = db.connect(path)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(runs)").fetchall()}
    assert {"tokens_in", "tokens_out"} <= cols


def test_router_thresholds_default_to_the_owner_rules(tmp_path):
    (tmp_path / "config.env").write_text(f"LEMD_DB={tmp_path}/q.db\n")
    cfg = load(tmp_path)
    assert (cfg.claude_weekly_ollama_pct, cfg.claude_opus_max_pct, cfg.codex_review_sonnet_pct) == (50, 35, 90)
    assert cfg.quota_poll_interval == 90
    (tmp_path / "config.env").write_text("LEMD_QUOTA_POLL_INTERVAL=5\nROUTER_CLAUDE_OPUS_MAX_PCT=20\n")
    cfg = load(tmp_path)
    assert cfg.quota_poll_interval == 60 and cfg.claude_opus_max_pct == 20


def test_run_lane_extracts_real_usage_from_the_cli_result(tmp_path):
    """The python in `_record_permission_run` writes the `.usage` side file the lane reads."""
    import re
    import subprocess

    src = (_PIPELINE / "lib" / "run_lane.sh").read_text()
    script = re.search(r"python3 - .*?<<'PY' 2>/dev/null \|\| true\n(.*?)\nPY\n", src, re.S).group(1)
    result = {"type": "result", "result": "done", "permission_denials": [],
              "usage": {"input_tokens": 10, "cache_creation_input_tokens": 100,
                        "cache_read_input_tokens": 1000, "output_tokens": 55},
              "modelUsage": {"claude-haiku-5-5": {"inputTokens": 5, "outputTokens": 1},
                             "claude-sonnet-5-5": {"inputTokens": 10, "outputTokens": 55,
                                                   "cacheReadInputTokens": 1000}}}
    out = tmp_path / "run.out"
    out.write_text("stderr note\n" + json.dumps(result) + "\n")
    argv = [sys.executable, "-", str(out), str(tmp_path / "den.jsonl"), "0", "claude", "start",
            "1", "", "", "profile.json"]
    subprocess.run(argv, input=script, text=True, check=True)
    assert (tmp_path / "run.out.usage").read_text() == (
        "tokens_in=1110\ntokens_out=55\nmodel_used=claude-sonnet-5-5\n")
    assert out.read_text() == "stderr note\ndone\n"
