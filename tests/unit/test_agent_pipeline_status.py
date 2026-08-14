"""Regression tests for the operator report in scripts/agent-pipeline/status.sh.

status.sh is the one command an operator runs when the pipeline looks wrong, so the failure that
matters is not a crash — it is the report DISAGREEING with tick.sh while looking authoritative.
Three of the four cases below are exactly that: a cap that omits the degraded clamp, a lane verdict
read past the TTL the pipeline itself honours, and a stall warning silently unreachable offline.
The fourth is a hang on a mistyped flag.

Everything here runs the shipped script for real against a scratch $BASE — it is self-contained and
every path it reads is overridable by env, which is what makes that possible.
"""

import json
import os
import re
import shutil
import subprocess
import textwrap
import time
from pathlib import Path

import pytest

STATUS_SH = Path(__file__).resolve().parents[2] / "scripts" / "agent-pipeline" / "status.sh"
SOURCE = STATUS_SH.read_text(encoding="utf-8")

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(shutil.which("bash") is None, reason="status.sh needs bash"),
]


def _base(tmp_path: Path, config: str = "", ollama_state: str = "") -> Path:
    """A scratch $BASE laid out the way the installer leaves one on the box."""
    for sub in ("state", "logs", "locks"):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    (tmp_path / "config.env").write_text(config, encoding="utf-8")
    if ollama_state:
        (tmp_path / "state" / "ollama.state").write_text(ollama_state, encoding="utf-8")
    return tmp_path


def _run(base: Path, *args: str, outcomes: str = "", timeout: int = 60, env_extra: dict | None = None):
    """Run status.sh against `base`, isolated from the real box."""
    env = {
        **os.environ,
        "BASE": str(base),
        "REPO": str(base),
        "OUTCOMES": outcomes or str(base / "logs" / "tick-outcomes.ndjson"),
        "CLAUDE_PROJECTS_DIR": str(base / "projects"),
        "NO_COLOR": "1",
        **(env_extra or {}),
    }
    return subprocess.run(
        ["bash", str(STATUS_SH), *args],
        capture_output=True, text=True, timeout=timeout, env=env,
    )


def _json(base: Path, *args: str, outcomes: str = "", env_extra: dict | None = None) -> dict:
    proc = _run(base, "--no-gh", "--json", *args, outcomes=outcomes, env_extra=env_extra)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_hours_without_a_value_is_refused_not_hung(tmp_path):
    """`--hours` as the last argument used to spin forever.

    `shift 2` with one argument left fails WITHOUT consuming anything, so the option loop re-read
    the same `$1` for ever — no output, no exit, terminal wedged on a plain typo.
    """
    proc = _run(_base(tmp_path), "--hours", timeout=20)
    assert proc.returncode == 2
    assert "--hours" in proc.stderr


def test_hours_rejects_a_non_numeric_window(tmp_path):
    proc = _run(_base(tmp_path), "--hours", "six", timeout=20)
    assert proc.returncode == 2


def test_cap_applies_the_degraded_clamp_like_tick_sh(tmp_path):
    """Neither lane above the threshold forces CAP=1 in tick.sh, so it must here too.

    Reporting the unclamped cap understates the constraint at exactly the moment an operator is
    reading the report to find out why nothing is moving.
    """
    base = _base(tmp_path, config="MAX_AGENTS=5\nCLAUDE_CAPACITY_PCT=10\nOLLAMA_CAPACITY_PCT=20\n")
    report = _json(base)
    assert report["pipeline"]["cap"] == 1
    assert report["pipeline"]["degraded"] is True

    healthy = _json(_base(tmp_path / "ok", config="MAX_AGENTS=5\nCLAUDE_CAPACITY_PCT=90\n"))
    assert healthy["pipeline"]["degraded"] is False


