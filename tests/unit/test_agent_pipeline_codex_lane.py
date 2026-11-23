"""The Codex review lane (#2561, phase 3 of the model router).

Acceptance boxes covered here:

1. `DRY_RUN=1` shows harness, model and base URL on the launch line (``test_dry_run_*``).
2. A missing or non-executable Codex refuses loudly and touches no lane gauge (``test_missing_*``,
   ``test_*_path``).
4. The exec-policy rules deny each class `claude-headless.json` denies; one test per denial.
5. A stub proxy answering 429, 404 `model_not_found`, a hang, a dead port and a bad key gives the
   right fallback and a Codex-only pause each time.
6. No GPT model on the Claude CLI and no Claude model on Codex, over the whole decision table.

Boxes 3 (a real review through `codex exec`) and the real-rules smoke run need the live box and a
throwaway PR; they are owner-run and named in the PR body.
"""

from __future__ import annotations

import itertools
import json
import shutil
import subprocess
import sys
import textwrap
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_PIPE = _ROOT / "scripts" / "agent-pipeline"
sys.path.insert(0, str(_PIPE / "v2"))

from lemd import codex_lane, route  # noqa: E402

pytestmark = pytest.mark.unit

RULES = codex_lane.load_rules(_PIPE / "config" / "codex-review.rules")
PROFILE = json.loads((_PIPE / "config" / "claude-headless.json").read_text(encoding="utf-8"))


# ------------------------------------------------------------------ failure classes / family / binary

@pytest.mark.parametrize("rc,text,want", [
    (0, "anything 429", ""),
    (1, "unexpected status 429 Too Many Requests", "rate_limit"),
    (1, "usage limit reached for the week", "rate_limit"),
    (1, '{"error":{"code":"model_not_found"}}', "model_not_found"),
    (1, "The model `gpt-9` does not exist", "model_not_found"),
    (1, "unexpected status 401 Unauthorized: invalid api key", "bad_key"),
    (1, "error sending request for url (http://127.0.0.1:1/responses): Connection refused", "proxy_down"),
    (1, "502 Bad Gateway", "proxy_down"),
    (124, "", "timeout"),
    (1, "the reviewer found a bug", "other"),
])
def test_classify_failure(rc, text, want):
    assert codex_lane.classify_failure(rc, text) == want


@pytest.mark.parametrize("harness,model,ok", [
    ("claude", "claude-sonnet-5-5", True), ("claude", "sonnet", True), ("claude", "gpt-5.6-luna", False),
    ("claude", "codex-auto-review", False), ("claude", "", True),
    ("codex", "codex-auto-review", True), ("codex", "gpt-5.6-luna", True),
    ("codex", "claude-sonnet-5-5", False), ("codex", "opus", False), ("codex", "Haiku", False),
    ("ollama", "lem-agent-tier3", True), ("ollama", "gpt-5", False),
])
def test_family_ok(harness, model, ok):
    assert codex_lane.family_ok(harness, model) is ok


def test_no_gpt_model_reaches_claude_and_no_claude_model_reaches_codex_in_the_decision_table():
    """Box 6: a static sweep of `route.decide` over every mode, flag, label set and attempt."""
    labels = [(), ("risk:high",), ("risk:security",), ("agent:model:opus",), ("agent:tier:3",), ("priority:high",)]
    modes = sorted(route.CODE_MODES | route.REVIEW_MODES | route.MECHANICAL_MODES | {"depmajor"})
    for mode, lab, attempt, protected in itertools.product(modes, labels, (1, 3), (False, True)):
        inputs = route.Inputs(mode=mode, labels=frozenset(lab), attempt=attempt, protected_path=protected)
        for paused in (frozenset(), frozenset({route.CODEX}), frozenset({route.CLAUDE})):
            harness, model, _ = route.decide(route.Context(flag="full", inputs=inputs, paused=paused))
            assert codex_lane.family_ok(harness, model), (mode, lab, harness, model)


def test_binary_is_absolute_and_never_found_on_path(tmp_path):
    exe = tmp_path / "codex"
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    exe.chmod(0o755)
    assert codex_lane.resolve_binary({"CODEX_BIN": str(exe)}) == str(exe)
    exe.chmod(0o644)
    assert codex_lane.resolve_binary({"CODEX_BIN": str(exe)}) is None          # not executable
    assert codex_lane.resolve_binary({"CODEX_BIN": "codex", "PATH": str(tmp_path)}) is None   # relative
    assert codex_lane.resolve_binary({"CODEX_BIN": str(tmp_path / "gone")}) is None


