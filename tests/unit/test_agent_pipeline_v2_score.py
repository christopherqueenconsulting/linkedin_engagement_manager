"""Tests for the no-model triage scorer (`lemd.score`) and its daemon hook.

The scorer decides what reaches the owner's trigger log from label names alone. The failure modes
that matter are quiet ones: a title that smuggles a newline or `|` into the log and forges a second
line, a log that is truncated or written through a symlink, a re-run that repeats every line, and
a scorer fault that stops the daemon tick. Each has a test here.
"""

from __future__ import annotations

import ast
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

_V2 = Path(__file__).resolve().parents[2] / "scripts" / "agent-pipeline" / "v2"
sys.path.insert(0, str(_V2))

from lemd import (  # noqa: E402
    config as lemd_config,  # noqa: E402
    daemon,
    score,
)
from lemd.config import load  # noqa: E402

LINE_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ \| (NEW|REMIND) \| lem:\d+:[0-3] \| .+$")
T0 = 1_800_000_000.0


def _cfg(tmp_path, scorer="1", extra=""):
    """A config rooted in a temp dir with the scorer's log inside it."""
    (tmp_path / "config.env").write_text(
        f"LEMD_DB={tmp_path}/queue.db\nLEMD_SHADOW=1\nLEMD_SCORER={scorer}\n"
        f"LEMD_TRIGGER_LOG={tmp_path}/ea/state/lem-triggers.log\n"
        "SLUG=christopherqueenconsulting/linkedin_engagement_manager\n" + extra
    )
    return load(tmp_path)


def _issue(number, labels, title="A title"):
    return {"number": number, "title": title, "labels": list(labels)}


class _Clock:
    """An injectable clock."""

    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now


def _scorer(cfg, issues, clock):
    """A Scorer whose fetch returns whatever `issues` currently holds."""
    return score.Scorer(cfg, clock=clock, fetch=lambda slug: list(issues))


def _log_lines(cfg):
    p = cfg.trigger_log
    return p.read_text().splitlines() if p.exists() else []


TABLE = [
    (2467, ["bug", "priority:high", "agent:blocked", "needs-human", "risk:live-linkedin"], 0, "needs-chris"),
    (2431, ["agent:blocked", "needs-human"], 0, "needs-chris"),
    (2495, ["outbound", "priority:medium", "agent:blocked", "needs-human"], 0, "needs-chris"),
    (2368, ["enhancement", "priority:high", "agent:ready", "agent:blocked", "needs-human", "risk:security",
            "ai:claude-subscription"], 0, "needs-chris"),
    (2184, ["infrastructure", "priority:medium", "needs-human", "blocked-on-chris"], 0, "needs-chris"),
    (2422, ["bug", "needs-human", "ai:claude-subscription"], 2, "work"),
    (2422, ["bug", "needs-human", "ai:claude-subscription", "blocked-on-chris"], 0, "needs-chris"),
    (2501, ["bug", "feedback-loop", "priority:medium", "needs-human"], 3, "work"),
    (2500, ["bug", "feedback-loop", "priority:medium", "needs-human"], 3, "work"),
    (2505, ["bug"], 2, "work"),
    (2258, ["infrastructure", "priority:low", "agent:ready", "ai:claude-subscription", "template:agent-task",
            "ai"], 0, "none"),
    (1530, ["priority:low", "needs-human", "ai:claude-subscription", "content-quality"], 0, "none"),
    (2506, ["enhancement", "priority:low", "agent-ready"], 0, "none"),
    (0, ["bug", "priority:low"], 1, "work"),
    (0, [], 0, "none"),
]


@pytest.mark.parametrize(("number", "labels", "value", "route"), TABLE)
def test_score_table(number, labels, value, route):
    """The documented label sets score exactly as the rule says."""
    got = score.score_issue(labels)
    assert (got.value, got.route) == (value, route)
    assert got.reason


@pytest.mark.parametrize(("labels", "value", "route"), [
    (["blocked-on-chris", "risk:security", "bug"], 0, "needs-chris"),   # a beats b
    (["risk:security", "bug", "infrastructure"], 3, "work"),            # b beats c, d
    (["bug", "infrastructure"], 2, "work"),                             # c beats d
    (["risk:live-linkedin", "priority:low"], 0, "needs-chris"),         # modifier never touches a
    (["agent:blocked"], 0, "none"),                                     # half of the pair is nothing
    (["needs-human"], 0, "none"),
    (["revenue", "priority:low"], 2, "work"),
    (["infrastructure", "priority:low"], 0, "none"),                    # lowered to 0 -> no line
    (["priority:medium", "priority:low"], 0, "none"),
])
def test_first_match_precedence_and_modifier(labels, value, route):
    got = score.score_issue(labels)
    assert (got.value, got.route) == (value, route)