def test_config_secrets_never_reach_the_output(tmp_path):
    """config.env holds AGENT_GH_TOKEN in plaintext; the report is pasted into issues and chat."""
    base = _base(tmp_path, config="AGENT_GH_TOKEN=ghp_tokenthatmustnotappear\nMAX_AGENTS=2\n")
    proc = _run(base, "--no-gh")
    assert proc.returncode == 0
    assert "ghp_tokenthatmustnotappear" not in proc.stdout + proc.stderr


ALIAS_HELD = re.search(r"\nalias_held\(\) \{.*?\n\}\n", SOURCE, re.S)


def _alias_verdict(alias_ok: str, age_seconds: int) -> str:
    """Run the shipped `alias_held` for real against a stubbed ollama.state.

    Extracted rather than driven through a full run: the lane's earlier branch is a live curl to
    LiteLLM, which is absent in the unit lane, so a whole-script run could never reach this clause.
    """
    assert ALIAS_HELD, "alias_held() not found in status.sh"
    script = textwrap.dedent(f"""
        set -uo pipefail
        NOW=1000000
        ALIAS_TTL=3600
        state_get() {{ case "$2" in
            alias_ok)    printf '%s' '{alias_ok}' ;;
            alias_ok_ts) printf '%s' '{1000000 - age_seconds}' ;;
        esac; }}
        {ALIAS_HELD.group(0)}
        if alias_held; then echo HELD; else echo CLEAR; fi
    """)
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                          timeout=30).stdout.strip()


def test_expired_alias_verdict_is_not_reported_as_a_dead_lane():
    """capacity.sh only trusts ollama.state's `alias_ok` inside OLLAMA_ALIAS_TTL.

    Reading the flag without its timestamp holds the lane `unavailable` — and raises a NEEDS
    ATTENTION warning — off a verdict the pipeline has already stopped believing.
    """
    assert _alias_verdict("0", 60) == "HELD"        # fresh "not served" verdict still stands
    assert _alias_verdict("0", 7200) == "CLEAR"     # past the TTL capacity.sh re-probes on
    assert _alias_verdict("1", 60) == "CLEAR"       # alias is served
    assert _alias_verdict("", 60) == "CLEAR"        # never probed is not a failure

    # And the lane clause has to go through it rather than reading the flag raw again.
    assert "elif alias_held; then" in SOURCE


def test_stall_warning_still_fires_without_github(tmp_path):
    """"Ticks run but nothing dispatches" is gated on the ready count.

    Under --no-gh that count comes from the last tick's own record, so it has to be resolved BEFORE
    the rollup reads it — otherwise the warning is unreachable in the one mode where the operator
    has the least other signal.
    """
    base = _base(tmp_path)
    outcomes = base / "logs" / "o.ndjson"
    stamp = lambda ago: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - ago))  # noqa: E731
    outcomes.write_text(
        json.dumps({"tick_outcome": "dispatched", "mode": "start", "ready_count": 7,
                    "duration_ms": 1000, "ts": stamp(7200)}) + "\n"
        + json.dumps({"tick_outcome": "skipped", "reason": "all_slots_busy",
                      "ready_count": 7, "ts": stamp(120)}) + "\n",
        encoding="utf-8",
    )
    report = _json(base, outcomes=str(outcomes))
    assert any("stalled, not idle" in w for w in report["warnings"])

    # ...and stays quiet when the pipeline dispatched recently — the same file, one fresh row.
    outcomes.write_text(
        json.dumps({"tick_outcome": "dispatched", "mode": "start", "ready_count": 7,
                    "duration_ms": 1000, "ts": stamp(120)}) + "\n",
        encoding="utf-8",
    )
    assert not any("stalled, not idle" in w for w in _json(base, outcomes=str(outcomes))["warnings"])


# ---------------------------------------------------------------- v2 alignment (#1347)

HAS_SQLITE3 = shutil.which("sqlite3") is not None