def test_parse_usage_sums_turn_events():
    out = "\n".join([
        json.dumps({"type": "turn.started"}),
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 50,
                                                         "output_tokens": 7}}),
        "not json",
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 2}}),
    ])
    assert codex_lane.parse_usage(out) == (151, 9)
    assert codex_lane.parse_usage("") == (0, 0)


# ------------------------------------------------------------------ exec-policy rules

def _decide(command: str) -> str:
    return codex_lane.evaluate(RULES, command.split())


@pytest.mark.parametrize("command", [
    "git status", "git diff origin/main", "git log --oneline -3", "git show HEAD",
    "gh pr view 5", "gh pr diff 5", "gh pr checks 5", "gh issue view 2561", "gh run view 1",
    "cat src/x.py", "grep -rn foo .", "ls -la",
    "poetry run pytest tests/unit -q", "ruff check .",
    "/home/lem/agent-pipeline/lib/gh_safe.sh pr-comment 5 --body-file tmp/r.md",
    "/home/lem/agent-pipeline/lib/review_threads.sh list 5",
])
def test_the_review_toolset_is_allowed(command):
    assert _decide(command) == "allow"


# One row per denial class in claude-headless.json (the PR body lists the same mapping).
@pytest.mark.parametrize("command,why", [
    ("sudo ls", "Bash(sudo *)"), ("su root", "Bash(su *)"),
    ("docker ps", "Bash(docker *)"), ("docker-compose up", "Bash(docker-compose *)"),
    ("systemctl restart lem-agentd", "Bash(systemctl *)"),
    ("bash scripts/deploy.sh", "Bash(bash *deploy.sh*): shell -c / shell scripts"),
    ("bash -c ls", "a shell string defeats prefix matching"),
    ("gh workflow run x.yml", "Bash(gh workflow run *)"),
    ("gh pr merge 5", "Bash(gh pr merge *)"), ("gh api repos/x/y", "Bash(gh api *)"),
    ("gh secret set X", "Bash(gh secret *)"), ("gh auth token", "Bash(gh auth *)"),
    ("gh pr create --title x", "Bash(gh pr create *)"), ("gh pr edit 5 --add-label x", "Bash(gh pr edit *)"),
    ("gh pr comment 5 --body x", "Bash(gh pr comment *)"), ("gh pr ready 5", "Bash(gh pr ready *)"),
    ("gh pr close 5", "Bash(gh pr close *)"), ("gh pr reopen 5", "Bash(gh pr reopen *)"),
    ("gh issue comment 5 --body x", "Bash(gh issue comment *)"), ("gh issue close 5", "Bash(gh issue close *)"),
    ("gh issue edit 5", "Bash(gh issue edit *)"), ("gh issue create --title x", "Bash(gh issue create *)"),
    ("git push", "Bash(git push)"), ("git push origin main", "Bash(git push *)"),
    ("git commit -m x", "Bash(git commit *)"), ("git remote add x y", "Bash(git remote *)"),
    ("git config user.name x", "Bash(git config *)"), ("git -C /opt/lem status", "Bash(git -C /opt/lem *)"),
    ("git rebase origin/main", "git rebase is not a review action"),
    ("git add .", "Bash(git add *)"), ("git rm x", "Bash(git rm *)"), ("git fetch origin refs/x:main", "git fetch"),
    ("rm -rf /", "Bash(rm -rf *)"), ("rm file", "write outside the scratch area"),
    ("tee /etc/x", "write outside the scratch area"), ("cp a /etc/b", "write outside the scratch area"),
    ("mv a b", "write outside the scratch area"), ("dd if=/dev/zero of=/x", "write outside the scratch area"),
    ("sed -i s/a/b/ x", "an interpreter can write anywhere"), ("python3 x.py", "an interpreter can write anywhere"),
    ("curl http://example.com", "Bash(curl *)"), ("wget http://example.com", "Bash(wget *)"),
    ("nc host 80", "network host other than the proxy and GitHub"),
    ("ssh host", "network host other than the proxy and GitHub"),
    ("env", "Bash(env)"), ("printenv HOME", "Bash(printenv *)"),
    ("cat /home/lem/agent-pipeline/secrets.env", "Read(secrets.env)"),
    ("cat /home/lem/agent-pipeline/config.env", "Read(config.env)"),
    ("cat /home/lem/agent-pipeline/state/gh-app-token", "Read(gh-app-token)"),
    ("cat /opt/lem/.env", "Read(/opt/lem/.env)"), ("cat .env", "Read(**/.env)"),
    ("cat /etc/lem", "Read(/etc/lem/**)"), ("ls ~/.ssh", "Read(~/.ssh/**)"),
    ("cat ~/.config/gh", "Read(~/.config/gh/**)"), ("cat ~/.codex", "the Codex credentials"),
    ("codex exec x", "no nested agent"),
    ("cat /proc/self/environ", "printenv by another name"), ("rg --pre sh x .", "rg --pre executes a program"),
])
def test_each_denial_the_claude_profile_enforces_is_denied(command, why):
    assert _decide(command) == "forbidden", why