def test_sanitise_title():
    """Pipes, newlines and control characters cannot forge a field or a line; length is capped."""
    assert score.sanitise_title("a | b\nc\r\n\td\x00e\x1b[31m") == "a / b c de[31m"
    assert score.sanitise_title("  spaced   out  ") == "spaced out"
    assert score.sanitise_title("x\u202ey") == "xy"  # bidi override is a control (Cf) character
    assert score.sanitise_title(None) == ""
    assert len(score.sanitise_title("z" * 300)) == 160


def test_first_run_lines_match_the_format_and_prefix(tmp_path):
    cfg = _cfg(tmp_path)
    issues = [_issue(1, ["bug"], "plain"), _issue(2, ["blocked-on-chris"], "needs me | now\nplease"),
              _issue(3, ["bug"], "t" * 300), _issue(4, [], "nothing")]
    out = _scorer(cfg, issues, _Clock()).run()
    assert out == _log_lines(cfg)
    assert len(out) == 3
    assert all(LINE_RE.match(ln) for ln in out)
    assert out[0] == "2027-01-15T08:00:00Z | NEW | lem:1:2 | plain"
    assert out[1].endswith("| lem:2:0 | NEEDS-CHRIS: needs me / now please")
    assert len(out[2].split(" | ", 3)[3]) == 160


def test_new_on_first_sight_and_on_change_but_quiet_when_unchanged(tmp_path):
    cfg = _cfg(tmp_path)
    clock = _Clock()
    issues = [_issue(7, ["bug"])]
    sc = _scorer(cfg, issues, clock)
    assert len(sc.run()) == 1
    clock.now += 700
    assert sc.run() == []                                     # unchanged: no line
    issues[0] = _issue(7, ["bug", "priority:high"])          # score 2 -> 3
    clock.now += 700
    out = sc.run()
    assert len(out) == 1 and " | NEW | lem:7:3 | " in out[0]
    issues[0] = _issue(7, ["bug", "priority:high", "blocked-on-chris"])   # route change
    clock.now += 700
    out = sc.run()
    assert len(out) == 1 and " | NEW | lem:7:0 | NEEDS-CHRIS: " in out[0]
    assert len(_log_lines(cfg)) == 3


def test_remind_after_the_window_and_not_before(tmp_path):
    cfg = _cfg(tmp_path, extra="LEMD_SCORER_INTERVAL=0\n")   # interval off: only the remind window gates
    clock = _Clock()
    sc = _scorer(cfg, [_issue(9, ["outbound"])], clock)
    sc.run()
    clock.now += 86399
    assert sc.run() == []
    clock.now += 1
    out = sc.run()
    assert len(out) == 1 and " | REMIND | lem:9:3 | " in out[0]
    clock.now += 700
    assert sc.run() == []                                     # the window restarts at the reminder


def test_state_survives_a_restart(tmp_path):
    cfg = _cfg(tmp_path)
    issues = [_issue(5, ["bug"])]
    _scorer(cfg, issues, _Clock()).run()
    assert _scorer(cfg, issues, _Clock(T0 + 700)).run() == []
    state = json.loads((tmp_path / "state" / "score-state.json").read_text())
    assert state["5"]["score"] == 2 and state["5"]["route"] == "work"


def test_closed_issue_is_dropped_without_a_line(tmp_path):
    cfg = _cfg(tmp_path)
    clock = _Clock()
    issues = [_issue(11, ["bug"]), _issue(12, ["bug"])]
    sc = _scorer(cfg, issues, clock)
    sc.run()
    issues.pop(0)
    clock.now += 700
    assert sc.run() == []
    state = json.loads((tmp_path / "state" / "score-state.json").read_text())
    assert list(state) == ["12"]
    assert len(_log_lines(cfg)) == 2


def test_interval_throttles_fetches(tmp_path):
    cfg = _cfg(tmp_path)
    clock = _Clock()
    calls = []
    sc = score.Scorer(cfg, clock=clock, fetch=lambda slug: calls.append(slug) or [])
    sc.run()
    clock.now += 599
    sc.run()
    assert len(calls) == 1
    clock.now += 1
    sc.run()
    assert len(calls) == 2


def test_log_is_0600_dir_0700_and_append_only(tmp_path):
    cfg = _cfg(tmp_path)
    clock = _Clock()
    issues = [_issue(21, ["bug"])]
    sc = _scorer(cfg, issues, clock)
    sc.run()
    log = cfg.trigger_log
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
    assert stat.S_IMODE(log.parent.stat().st_mode) == 0o700
    first = log.read_bytes()
    inode = log.stat().st_ino
    issues.append(_issue(22, ["revenue"]))
    clock.now += 700
    sc.run()
    second = log.read_bytes()
    assert second.startswith(first) and len(second) > len(first)
    assert log.stat().st_ino == inode


def test_symlinked_log_is_refused(tmp_path):
    cfg = _cfg(tmp_path)
    target = tmp_path / "elsewhere.txt"
    target.write_text("keep\n")
    cfg.trigger_log.parent.mkdir(parents=True)
    cfg.trigger_log.symlink_to(target)
    with pytest.raises(OSError, match="symlink"):
        _scorer(cfg, [_issue(31, ["bug"])], _Clock()).run()
    assert target.read_text() == "keep\n"
    assert not (tmp_path / "state" / "score-state.json").exists()   # no state for an unwritten line