def _v2_base(tmp_path: Path, *, config: str = "", heartbeat_age: int = 5,
             runs: tuple = (), items: tuple = ()) -> Path:
    """A scratch $BASE that looks like the box AFTER the v1 -> v2 cutover.

    The defining feature is the one that broke the report: `V1_RETIRED` is set, the daemon is
    alive, and `tick-outcomes.ndjson` is FROZEN because `tick.sh --failsafe` stands down on every
    fire while the heartbeat is fresh.
    """
    import sqlite3

    base = _base(tmp_path, config=config)
    (base / "V1_RETIRED").write_text("")
    (base / "state" / "lemd.heartbeat").write_text(str(int(time.time()) - heartbeat_age))
    (base / "v2" / "state").mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(base / "v2" / "state" / "queue.db")
    con.executescript(
        "CREATE TABLE items (id INTEGER PRIMARY KEY, kind TEXT, number INTEGER, state TEXT);"
        "CREATE TABLE runs (id INTEGER PRIMARY KEY, item_id INTEGER, mode TEXT, "
        "started_at INTEGER, ended_at INTEGER, rc INTEGER);"
    )
    con.executemany("INSERT INTO items (kind, number, state) VALUES (?,?,?)", items)
    con.executemany(
        "INSERT INTO runs (mode, started_at, ended_at, rc) VALUES (?,?,?,?)", runs)
    con.commit()
    con.close()
    return base


def _frozen_ledger(base: Path, *, age_s: int, ready: int) -> str:
    """A v1 outcomes file whose last row predates the cutover — exactly what the box has."""
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - age_s))
    path = base / "logs" / "tick-outcomes.ndjson"
    path.write_text(json.dumps({
        "tick_outcome": "dispatched", "mode": "start", "reason": "mode_start",
        "ready_count": ready, "ts": stamp,
    }) + "\n")
    return str(path)


@pytest.mark.skipif(not HAS_SQLITE3, reason="the v2 queue read needs the sqlite3 CLI")
def test_a_held_start_lane_is_not_reported_as_a_stalled_pipeline(tmp_path):
    """The report's whole failure mode on 2026-08-10.

    `LEMD_HOLD_STARTS=1` is a deliberate throttle from the cutover: no starts, merge/park/selfreview
    still draining. status.sh never read the knob, so it announced "the pipeline is stalled, not
    idle" and "cron may not be firing" on a daemon whose heartbeat was 26 seconds old. Both
    warnings were false, and the second one got louder every day because it was derived from a
    ledger that will never grow again.
    """
    base = _v2_base(
        tmp_path, config="LEMD_HOLD_STARTS=1\nLEMD_MAX_AGENTS=2\nMAX_AGENTS=5\n",
        runs=(("start", int(time.time()) - 36000, int(time.time()) - 35000, 0),),
    )
    outcomes = _frozen_ledger(base, age_s=36000, ready=48)

    proc = _run(base, "--no-gh", outcomes=outcomes)
    assert proc.returncode == 0, proc.stderr
    assert "stalled, not idle" not in proc.stdout
    assert "cron may not be firing" not in proc.stdout
    assert "HELD" in proc.stdout
    assert "LEMD_HOLD_STARTS" in proc.stdout

    report = _json(base, outcomes=outcomes)
    assert report["v2"]["hold_starts"] is True
    assert report["v2"]["active"] is True


@pytest.mark.skipif(not HAS_SQLITE3, reason="the v2 queue read needs the sqlite3 CLI")
def test_activity_comes_from_the_runs_table_not_the_frozen_v1_ledger(tmp_path):
    """`tick-outcomes.ndjson` stopped growing at the cutover; `runs` is what v2 writes.

    Reading the dead file made `last dispatch` an ever-increasing lie and left the activity block
    permanently empty — on a pipeline that had dispatched minutes earlier.
    """
    recent = int(time.time()) - 600
    base = _v2_base(tmp_path, config="LEMD_MAX_AGENTS=2\n",
                    runs=(("docfix", recent, recent + 120, 0),))
    outcomes = _frozen_ledger(base, age_s=36000, ready=3)

    report = _json(base, outcomes=outcomes)
    assert report["pipeline"]["last_dispatch_mode"] == "docfix"
    assert 0 <= report["pipeline"]["last_dispatch_age_s"] < 900, "the 10h-old v1 row must not win"
    assert report["v2"]["runs_in_window"] == 1

    text = _run(base, "--no-gh", outcomes=outcomes).stdout
    assert "OF RUNS (v2)" in text
    assert "docfix:1" in text


