"""The v2 daemon must REACH `sweep_stale_worktrees` (#2041).

The sweep had one call site, `tick.sh`, and `tick.sh` exits at the top of a `V1_RETIRED` box. So
from the 2026-08-10 cutover nothing swept `work/`: 76 worktrees and 8.7 GB by 2026-09-11. The
failure had no symptom — no error, no log line, just disk that never came back — which is why the
tests here pin the WIRING and not only the function: the daemon spawns the action, the action calls
the function, and the function removes a merged tree, each proved separately and once end to end.

The daemon half mocks the supervisor the way the existing daemon tests mock GitHub. The shell half
builds a throwaway repo under `tmp_path` in the `test_worktree_cleanup.py` style and runs the SHIPPED
`sweep.sh` + `common.sh` + `lib/guards.sh`, with `gh` replaced by a shell function — the only seam
`common.sh` leaves, because it pins `PATH` before the guards load.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_PIPE = _ROOT / "scripts" / "agent-pipeline"
_V2 = _PIPE / "v2"
sys.path.insert(0, str(_V2))

from lemd import daemon, db, dispatch  # noqa: E402
from lemd.config import load  # noqa: E402

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- daemon half

def _acting_cfg(tmp_path: Path, **extra: str):
    """A LIVE (non-shadow) config rooted in a temp dir; the sweep is inert in shadow mode."""
    lines = [f"LEMD_DB={tmp_path}/queue.db", "LEMD_SHADOW=0",
             "SLUG=christopherqueenconsulting/linkedin_engagement_manager"]
    lines += [f"{k}={v}" for k, v in extra.items()]
    (tmp_path / "config.env").write_text("\n".join(lines) + "\n")
    return load(tmp_path)


class _Spy:
    """Records `dispatch_sweep` calls and answers with whatever the test scripted."""

    def __init__(self, answer):
        self.calls: list[dict] = []
        self.answer = answer

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self.answer


def _fake_child() -> dispatch.Child:
    return dispatch.Child(proc=None, pool=dispatch.MAINT_POOL, mode=dispatch.SWEEP_MODE,
                          kind=dispatch.SWEEP_MODE, number=0, item_id=None, run_id=None,
                          deadline=0.0, started=0.0)


def test_tick_spawns_the_sweep_even_while_paused(tmp_path, monkeypatch):
    """The call site is the LOOP, and it sits before the PAUSED gate.

    Before the gate on purpose: a pause stops the pipeline STARTING work, and reclaiming disk from
    trees whose PR merged weeks ago is not work. It is also the moment an operator is most likely
    to be looking at the box. PAUSED in this test doubles as the thing that keeps `tick()` off the
    network — nothing past the gate runs.
    """
    dm = daemon.Daemon(_acting_cfg(tmp_path))
    (tmp_path / "PAUSED").write_text("")
    spy = _Spy(_fake_child())
    monkeypatch.setattr(dm.sup, "dispatch_sweep", spy)
    dm.tick()
    assert len(spy.calls) == 1, "one tick on a never-swept box must spawn exactly one sweep"
    assert spy.calls[0] == {"interval_s": 3600}, "the child is handed the daemon's own interval"


def test_the_stamp_file_is_the_clock(tmp_path, monkeypatch):
    """The daemon reads the SAME stamp `sweep_stale_worktrees` touches, so the two halves agree.

    A daemon with its own clock would spawn a process an hour early that the function's guard only
    sends home — or, with the two clocks a second apart, skip a whole hour.
    """
    dm = daemon.Daemon(_acting_cfg(tmp_path))
    stamp = dm.cfg.worktree_sweep_stamp
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text("")   # swept just now
    spy = _Spy(_fake_child())
    monkeypatch.setattr(dm.sup, "dispatch_sweep", spy)
    assert dm.sweep_worktrees() is False
    assert spy.calls == []

    old = time.time() - 3601
    os.utime(stamp, (old, old))
    assert dm.sweep_worktrees() is True
    assert len(spy.calls) == 1


def test_the_interval_is_a_knob_read_from_config_env(tmp_path, monkeypatch):
    dm = daemon.Daemon(_acting_cfg(tmp_path, LEMD_WORKTREE_SWEEP_INTERVAL="600"))
    stamp = dm.cfg.worktree_sweep_stamp
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text("")
    old = time.time() - 700
    os.utime(stamp, (old, old))
    spy = _Spy(_fake_child())
    monkeypatch.setattr(dm.sup, "dispatch_sweep", spy)
    assert dm.sweep_worktrees() is True
    assert spy.calls[0] == {"interval_s": 600}


def test_shadow_mode_spawns_no_sweep(tmp_path, monkeypatch):
    """Shadow's contract is "spawn nothing"; v1 still owns dispatch there and its tick sweeps."""
    (tmp_path / "config.env").write_text(f"LEMD_DB={tmp_path}/queue.db\nLEMD_SHADOW=1\n")
    dm = daemon.Daemon(load(tmp_path))
    spy = _Spy(_fake_child())
    monkeypatch.setattr(dm.sup, "dispatch_sweep", spy)
    (tmp_path / "PAUSED").write_text("")
    dm.tick()
    assert spy.calls == []