@pytest.mark.parametrize("command", [
    "make test", "pip install x", "npm ci", "tar xf a.tgz", "find . -delete", "awk '{print}' x", "perl -e 1",
])
def test_an_unlisted_tool_is_refused_by_default(command):
    """No allow rule = `prompt`, and a headless run has nobody to answer it."""
    assert _decide(command) in ("prompt", "forbidden")
    assert _decide(command) != "allow"


def test_the_rules_file_has_only_prefix_rules():
    with pytest.raises(ValueError):
        codex_lane.parse_rules("import os\n")
    with pytest.raises(ValueError):
        codex_lane.parse_rules('prefix_rule(pattern=["x"], decision="maybe")\n')


def test_every_claude_profile_deny_class_has_a_codex_counterpart():
    """The profile's Bash denies, by leading word, all appear as a forbidden rule or are unallowed."""
    for entry in PROFILE["permissions"]["deny"]:
        if not entry.startswith("Bash(") or entry.startswith(("Bash(*", "Bash(gh * ")):
            continue
        if any(s in entry for s in ("/*", "~*", "$*", "..", "--", "\"", "'", " -")):
            continue   # argument-shape denials: a prefix rule cannot express them (see the rules header)
        words = entry[len("Bash("):-1].replace("*", "").split()
        if not words:
            continue
        probe = [w for w in words if not w.startswith(("-", "/", "$", "~", "\"", "'"))] or words[:1]
        assert _decide(" ".join(probe)) != "allow", entry


# ------------------------------------------------------------------ run_lane: fixtures

class _Stub:
    """A local proxy: answers every POST by `mode`, or hangs."""

    def __init__(self, mode: str):
        stub = self
        stub.mode, stub.hits = mode, 0

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                stub.hits += 1
                if stub.mode == "hang":
                    time.sleep(4)   # outlasts the 2s CODEX_TIMEOUT; short so shutdown() is not held up
                    return
                status, body = {
                    "429": (429, '{"error":{"message":"rate limit exceeded"}}'),
                    "model_not_found": (404, '{"error":{"code":"model_not_found"}}'),
                    "bad_key": (401, '{"error":{"message":"Invalid API key"}}'),
                    "ok": (200, "{}"),
                }[stub.mode]
                self.send_response(status)
                self.end_headers()
                self.wfile.write(body.encode())

            def log_message(self, *a):  # silence
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


_FAKE_CODEX = """#!/usr/bin/env python3
import os, sys, urllib.request, urllib.error
env = os.environ
open(env["CODEX_ARGV"], "w").write("\\n".join(sys.argv[1:]) + "\\nHOME_CODEX=" + env.get("CODEX_HOME", ""))
url = env["CLIPROXY_BASE_URL"] + "/responses"
req = urllib.request.Request(url, data=b"{}", headers={"Authorization": "Bearer " + env.get("CLIPROXY_AUTH_TOKEN", "")})
try:
    urllib.request.urlopen(req, timeout=60)
    print('{"type":"turn.completed","usage":{"input_tokens":120,"cached_input_tokens":30,"output_tokens":9}}')
except urllib.error.HTTPError as e:
    print("unexpected status %d: %s" % (e.code, e.read().decode())); sys.exit(1)
except urllib.error.URLError as e:
    print("error sending request for url (%s): %s" % (url, e.reason)); sys.exit(1)
"""