@pytest.mark.skipif(not HAS_SQLITE3, reason="the v2 queue read needs the sqlite3 CLI")
def test_the_v2_queue_state_line_actually_renders(tmp_path):
    """It never once printed: `"="` inside single quotes is an IDENTIFIER to SQLite.

    The query failed with `no such column: "="` on every run and `2>/dev/null` swallowed it, so the
    single most useful v2 line in the report silently rendered as nothing.
    """
    base = _v2_base(tmp_path, config="LEMD_MAX_AGENTS=2\n",
                    items=(("issue", 1, "ready"), ("issue", 2, "ready"), ("pr", 3, "parked")))
    text = _run(base, "--no-gh").stdout
    assert "v2 queue:" in text
    assert "ready=2" in text
    assert "parked=1" in text


@pytest.mark.skipif(not HAS_SQLITE3, reason="the v2 queue read needs the sqlite3 CLI")
def test_v2_capacity_is_reported_as_pools_not_v1_slot_locks(tmp_path):
    """v2 has no numbered flock slots. Printing `free: 1 2 3 4 5` under `v2 cap: 2` read as five."""
    now = int(time.time())
    base = _v2_base(tmp_path, config="LEMD_MAX_AGENTS=2\nMAX_AGENTS=5\n",
                    runs=(("start", now - 60, None, None), ("merge_enable", now - 30, None, None)))
    text = _run(base, "--no-gh").stdout
    assert "v2 pools:" in text
    assert "agent 1/2" in text
    assert "gh 1/" in text
    assert "v1 failsafe" in text, "the v1 block must be labelled, not presented as the live one"


def test_without_v1_retired_the_v1_stall_warning_still_fires(tmp_path):
    """The failsafe path must keep working — v1 is still the rollback target."""
    base = _base(tmp_path, config="MAX_AGENTS=5\n")
    outcomes = _frozen_ledger(base, age_s=36000, ready=12)
    proc = _run(base, "--no-gh", outcomes=outcomes)
    assert "stalled, not idle" in proc.stdout
    assert "cron may not be firing" in proc.stdout


# ---------------------------------------------------------------- stale units (#1412)

_SYSTEMCTL_STUB = """#!/usr/bin/env bash
# Stub systemctl. Reads "<unit> <active|inactive> <systemd timestamp>" lines from $STUB_UNITS,
# which is how a test says "this unit is up and started then" without a real init system.
case "$1" in
  is-active)
    while read -r u s _; do [ "$u" = "$3" ] && { [ "$s" = active ] && exit 0 || exit 3; }; done < "$STUB_UNITS"
    exit 4 ;;
  show)
    [ "$2" = "-p" ] && [ "$3" = ExecMainStartTimestamp ] || exit 0
    while read -r u _ rest; do
      [ "$u" = "$5" ] && { printf '%s\\n' "$rest"; exit 0; }
    done < "$STUB_UNITS"
    exit 0 ;;
esac
exit 0
"""


def _stub_systemctl(base: Path, units: dict) -> dict:
    """Put a fake `systemctl` on PATH. `units` maps unit -> (active, start epoch or None)."""
    bin_ = base / "stubbin"
    bin_.mkdir(exist_ok=True)
    (bin_ / "systemctl").write_text(_SYSTEMCTL_STUB, encoding="utf-8")
    (bin_ / "systemctl").chmod(0o755)
    lines = []
    for unit, (active, started) in units.items():
        stamp = "" if started is None else time.strftime("%a %Y-%m-%d %H:%M:%S UTC", time.gmtime(started))
        lines.append(f"{unit} {'active' if active else 'inactive'} {stamp}")
    stub_file = base / "stub-units"
    stub_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"PATH": f"{bin_}{os.pathsep}{os.environ['PATH']}", "STUB_UNITS": str(stub_file)}