def test_a_failed_spawn_backs_off_instead_of_retrying_every_pass(tmp_path, monkeypatch):
    """A failed spawn must not become a once-a-second retry.

    A spawn that fails leaves the stamp untouched, so without a backoff the loop would try again
    on every pass for as long as the box stayed broken.
    """
    dm = daemon.Daemon(_acting_cfg(tmp_path))
    spy = _Spy(None)
    monkeypatch.setattr(dm.sup, "dispatch_sweep", spy)
    assert dm.sweep_worktrees() is False
    assert dm.sweep_worktrees() is False
    assert len(spy.calls) == 1
    # ...and the backoff is bounded: once it elapses the sweep is offered again.
    dm._last_sweep_spawn -= dm.SWEEP_RETRY_SECONDS + 1
    dm.sweep_worktrees()
    assert len(spy.calls) == 2


def test_collect_tolerates_a_finished_sweep_child(tmp_path):
    """The sweep is not an item. Reaping it must neither raise nor touch the queue."""
    dm = daemon.Daemon(_acting_cfg(tmp_path))

    class _Done:
        pid = 4242

        @staticmethod
        def poll():
            return 0

    child = _fake_child()
    child.proc = _Done()
    dm.sup.children.append(child)
    assert dm.collect() == 1
    assert dm.sup.children == []
    assert dm.conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"] == 0


# --------------------------------------------------------------------------- supervisor half

class _Cfg:
    def __init__(self, tmp_path: Path):
        self.base = tmp_path
        self.repo = tmp_path
        self.slug = "o/r"
        self.max_agents = 3
        self.gh_slots = 2


class _FakeProc:
    def __init__(self):
        self.pid = 31337
        self.rc = None

    def poll(self):
        return self.rc


def test_dispatch_sweep_runs_the_action_in_its_own_pool(tmp_path, monkeypatch):
    """The sweep must never hold a slot a merge-enable is waiting for."""
    spawned: list[dict] = []
    proc = _FakeProc()

    def fake_popen(argv, **kwargs):
        spawned.append({"argv": argv, **kwargs})
        return proc

    monkeypatch.setattr(dispatch.subprocess, "Popen", fake_popen)
    sup = dispatch.Supervisor(_Cfg(tmp_path), db.connect(tmp_path / "q.db"))

    child = sup.dispatch_sweep(interval_s=1234)
    assert child is not None
    assert Path(spawned[0]["argv"][0]) == tmp_path / "v2" / "actions" / "sweep.sh"
    assert spawned[0]["env"]["WORKTREE_SWEEP_INTERVAL"] == "1234"
    assert child.pool == dispatch.MAINT_POOL
    assert child.log_path == tmp_path / "logs" / "v2-sweep.log", "one stable log, not one per hour"
    assert child.run_id is None, "not an item: no runs row"

    # In flight: the gh and agent pools are untouched, and a second sweep is refused.
    assert sup.free("gh") == 2
    assert sup.free("agent") == 3
    assert sup.sweep_in_flight()
    assert sup.dispatch_sweep(interval_s=1234) is None
    assert len(spawned) == 1

    proc.rc = 0
    assert [(c.mode, rc) for c, rc in sup.reap()] == [(dispatch.SWEEP_MODE, 0)]
    assert not sup.sweep_in_flight()