def _install(tmp_path: Path, *, with_codex: bool = True, claude_rc: int = 0) -> dict:
    base = tmp_path / "base"
    (base / "lib").mkdir(parents=True)
    for sh in (_PIPE / "lib").glob("*.sh"):
        (base / "lib" / sh.name).write_text(sh.read_text(encoding="utf-8"), encoding="utf-8")
    (base / "config").mkdir()
    for name in ("claude-headless.json", "codex-review.rules"):
        (base / "config" / name).write_text((_PIPE / "config" / name).read_text(encoding="utf-8"), encoding="utf-8")
    (base / "v2" / "lemd").mkdir(parents=True)
    shutil.copy(_PIPE / "v2" / "codex_lane_cli.py", base / "v2")
    for py in ("__init__.py", "codex_lane.py"):
        shutil.copy(_PIPE / "v2" / "lemd" / py, base / "v2" / "lemd")
    binf = tmp_path / "bin"
    binf.mkdir()
    runs = tmp_path / "claude.runs"
    claude = binf / "claude"
    claude.write_text(
        "#!/bin/sh\n"
        f'echo "model=$(echo "$@" | sed -n "s/.*--model \\([^ ]*\\).*/\\1/p") base=$ANTHROPIC_BASE_URL" >> "{runs}"\n'
        f"exit {claude_rc}\n", encoding="utf-8")
    claude.chmod(0o755)
    codex = tmp_path / "npmbin" / "codex"
    if with_codex:
        codex.parent.mkdir()
        codex.write_text(_FAKE_CODEX, encoding="utf-8")
        codex.chmod(0o755)
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text("gitdir: /nowhere\n", encoding="utf-8")
    return {"base": base, "bin": binf, "codex": codex, "wt": wt, "runs": runs,
            "argv": tmp_path / "codex.argv"}


def _run(tmp_path: Path, fx: dict, *, url: str = "http://127.0.0.1:1/v1", model: str = "codex-auto-review",
         harness: str = "codex", mode: str = "review", dry: bool = False, extra: dict | None = None):
    script = textwrap.dedent(f"""
        set -uo pipefail
        BASE="{fx['base']}"
        LOGDIR="$BASE/logs"; mkdir -p "$LOGDIR"; LOG="$LOGDIR/actions.log"
        . "$BASE/lib/run_lane.sh"
        log() {{ echo "LOG: $*"; }}
        posthog_capture() {{ :; }}
        record_lane_outcome() {{ echo "OUTCOME: lane=$1 ok=$2 ul=$3"; }}
        apply_lane_labels() {{ :; }}
        _emit() {{ :; }}
        run_lane "{fx['wt']}" "prompt" ""
        echo "rc=$? lane=${{TICK_LANE:-}}"
    """)
    env = {
        "PATH": f"{fx['bin']}:/usr/bin:/bin", "HOME": str(tmp_path), "MODE": mode, "PR": "9", "SLOT": "1",
        "DRY_RUN": "1" if dry else "0", "CLAUDE_AVAIL": "0", "OLLAMA_AVAIL": "0", "DEGRADED": "0",
        "LEM_LANE_OVERRIDE": harness, "LEM_LANE_MODEL": model, "LEM_LANE_REASON": "route arm=test",
        "CLIPROXY_BASE_URL": url, "CLIPROXY_AUTH_TOKEN": "test-not-a-key", "CODEX_BIN": str(fx["codex"]),
        "CODEX_ARGV": str(fx["argv"]), "CLAUDE_TIMEOUT": "20s", "CODEX_TIMEOUT": "20s",
        "LITELLM_MASTER_KEY": "test-not-a-key", "OLLAMA_LITELLM_URL": "http://127.0.0.1:4000",
        "CODEX_PAUSE_MINUTES": "60", "BASE": str(fx["base"]),
    }
    env.update(extra or {})
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, check=False)


def _pause_file(fx) -> Path:
    return fx["base"] / "CODEX_PAUSED_UNTIL"


# ------------------------------------------------------------------ box 1: the launch line

def test_dry_run_names_harness_model_and_base_url(tmp_path):
    fx = _install(tmp_path)
    out = _run(tmp_path, fx, url="http://127.0.0.1:8317/v1", dry=True)
    assert "lane=codex harness=codex-exec model=codex-auto-review base_url=http://127.0.0.1:8317/v1" in out.stdout, \
        out.stdout + out.stderr
    assert "rc=0 lane=codex" in out.stdout
    assert not fx["argv"].exists(), "a dry run must not launch codex"