@pytest.mark.parametrize("value", ["1", "true", "yes"])
def test_flag_values_that_turn_it_on(tmp_path, value):
    assert _cfg(tmp_path, scorer=value).scorer is True


@pytest.mark.parametrize("value", ["0", "false", "no", "on", "y", "TRUE", "2"])
def test_other_flag_values_stay_off(tmp_path, value):
    assert _cfg(tmp_path, scorer=value).scorer is False


def test_defaults(tmp_path):
    (tmp_path / "config.env").write_text("LEMD_SHADOW=1\n")
    cfg = load(tmp_path)
    assert cfg.scorer is False
    assert cfg.trigger_log == Path("/home/lem/ea/state/lem-triggers.log")
    assert (cfg.scorer_interval, cfg.scorer_remind_seconds) == (600, 86400)


def _daemon(tmp_path, scorer, monkeypatch, issues):
    cfg = _cfg(tmp_path, scorer=scorer)
    monkeypatch.setattr(score.github, "gh_json", lambda args, **kw: [
        {"number": i["number"], "title": i["title"], "labels": [{"name": n} for n in i["labels"]]}
        for i in issues])
    return cfg, daemon.Daemon(cfg)


def test_flag_off_writes_nothing(tmp_path, monkeypatch):
    cfg, dm = _daemon(tmp_path, "0", monkeypatch, [_issue(41, ["bug"])])
    cfg.paused_file.write_text("")   # a PAUSED tick stops after the pre-gate steps: no probes, no gh
    dm.tick()
    assert not cfg.trigger_log.exists()
    assert not (tmp_path / "state" / "score-state.json").exists()


def test_scorer_runs_while_paused(tmp_path, monkeypatch):
    cfg, dm = _daemon(tmp_path, "1", monkeypatch, [_issue(42, ["bug", "priority:high"])])
    cfg.paused_file.write_text("")
    assert cfg.is_paused()
    dm.tick()
    lines = _log_lines(cfg)
    assert len(lines) == 1 and " | NEW | lem:42:3 | " in lines[0]


def test_scorer_exception_never_breaks_the_tick(tmp_path, monkeypatch, caplog):
    cfg, dm = _daemon(tmp_path, "1", monkeypatch, [])
    cfg.paused_file.write_text("")

    def boom(self):
        raise RuntimeError("gh exploded")

    monkeypatch.setattr(score.Scorer, "run", boom)
    with caplog.at_level("WARNING", logger="lemd"):
        dm.run_scorer()
        dm.tick()
    assert "triage scorer skipped" in caplog.text


def test_score_module_is_provably_no_model():
    """score.py names no model client and runs no subprocess of its own."""
    tree = ast.parse((_V2 / "lemd" / "score.py").read_text())
    banned = ("claude", "anthropic", "litellm", "openai", "ollama")
    imported = set()
    words = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0] or "." + ",".join(a.name for a in node.names))
        if isinstance(node, ast.Name):
            words.append(node.id)
        elif isinstance(node, ast.Attribute):
            words.append(node.attr)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            words.append(node.value)
    assert "subprocess" not in imported
    assert "system" not in words and "popen" not in words and "Popen" not in words
    blob = " ".join(words).lower() + " " + " ".join(imported).lower()
    for name in banned:
        assert name not in blob


def _write_issues(path):
    path.write_text(json.dumps([_issue(100 + i, row[1], f"title {row[0]}") for i, row in enumerate(TABLE)]))


def test_dry_run_prints_lines_and_writes_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(lemd_config, "BASE", tmp_path / "base")
    log = tmp_path / "ea" / "state" / "lem-triggers.log"
    monkeypatch.setenv("LEMD_TRIGGER_LOG", str(log))
    src = tmp_path / "issues.json"
    _write_issues(src)
    before = sorted(p.name for p in tmp_path.rglob("*"))
    assert score.main(["--dry-run", "--issues-json", str(src)]) == 0
    out = capsys.readouterr().out.splitlines()
    assert len(out) == 11 and all(LINE_RE.match(ln) for ln in out)
    assert sorted(p.name for p in tmp_path.rglob("*")) == before   # no log, no state, no base dir


def test_dry_run_via_python_dash_m(tmp_path):
    src = tmp_path / "issues.json"
    _write_issues(src)
    env = {**os.environ, "BASE": str(tmp_path / "base"), "LEMD_TRIGGER_LOG": str(tmp_path / "t.log")}
    proc = subprocess.run([sys.executable, "-m", "lemd.score", "--dry-run", "--issues-json", str(src)],
                          cwd=_V2, env=env, capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr
    assert len(proc.stdout.splitlines()) == 11
    assert not (tmp_path / "t.log").exists() and not (tmp_path / "base").exists()
