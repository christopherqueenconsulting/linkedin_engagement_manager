"""Tests for `seed_graft` in scripts/agent-pipeline/lib/guards.sh.

`seed_graft` copies the main checkout's graft/ context graph into a worktree the pipeline just
built, then runs an incremental `graft build`. It sits inside `add_worktree`, whose stdout IS its
return value and whose failure costs a whole tick, so the properties that matter are the negative
ones: it prints nothing, it never fails, and it never leaves an unignored 120MB tree an agent could
commit. The function is lifted verbatim from the shipped file and run for real against a stubbed
`graft`, following the harness in test_agent_pipeline_trust_boundary.py.
"""

import re
import subprocess
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

PIPELINE = Path(__file__).resolve().parents[2] / "scripts" / "agent-pipeline"
GUARDS = (PIPELINE / "lib" / "guards.sh").read_text(encoding="utf-8")
LINUXBREW_BIN = "/home/linuxbrew/.linuxbrew/bin"


def _seed_graft() -> str:
    """The shipped `seed_graft` function body, lifted from lib/guards.sh."""
    block = re.search(r"\nseed_graft\(\) \{.*?\n\}\n", GUARDS, re.S)
    assert block, "seed_graft not found in lib/guards.sh"
    return block.group(0)


@pytest.fixture
def layout(tmp_path: Path) -> dict[str, Path]:
    """A main checkout holding a built graph, and a fresh git worktree that ignores graft/."""
    repo = tmp_path / "repo"
    (repo / "graft" / ".cache" / "session").mkdir(parents=True)
    (repo / "graft" / "INDEX.md").write_text("# graft — repo map\n", encoding="utf-8")
    (repo / "graft" / ".cache" / "extract.abc.json").write_text("{}", encoding="utf-8")
    (repo / "graft" / ".cache" / "session" / "s1.json").write_text("{}", encoding="utf-8")

    wt = tmp_path / "wt"
    wt.mkdir()
    subprocess.run(["git", "init", "-q", str(wt)], check=True)
    (wt / ".gitignore").write_text("/graft/\n", encoding="utf-8")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    return {"repo": repo, "wt": wt, "bin": bin_dir, "calls": tmp_path / "graft-calls.txt"}


def _stub_graft(layout: dict[str, Path], exit_code: int = 0) -> None:
    """Install a `graft` that records its argv and exits with `exit_code`."""
    stub = layout["bin"] / "graft"
    stub.write_text(
        f'#!/usr/bin/env bash\necho "$*" >> "{layout["calls"]}"\necho noisy-build-output\n'
        f"exit {exit_code}\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)


def _run(layout: dict[str, Path]) -> subprocess.CompletedProcess:
    """Run the real `seed_graft` against the layout, the way add_worktree calls it."""
    script = (
        "set -uo pipefail\n"
        f'REPO="{layout["repo"]}"\n'
        'log() { echo "LOG: $*" >&2; }\n'
        f"{_seed_graft()}\n"
        + textwrap.dedent(f'seed_graft "{layout["wt"]}"; echo "rc=$?" >&2\n')
    )
    env = {"PATH": f"{layout['bin']}:/usr/bin:/bin", "HOME": str(layout["wt"].parent)}
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)


def _calls(layout: dict[str, Path]) -> list[str]:
    path = layout["calls"]
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


class TestSeedGraft:
    def test_copies_the_graph_and_builds_incrementally(self, layout: dict[str, Path]) -> None:
        _stub_graft(layout)
        r = _run(layout)

        assert (layout["wt"] / "graft" / "INDEX.md").is_file()
        assert (layout["wt"] / "graft" / ".cache" / "extract.abc.json").is_file()
        assert _calls(layout) == [f"build {layout['wt']} --no-gitignore --no-ignore"]
        assert "rc=0" in r.stderr

    def test_prints_nothing_to_stdout(self, layout: dict[str, Path]) -> None:
        """add_worktree's stdout is the worktree path its callers capture — one stray line breaks it."""
        _stub_graft(layout)
        assert _run(layout).stdout == ""

    def test_drops_the_main_checkouts_session_state(self, layout: dict[str, Path]) -> None:
        _stub_graft(layout)
        _run(layout)
        assert not (layout["wt"] / "graft" / ".cache" / "session").exists()

    def test_no_graph_in_the_main_checkout_is_a_no_op(self, layout: dict[str, Path]) -> None:
        _stub_graft(layout)
        subprocess.run(["rm", "-rf", str(layout["repo"] / "graft")], check=True)
        r = _run(layout)

        assert not (layout["wt"] / "graft").exists()
        assert _calls(layout) == []
        assert "rc=0" in r.stderr

    def test_graft_not_installed_is_a_no_op(self, layout: dict[str, Path]) -> None:
        r = _run(layout)  # no stub on PATH

        assert not (layout["wt"] / "graft").exists()
        assert r.stdout == ""
        assert "rc=0" in r.stderr

    def test_a_worktree_that_does_not_ignore_graft_is_never_seeded(
        self, layout: dict[str, Path]
    ) -> None:
        """A pre-#2039 branch would show the copied graph as untracked files an agent could commit."""
        _stub_graft(layout)
        (layout["wt"] / ".gitignore").write_text("", encoding="utf-8")
        r = _run(layout)

        assert not (layout["wt"] / "graft").exists()
        assert _calls(layout) == []
        assert "rc=0" in r.stderr

    def test_a_failed_build_keeps_the_copy_and_still_succeeds(self, layout: dict[str, Path]) -> None:
        _stub_graft(layout, exit_code=1)
        r = _run(layout)

        assert (layout["wt"] / "graft" / "INDEX.md").is_file()
        assert r.stdout == ""
        assert "graft build failed" in r.stderr
        assert "rc=0" in r.stderr


class TestWiring:
    def test_add_worktree_seeds_before_returning_the_path(self) -> None:
        body = re.search(r"\nadd_worktree\(\) \{.*?\n\}\n", GUARDS, re.S)
        assert body, "add_worktree not found in lib/guards.sh"
        lines = body.group(0)
        assert 'seed_graft "$wt"' in lines
        assert lines.index('seed_graft "$wt"') < lines.index('echo "$wt"')

    @pytest.mark.parametrize("script", ["tick.sh", "v2/actions/common.sh"])
    def test_runner_path_reaches_graft_without_shadowing_system_commands(self, script: str) -> None:
        source = (PIPELINE / script).read_text(encoding="utf-8")
        exported = re.search(r'^export PATH="([^"]+)"', source, re.M)
        assert exported, f"no PATH export in {script}"
        dirs = exported.group(1).split(":")
        assert LINUXBREW_BIN in dirs
        assert dirs.index(LINUXBREW_BIN) > dirs.index("/usr/bin")
        assert dirs.index(LINUXBREW_BIN) > dirs.index("/bin")

    def test_claude_code_worktrees_copy_the_graph(self) -> None:
        include = (PIPELINE.parents[1] / ".worktreeinclude").read_text(encoding="utf-8")
        assert "graft/" in [line.strip() for line in include.splitlines()]