def test_dry_run_of_a_claude_decision_keeps_the_claude_harness(tmp_path):
    fx = _install(tmp_path)
    out = _run(tmp_path, fx, harness="claude", model="claude-sonnet-5-5", dry=True)
    assert "harness=claude-cli model=claude-sonnet-5-5" in out.stdout, out.stdout + out.stderr


# ------------------------------------------------------------------ box 2: a missing binary

def test_missing_codex_refuses_loudly_and_records_no_lane_failure(tmp_path):
    fx = _install(tmp_path, with_codex=False)
    out = _run(tmp_path, fx)
    assert "rc=73" in out.stdout, out.stdout + out.stderr
    assert "REFUSING to dispatch to Codex" in out.stdout and "NOT a lane failure" in out.stdout
    assert "OUTCOME:" not in out.stdout, "the lane gauge must not be touched"
    assert not _pause_file(fx).exists()
    assert not fx["runs"].exists(), "no fallback run: a missing binary is an environment fault"


def test_a_non_executable_codex_is_refused_the_same_way(tmp_path):
    fx = _install(tmp_path)
    fx["codex"].chmod(0o644)
    out = _run(tmp_path, fx)
    assert "rc=73" in out.stdout and "OUTCOME:" not in out.stdout, out.stdout + out.stderr
    assert not _pause_file(fx).exists()


def test_codex_on_path_alone_is_not_enough_because_the_unit_path_is_minimal(tmp_path):
    """The 2026-08-10 trap: resolution is by absolute CODEX_BIN, never a PATH search."""
    fx = _install(tmp_path)
    out = _run(tmp_path, fx, extra={"CODEX_BIN": str(tmp_path / "nope" / "codex"),
                                    "PATH": f"{fx['codex'].parent}:{fx['bin']}:/usr/bin:/bin"})
    assert "rc=73" in out.stdout and "OUTCOME:" not in out.stdout, out.stdout + out.stderr


def test_the_systemd_unit_path_includes_the_codex_directory():
    unit = (_PIPE / "v2" / "systemd" / "lem-agentd.service").read_text(encoding="utf-8")
    path_line = next(line for line in unit.splitlines() if line.startswith("Environment=PATH="))
    assert "/home/lem/.npm-global/bin" in path_line


def test_the_codex_harness_refuses_a_code_mode(tmp_path):
    out = _run(tmp_path, _install(tmp_path), mode="start")
    assert "review modes only" in out.stdout and "rc=73" in out.stdout, out.stdout + out.stderr


# ------------------------------------------------------------------ a clean Codex run

def test_a_clean_run_records_tokens_and_uses_the_isolated_home_and_rules(tmp_path):
    stub = _Stub("ok")
    try:
        fx = _install(tmp_path)
        meta = tmp_path / "meta"
        out = _run(tmp_path, fx, url=stub.url, extra={"LEMD_RUN_META": str(meta)})
    finally:
        stub.close()
    assert "rc=0 lane=codex" in out.stdout, out.stdout + out.stderr
    assert "OUTCOME:" not in out.stdout
    assert meta.read_text(encoding="utf-8") == (
        "lane=codex\nmodel=codex-auto-review\nroute_reason=route arm=test\ntokens_in=150\ntokens_out=9\n")
    argv = fx["argv"].read_text(encoding="utf-8")
    assert "--sandbox\ndanger-full-access" in argv and 'approval_policy="untrusted"' in argv
    assert "-m\ncodex-auto-review" in argv and "wire_api=\"responses\"" in argv
    home = fx["base"] / "codex-home"
    assert f"HOME_CODEX={home}" in argv
    assert (home / "rules" / "default.rules").read_text(encoding="utf-8") == \
        (_PIPE / "config" / "codex-review.rules").read_text(encoding="utf-8")
    assert not _pause_file(fx).exists()


# ------------------------------------------------------------------ box 5: failure injection

@pytest.mark.parametrize("mode,cls", [("429", "rate_limit"), ("model_not_found", "model_not_found"),
                                      ("bad_key", "bad_key")])
def test_a_proxy_error_falls_to_sonnet_and_pauses_only_codex(tmp_path, mode, cls):
    stub = _Stub(mode)
    try:
        fx = _install(tmp_path)
        out = _run(tmp_path, fx, url=stub.url)
    finally:
        stub.close()
    assert f"codex run failed ({cls})" in out.stdout, out.stdout + out.stderr
    assert "rc=0 lane=claude" in out.stdout
    assert fx["runs"].read_text(encoding="utf-8").startswith("model=claude-sonnet-5-5")
    assert "OUTCOME: lane=codex" not in out.stdout and "OUTCOME: lane=claude ok=1" in out.stdout
    assert float(_pause_file(fx).read_text(encoding="utf-8")) > time.time()
    assert not (fx["base"] / "CLAUDE_PAUSED_UNTIL").exists() and not (fx["base"] / "OLLAMA_PAUSED_UNTIL").exists()