def _lemd_source(base: Path, *, mtime: float) -> None:
    """The `lemd` package as installed, with a known newest-file mtime."""
    pkg = base / "v2" / "lemd"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "db.py").write_text("SCHEMA_VERSION = 3\n", encoding="utf-8")
    os.utime(pkg / "db.py", (mtime, mtime))


def test_a_receiver_older_than_the_code_it_imported_is_reported_stale(tmp_path):
    """#1412: the receiver ran 23-hour-old code through nine merged changes, invisibly.

    Both units load `lemd` and keep whatever they imported at their own start, so the deploy step
    naming only `lem-agentd` left the receiver on stale imports for a day. The only symptom was a
    `kv.schema_version` that would not advance — which is not a thing anyone checks. Comparing the
    unit's start time against the newest file in `v2/lemd/` is.
    """
    now = int(time.time())
    base = _base(tmp_path)
    _lemd_source(base, mtime=now - 3600)          # merged an hour ago
    env = _stub_systemctl(base, {
        "lem-agentd.service": (True, now - 300),          # restarted after the sync — current
        "lem-agent-webhook.service": (True, now - 82800),  # 23 hours old — stale
    })

    report = _json(base, env_extra=env)
    assert report["v2"]["stale_units"] == ["lem-agent-webhook.service"]
    assert any("lem-agent-webhook.service" in w and "no longer on disk" in w
               for w in report["warnings"])
    assert any("systemctl restart lem-agent-webhook.service" in w for w in report["warnings"]), \
        "a warning without the fix makes the operator go looking for it"

    text = _run(base, "--no-gh", env_extra=env).stdout
    assert "stale units:" in text
    assert "lem-agent-webhook.service" in text
    assert "lem-agentd.service" not in text.split("stale units:")[1].splitlines()[0]


def test_units_restarted_after_the_newest_change_are_not_flagged(tmp_path):
    """The check has to be quiet on a correctly deployed box, or it stops being read."""
    now = int(time.time())
    base = _base(tmp_path)
    _lemd_source(base, mtime=now - 3600)
    env = _stub_systemctl(base, {
        "lem-agentd.service": (True, now - 300),
        "lem-agent-webhook.service": (True, now - 290),
    })

    report = _json(base, env_extra=env)
    assert report["v2"]["stale_units"] == []
    assert not any("no longer on disk" in w for w in report["warnings"])
    assert "stale units:" not in _run(base, "--no-gh", env_extra=env).stdout


def test_an_unreadable_or_stopped_unit_is_skipped_not_assumed_current(tmp_path):
    """`date -d ''` is NOW, so an empty timestamp would render as the freshest possible unit.

    Unknown must never read as up to date, and a unit that is not running cannot be serving stale
    imports — that is a different fault with a different fix.
    """
    now = int(time.time())
    base = _base(tmp_path)
    _lemd_source(base, mtime=now - 3600)
    env = _stub_systemctl(base, {
        "lem-agentd.service": (True, None),                # never started / unreadable
        "lem-agent-webhook.service": (False, now - 82800),  # stopped
    })

    report = _json(base, env_extra=env)
    assert report["v2"]["stale_units"] == []
    assert not any("no longer on disk" in w for w in report["warnings"])


def test_the_check_covers_every_unit_that_imports_the_package(tmp_path):
    """A future unit loading `lemd` inherits the check by being named in LEMD_UNITS."""
    now = int(time.time())
    base = _base(tmp_path)
    _lemd_source(base, mtime=now - 60)
    env = _stub_systemctl(base, {"lem-agent-newthing.service": (True, now - 7200)})
    env["LEMD_UNITS"] = "lem-agent-newthing.service"

    report = _json(base, env_extra=env)
    assert report["v2"]["stale_units"] == ["lem-agent-newthing.service"]
