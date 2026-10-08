"""Root-run pipeline units execute root-owned copies of their scripts.

`lem-gh-token.service` and `lem-agentd-watchdog.service` run as root. They execute copies in
`/usr/local/lib/lem/` that only `install-root-scripts.sh`, run as root, writes; `sync.sh` runs as the
pipeline user, never installs them, and only warns when they are behind the repo.

The installer is exercised here against a scratch DEST by sourcing it and calling `install_all` with
the test's own uid as the expected owner, so no test needs root.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

PIPELINE = Path(__file__).resolve().parents[2] / "scripts" / "agent-pipeline"
INSTALLER = PIPELINE / "install-root-scripts.sh"
SYNC = PIPELINE / "sync.sh"
SOURCES = ("install-root-scripts.sh", "lib/gh_app_token.sh", "lib/posthog.sh", "v2/watchdog.sh")
ROOT_LIB = "/usr/local/lib/lem/"


def _units() -> list[Path]:
    return sorted([*(PIPELINE / "systemd").glob("*.service"),
                   *(PIPELINE / "v2" / "systemd").glob("*.service")])


def _runs_as_root(text: str) -> bool:
    users = re.findall(r"^User=(\S+)", text, re.M)
    return not users or users[-1] == "root"


def _exec_lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if re.match(r"^Exec\w*=", ln)]


def _copy_pipeline(tmp_path: Path) -> Path:
    """The four files the installer reads, in their repo layout, owned by the test uid."""
    root = tmp_path / "repo"
    for rel in SOURCES:
        dst = root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(PIPELINE / rel, dst)
    for p in [root, *root.rglob("*")]:
        p.chmod(0o755)
    return root


def _render(name: str, installer: Path = INSTALLER, dest: str = ROOT_LIB.rstrip("/")):
    return subprocess.run(["bash", str(installer), "--render", name], capture_output=True,
                          text=True, env={"PATH": "/usr/bin:/bin", "LEM_ROOT_LIB_DIR": dest})


def _install(installer: Path, dest: Path) -> subprocess.CompletedProcess:
    """Source the installer and run install_all with the test uid standing in for root."""
    script = f'. "{installer}"; OWNER="$(id -u)"; GROUP="$(id -g)"; install_all'
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin", "LEM_ROOT_LIB_DIR": str(dest)})


def _check(installer: Path, dest: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(installer), "--check"], capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin", "LEM_ROOT_LIB_DIR": str(dest)})


# ---------------------------------------------------------------- the unit files


def test_root_units_execute_nothing_from_the_pipeline_tree():
    root_units = [u for u in _units() if _runs_as_root(u.read_text())]
    assert {u.name for u in root_units} >= {"lem-gh-token.service", "lem-agentd-watchdog.service"}
    for unit in root_units:
        for line in _exec_lines(unit.read_text()):
            assert "/home/" not in line, f"{unit.name}: {line}"


def test_every_root_lib_path_a_unit_uses_is_one_the_installer_writes():
    referenced = set()
    for unit in _units():
        for line in _exec_lines(unit.read_text()):
            referenced |= set(re.findall(re.escape(ROOT_LIB) + r"([\w.-]+)", line))
    assert referenced == {"gh_app_token.sh", "watchdog.sh"}
    for name in referenced:
        assert _render(name).returncode == 0, name


# ---------------------------------------------------------------- rendering


def test_rendered_copies_read_no_code_from_the_pipeline_tree():
    for name in ("gh_app_token.sh", "posthog.sh", "watchdog.sh"):
        got = _render(name)
        assert got.returncode == 0, got.stderr
        assert "$BASE/lib/" not in got.stdout, name
        assert not re.search(r'^\s*\.\s+"?\$BASE', got.stdout, re.M), name
        assert got.stdout.startswith("#!/usr/bin/env bash\n# Root-owned copy of"), name


def test_the_watchdog_copy_uses_the_root_owned_telemetry_helper_and_the_journal():
    out = _render("watchdog.sh").stdout
    assert 'POSTHOG_LIB="/usr/local/lib/lem/posthog.sh"' in out
    assert re.search(r"^LOG=/dev/null", out, re.M)
    assert '$BASE/logs/' not in out


def test_the_telemetry_copy_reads_its_key_from_a_root_owned_file():
    out = _render("posthog.sh").stdout
    assert "[ -f /etc/lem/posthog.env ] && . /etc/lem/posthog.env" in out
    assert '. "$BASE/secrets.env"' not in out


def test_the_token_copy_is_the_repo_file_plus_a_header():
    out = _render("gh_app_token.sh").stdout.splitlines()
    repo = (PIPELINE / "lib" / "gh_app_token.sh").read_text().splitlines()
    assert out[0] == repo[0]
    assert out[2:] == repo[1:]


def test_a_rewrite_that_no_longer_matches_is_a_hard_failure(tmp_path):
    root = _copy_pipeline(tmp_path)
    wd = root / "v2" / "watchdog.sh"
    wd.write_text(wd.read_text().replace('POSTHOG_LIB="$BASE/lib/posthog.sh"',
                                         'POSTHOG_LIB="${BASE}/lib/posthog.sh"'))
    got = _render("watchdog.sh", installer=root / "install-root-scripts.sh")
    assert got.returncode != 0
    assert "rewrite matched 0 lines" in got.stderr


def test_a_new_source_line_from_the_tree_is_refused(tmp_path):
    root = _copy_pipeline(tmp_path)
    lib = root / "lib" / "gh_app_token.sh"
    lib.write_text(lib.read_text() + '\n. "$BASE/lib/labels.sh"\n')
    got = _render("gh_app_token.sh", installer=root / "install-root-scripts.sh")
    assert got.returncode == 3
    assert "still reads code from $BASE" in got.stderr


# ---------------------------------------------------------------- install mode


@pytest.mark.skipif(os.geteuid() == 0, reason="the refusal is for non-root callers")
def test_install_mode_refuses_without_root(tmp_path):
    got = subprocess.run(["bash", str(INSTALLER)], capture_output=True, text=True,
                         env={"PATH": "/usr/bin:/bin", "LEM_ROOT_LIB_DIR": str(tmp_path / "d")})
    assert got.returncode == 1
    assert "runs as root only" in got.stderr
    assert not (tmp_path / "d").exists()


def test_install_places_755_copies_prints_hashes_and_is_idempotent(tmp_path):
    root = _copy_pipeline(tmp_path)
    dest = tmp_path / "lib-lem"
    first = _install(root / "install-root-scripts.sh", dest)
    assert first.returncode == 0, first.stderr
    for name in ("gh_app_token.sh", "posthog.sh", "watchdog.sh"):
        path = dest / name
        assert oct(path.stat().st_mode & 0o777) == "0o755"
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert f"{digest}  {path}  (installed)" in first.stdout
    mtimes = {p.name: p.stat().st_mtime_ns for p in dest.iterdir()}

    second = _install(root / "install-root-scripts.sh", dest)
    assert second.returncode == 0, second.stderr
    assert second.stdout.count("(unchanged)") == 3
    assert {p.name: p.stat().st_mtime_ns for p in dest.iterdir()} == mtimes
    assert not list(dest.glob(".*")), "no temp files left behind"


def test_check_reports_current_then_drift(tmp_path):
    root = _copy_pipeline(tmp_path)
    dest = tmp_path / "lib-lem"
    installer = root / "install-root-scripts.sh"
    assert _check(installer, dest).returncode == 3          # nothing installed yet
    assert _install(installer, dest).returncode == 0
    assert _check(installer, dest).returncode == 0
    (root / "lib" / "gh_app_token.sh").write_text(
        (root / "lib" / "gh_app_token.sh").read_text() + "# changed\n")
    got = _check(installer, dest)
    assert got.returncode == 3
    assert "DIFFERS" in got.stdout and "gh_app_token.sh" in got.stdout


def test_install_refuses_a_source_another_user_can_write(tmp_path):
    root = _copy_pipeline(tmp_path)
    (root / "lib" / "posthog.sh").chmod(0o775)
    dest = tmp_path / "lib-lem"
    got = _install(root / "install-root-scripts.sh", dest)
    assert got.returncode != 0
    assert "untrusted source path" in got.stderr
    assert not dest.exists() or not any(dest.iterdir())


# ---------------------------------------------------------------- sync.sh


def test_sync_only_ever_checks_the_root_copies():
    body = SYNC.read_text()
    calls = [ln for ln in body.splitlines()
             if "ROOT_INSTALLER" in ln and not ln.lstrip().startswith("#") and '"$ROOT_INSTALLER"' in ln
             and "bash" in ln]
    assert calls and all("--check" in ln for ln in calls), calls
    code = "\n".join(ln for ln in body.splitlines() if not ln.lstrip().startswith("#"))
    assert "/usr/local/lib/lem" not in code
    assert not re.search(r"\b(install|cp|mv|tee)\b[^\n]*(/usr/local|/etc/)", code)


@pytest.fixture
def sync_box(tmp_path: Path):
    """A scratch BASE and git mirror whose pipeline tree carries the real root-script sources."""
    base, src, bin_ = tmp_path / "base", tmp_path / "src", tmp_path / "bin"
    for d in (base / "state", base / "logs", base / "lib", base / "v2", bin_):
        d.mkdir(parents=True)
    (base / "v2" / "marker.py").write_text("v = 1\n")
    pipeline = src / "scripts" / "agent-pipeline"
    for rel in SOURCES:
        (pipeline / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(PIPELINE / rel, pipeline / rel)
    (pipeline / "install.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
    for p in [src, *src.rglob("*")]:
        p.chmod(0o755)
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    for cmd in (["git", "init", "-q", "-b", "main"], ["git", "add", "-A"],
                ["git", "commit", "-qm", "init"],
                ["git", "update-ref", "refs/remotes/origin/main", "HEAD"]):
        subprocess.run(cmd, cwd=src, env=env, check=True, capture_output=True)
    for name in ("systemctl", "sudo"):
        (bin_ / name).write_text("#!/usr/bin/env bash\nexit 0\n")
        (bin_ / name).chmod(0o755)
    dest = tmp_path / "lib-lem"
    run_env = {"PATH": f"{bin_}:/usr/bin:/bin", "BASE": str(base), "HOME": str(base),
               "LEM_SYNC_SRC": str(src), "LEM_SYNC_VERIFY_SECONDS": "1",
               "LEM_ROOT_LIB_DIR": str(dest)}
    return pipeline, dest, run_env


def test_sync_warns_when_root_copies_are_behind_and_writes_none(sync_box):
    pipeline, dest, run_env = sync_box
    got = subprocess.run(["bash", str(SYNC)], capture_output=True, text=True, timeout=60,
                         env=run_env)
    assert "WARNING: root-owned script copies differ" in got.stdout
    assert "install-root-scripts.sh" in got.stdout
    assert not dest.exists(), "sync.sh must never create or write the root-owned copies"


def test_sync_is_quiet_about_root_copies_that_match(sync_box):
    pipeline, dest, run_env = sync_box
    assert _install(pipeline / "install-root-scripts.sh", dest).returncode == 0
    got = subprocess.run(["bash", str(SYNC)], capture_output=True, text=True, timeout=60,
                         env=run_env)
    assert "root-owned script copies differ" not in got.stdout