# --------------------------------------------------------------------------- shell half

_OLD = "2020-01-01T00:00:00 +0000"


def _git_env(**overrides: str) -> dict[str, str]:
    env = os.environ.copy()
    env.update({
        "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_AUTHOR_NAME": "Sweep Test", "GIT_AUTHOR_EMAIL": "sweep@example.com",
        "GIT_COMMITTER_NAME": "Sweep Test", "GIT_COMMITTER_EMAIL": "sweep@example.com",
        "GIT_TERMINAL_PROMPT": "0",
    })
    env.update(overrides)
    return env


def _git(cwd: Path, *args: str, when: str | None = None) -> str:
    env = _git_env()
    if when is not None:
        env["GIT_AUTHOR_DATE"] = when
        env["GIT_COMMITTER_DATE"] = when
    return subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True,
                          check=True).stdout.strip()


def _commit(repo: Path, name: str, when: str | None = None) -> str:
    (repo / name).write_text(name, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-m", name, when=when)
    return _git(repo, "rev-parse", "HEAD")


def _base(tmp_path: Path, guards_body: str) -> Path:
    """A pipeline BASE whose lib/ holds only the guards the test scripts, plus an empty repo."""
    base = tmp_path / "base"
    (base / "lib").mkdir(parents=True)
    (base / "lib" / "guards.sh").write_text(textwrap.dedent(guards_body))
    return base


def _run_sweep(base: Path, repo: Path, **env: str) -> subprocess.CompletedProcess[str]:
    run_env = _git_env(BASE=str(base), REPO=str(repo), AGENT_GH_TOKEN="test-token",
                       SLUG="acme/widget", **env)
    return subprocess.run(["bash", str(_V2 / "actions" / "sweep.sh")], env=run_env,
                          capture_output=True, text=True)


_FAKE_GUARDS = '''
    sweep_stale_worktrees() {
      echo called >> "$BASE/state/sweep-called"
      : > "$BASE/locks/.worktree-sweep"
      return 0
    }
'''


def test_sweep_sh_reaches_sweep_stale_worktrees(tmp_path):
    """The wiring, with the function itself replaced by a marker.

    Runs the shipped `sweep.sh` and `common.sh`; only `lib/guards.sh` is the test's, so what is
    proved is that the action bootstraps and CALLS the function — the exact claim v1 lost.
    """
    base = _base(tmp_path, _FAKE_GUARDS)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main", ".")
    result = _run_sweep(base, repo)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (base / "state" / "sweep-called").read_text() == "called\n"
    assert "worktree sweep: complete" in result.stdout


def test_sweep_sh_says_when_the_hourly_guard_sent_it_home(tmp_path):
    """A skip is reported in words that do not match `worktree sweep:`, so a grep counts sweeps."""
    base = _base(tmp_path, '''
        sweep_stale_worktrees() { echo called >> "$BASE/state/sweep-called"; return 0; }
    ''')
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main", ".")
    result = _run_sweep(base, repo)
    assert result.returncode == 0
    assert "sweep skipped" in result.stdout
    assert "worktree sweep:" not in result.stdout


def test_sweep_sh_refuses_when_the_function_is_missing(tmp_path):
    """A guards.sh that lost the function must be a loud EX_SETUP, not a silent 127."""
    base = _base(tmp_path, "true\n")
    result = _run_sweep(base, tmp_path)
    assert result.returncode == dispatch.EX_SETUP
    assert "sweep_stale_worktrees is not defined" in result.stdout


def test_sweep_sh_honours_dry_run(tmp_path):
    base = _base(tmp_path, _FAKE_GUARDS)
    result = _run_sweep(base, tmp_path, DRY_RUN="1")
    assert result.returncode == 0
    assert not (base / "state" / "sweep-called").exists()


def test_sweep_action_is_executable_and_valid():
    """A syntax error in an action is a scheduler that decides correctly and can never act."""
    path = _V2 / "actions" / "sweep.sh"
    assert os.access(path, os.X_OK)
    subprocess.run(["bash", "-n", str(path)], check=True)


def test_end_to_end_a_merged_clean_tree_goes_and_everything_else_stays(tmp_path):
    """The acceptance box, against the REAL `sweep_stale_worktrees` and `release_worktree`.

    `gh` is a shell function layered over the real guards — `common.sh` pins PATH before sourcing
    them, so a stub binary cannot reach the function, but a function defined after it can. GitHub
    says #1 merged and #3 is open; nothing is said about #2 or #4.
    """
    base = _base(tmp_path, f'''
        . "{_PIPE / "lib" / "guards.sh"}"
        gh() {{
          case " $* " in
            *" --state open "*)   printf 'feature/claude-issue-3\\n' ;;
            *" --state merged "*) printf 'feature/claude-issue-1\\n' ;;
          esac
          return 0
        }}
    ''')
    workroot = base / "work"
    origin = tmp_path / "origin.git"
    repo = tmp_path / "repo"
    origin.mkdir()
    repo.mkdir()
    _git(origin, "init", "--bare", "-b", "main", ".")
    _git(repo, "init", "-b", "main", ".")
    _git(repo, "remote", "add", "origin", str(origin))
    _commit(repo, "seed", _OLD)
    _git(repo, "push", "-u", "origin", "main")

    trees: dict[str, Path] = {}
    for n in (1, 2, 3, 4):
        branch = f"feature/claude-issue-{n}"
        path = workroot / branch
        _git(repo, "worktree", "add", "-b", branch, str(path), "main")
        _commit(path, f"work-{n}", _OLD)          # squash-merged or not, never on origin/main
        trees[branch] = path
    (trees["feature/claude-issue-2"] / "scratch.txt").write_text("unsaved", encoding="utf-8")
    three_hours_ago = time.time() - 3 * 3600   # past the function's own 2h grace
    for path in trees.values():
        os.utime(path, (three_hours_ago, three_hours_ago))

    result = _run_sweep(base, repo, WORKROOT=str(workroot))
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert not trees["feature/claude-issue-1"].exists(), "merged + clean + past grace: removed"
    assert trees["feature/claude-issue-2"].exists(), "dirty: kept, merged or not"
    assert trees["feature/claude-issue-3"].exists(), "open PR: kept"
    assert trees["feature/claude-issue-4"].exists(), "unpushed and no merged PR: kept"
    assert "kept: uncommitted changes" in out
    assert "kept: it holds unpushed work and no merged PR" in out
    assert "worktree sweep: removed 1 stale worktree(s)" in out
    assert "worktree sweep: complete" in out
    # The registration went with the directory, and the branch ref did not.
    listed = _git(repo, "worktree", "list", "--porcelain")
    assert str(trees["feature/claude-issue-1"]) not in listed
    assert _git(repo, "log", "-1", "--format=%s", "feature/claude-issue-1") == "work-1"


# --------------------------------------------------------------------------- the dead call site

def test_v1s_call_site_is_still_behind_the_retired_gate():
    """Pins the shape of the defect, so the fix is never mistaken for redundancy.

    `tick.sh` calls the sweep, and `tick.sh` stands down on `V1_RETIRED` before it gets there. If
    either half of that changes, the daemon's call site is still correct — but the reasoning in
    `Daemon.sweep_worktrees` should be revisited.
    """
    tick = (_PIPE / "tick.sh").read_text()
    assert tick.index("V1_RETIRED_FILE=") < tick.index("\nsweep_stale_worktrees\n")
    src = (_V2 / "lemd" / "daemon.py").read_text()
    assert src.index("self.collect()") < src.index("self.sweep_worktrees()") \
        < src.index("if self.cfg.is_paused():")