def test_a_dead_proxy_falls_to_sonnet_and_pauses_only_codex(tmp_path):
    fx = _install(tmp_path)
    out = _run(tmp_path, fx, url="http://127.0.0.1:1/v1")      # nothing listens on port 1
    assert "codex run failed (proxy_down)" in out.stdout, out.stdout + out.stderr
    assert fx["runs"].read_text(encoding="utf-8").startswith("model=claude-sonnet-5-5")
    assert _pause_file(fx).exists()
    assert not (fx["base"] / "CLAUDE_PAUSED_UNTIL").exists()


def test_a_hang_times_out_then_falls_to_sonnet_and_pauses_only_codex(tmp_path):
    stub = _Stub("hang")
    try:
        fx = _install(tmp_path)
        out = _run(tmp_path, fx, url=stub.url, extra={"CODEX_TIMEOUT": "2s"})
    finally:
        stub.close()
    assert "codex run failed (timeout)" in out.stdout, out.stdout + out.stderr
    assert fx["runs"].read_text(encoding="utf-8").startswith("model=claude-sonnet-5-5")
    assert _pause_file(fx).exists()


def test_when_sonnet_also_fails_the_review_falls_to_ollama_tier_three(tmp_path):
    fx = _install(tmp_path, claude_rc=1)
    out = _run(tmp_path, fx, url="http://127.0.0.1:1/v1")
    assert "falling to Ollama tier 3" in out.stdout, out.stdout + out.stderr
    lines = fx["runs"].read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("model=claude-sonnet-5-5") and lines[1].startswith("model=lem-agent-tier3")
    assert "base=http://127.0.0.1:4000" in lines[1]


def test_a_paused_claude_lane_goes_straight_to_ollama_tier_three(tmp_path):
    fx = _install(tmp_path)
    (fx["base"] / "CLAUDE_PAUSED_UNTIL").write_text(str(int(time.time()) + 600), encoding="utf-8")
    out = _run(tmp_path, fx, url="http://127.0.0.1:1/v1")
    assert "going straight to Ollama tier 3" in out.stdout, out.stdout + out.stderr
    assert fx["runs"].read_text(encoding="utf-8").splitlines() == [
        "model=lem-agent-tier3 base=http://127.0.0.1:4000"]


def test_the_router_stops_choosing_codex_while_its_pause_file_is_live(tmp_path):
    """lane_for reads CODEX_PAUSED_UNTIL, so the next dispatch is routed to Sonnet without a Codex try."""
    (tmp_path / "state").mkdir()
    (tmp_path / "CODEX_PAUSED_UNTIL").write_text(str(int(time.time()) + 600), encoding="utf-8")
    env = {"BASE": str(tmp_path), "LEM_ROUTER": "reviews", "LEM_ROUTE_LABELS": "", "PATH": "/usr/bin:/bin"}
    out = subprocess.run([sys.executable, str(_PIPE / "v2" / "lane_for.py"), "review"],
                         capture_output=True, text=True, env=env, check=False)
    assert "codex_paused" in out.stdout, out.stdout + out.stderr
    assert "LEM_LANE_MODEL='claude-sonnet-5-5'" in out.stdout


# ------------------------------------------------------------------ box 6: the family guard at launch

def test_a_gpt_model_is_refused_on_the_claude_harness(tmp_path):
    fx = _install(tmp_path)
    out = _run(tmp_path, fx, harness="claude", model="gpt-5.6-luna")
    assert "may not run on the claude harness" in out.stdout and "rc=1" in out.stdout, out.stdout + out.stderr
    assert not fx["runs"].exists()


@pytest.mark.parametrize("model", ["claude-sonnet-5-5", "opus", ""])
def test_a_claude_model_or_none_is_refused_on_the_codex_harness(tmp_path, model):
    fx = _install(tmp_path)
    out = _run(tmp_path, fx, harness="codex", model=model)
    assert "may not run on the codex harness" in out.stdout and "rc=1" in out.stdout, out.stdout + out.stderr
    assert not fx["argv"].exists()
