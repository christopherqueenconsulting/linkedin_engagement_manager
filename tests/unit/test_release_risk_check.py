"""`release-risk-check` job logic (#1133): flag a migration-bearing release.

Two layers, tested separately per the pattern in `test_pipeline_selfmod_gate.py`: pure decision
functions (no I/O) get direct unit tests; the thin `gh`-CLI I/O layer is exercised with
`_run_gh` monkeypatched so nothing here ever shells out for real.

Scope note: the `risk:*`-label half of the original design was dropped by owner decision on PR
#1590 (it would have flagged 10 of the last 14 real releases, and `stage-pr.sh` already holds those
PRs for a human to merge). An added migration is the only signal, so the tests that follow are the
only vocabulary this gate has.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "scripts"))

import release_risk_check as rrc  # noqa: E402

REPO = "christopherqueenconsulting/linkedin_engagement_manager"


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=["gh"], returncode=returncode, stdout=stdout, stderr=stderr)


# ---------------------------------------------------------------- resolve_previous_release


def test_finds_the_next_older_entry_from_this_runs_own_tag_position():
    """Never list index 0/1 — anchored to THIS tag's own position, sorted explicitly."""
    releases = [
        {"tagName": "v1.0.2", "createdAt": "2026-08-16T00:00:00Z"},
        {"tagName": "v1.0.1", "createdAt": "2026-08-15T00:00:00Z"},
        {"tagName": "v1.0.0", "createdAt": "2026-08-14T00:00:00Z"},
    ]
    assert rrc.resolve_previous_release(releases, "v1.0.2") == "v1.0.1"
    assert rrc.resolve_previous_release(releases, "v1.0.1") == "v1.0.0"


def test_the_oldest_release_has_no_previous():
    releases = [{"tagName": "v1.0.0", "createdAt": "2026-08-14T00:00:00Z"}]
    assert rrc.resolve_previous_release(releases, "v1.0.0") is None


def test_a_tag_outside_the_fetched_window_resolves_to_nothing():
    releases = [{"tagName": "v1.0.0", "createdAt": "2026-08-14T00:00:00Z"}]
    assert rrc.resolve_previous_release(releases, "v9.9.9") is None


def test_never_trusts_the_apis_own_order():
    """The acceptance criterion, made concrete: feed it already out of order."""
    releases = [
        {"tagName": "v1.0.0", "createdAt": "2026-08-14T00:00:00Z"},
        {"tagName": "v1.0.2", "createdAt": "2026-08-16T00:00:00Z"},
        {"tagName": "v1.0.1", "createdAt": "2026-08-15T00:00:00Z"},
    ]
    assert rrc.resolve_previous_release(releases, "v1.0.2") == "v1.0.1"


def test_a_race_publishing_a_second_release_mid_build_does_not_confuse_the_anchor():
    """A race where a `release:now` release lands mid-build.

    v1.0.3 exists but this run is still about v1.0.2 — anchoring on the tag itself (not position 0)
    still gets it right.
    """
    releases = [
        {"tagName": "v1.0.3", "createdAt": "2026-08-16T01:00:00Z"},
        {"tagName": "v1.0.2", "createdAt": "2026-08-16T00:00:00Z"},
        {"tagName": "v1.0.1", "createdAt": "2026-08-15T00:00:00Z"},
    ]
    assert rrc.resolve_previous_release(releases, "v1.0.2") == "v1.0.1"


# ---------------------------------------------------------------- added_migration_files


def test_identifies_a_newly_added_migration():
    files = [
        {"filename": "CHANGELOG.md", "status": "modified"},
        {
            "filename": "compose/local/database/migrations/V20260816__add_thing.sql",
            "status": "added",
        },
    ]
    assert rrc.added_migration_files(files) == [
        "compose/local/database/migrations/V20260816__add_thing.sql"
    ]


def test_a_modified_migration_is_not_a_new_one():
    """Migrations are additive-only; an edit to an existing file is a different problem."""
    files = [{"filename": "compose/local/database/migrations/V1__old.sql", "status": "modified"}]
    assert rrc.added_migration_files(files) == []


def test_an_added_file_outside_the_migrations_dir_does_not_count():
    files = [{"filename": "src/cqc_lem/utilities/db.py", "status": "added"}]
    assert rrc.added_migration_files(files) == []


def test_multiple_migrations_are_all_named_and_sorted():
    files = [
        {"filename": "compose/local/database/migrations/V2__b.sql", "status": "added"},
        {"filename": "compose/local/database/migrations/V1__a.sql", "status": "added"},
    ]
    assert rrc.added_migration_files(files) == [
        "compose/local/database/migrations/V1__a.sql",
        "compose/local/database/migrations/V2__b.sql",
    ]


def test_no_files_is_the_common_case():
    assert rrc.added_migration_files([]) == []


# ---------------------------------------------------------------- collect_migration_files (300 cap)


def _padding_files(count: int) -> list[dict]:
    return [{"filename": f"src/pad_{i}.py", "status": "modified"} for i in range(count)]


def test_an_untruncated_compare_is_read_straight_off_the_payload(monkeypatch):
    """Below the cap, no per-commit calls at all — the common case stays one API read."""

    def no_calls(args, **kw):
        raise AssertionError(f"must not shell out below the cap: {args}")

    monkeypatch.setattr(rrc, "_run_gh", no_calls)
    compare = {
        "files": [{"filename": "compose/local/database/migrations/V1__x.sql", "status": "added"}],
        "commits": [{"sha": "abc"}],
    }
    assert rrc.collect_migration_files(REPO, compare) == [
        "compose/local/database/migrations/V1__x.sql"
    ]


def test_a_migration_hidden_past_the_300_file_cap_is_still_found(monkeypatch):
    """The cap is silent and unpaginated, so the range's commits get walked individually.

    Measured against the real `v0.147.0...v0.148.0` range: the compare API answers with exactly 300
    files and `?page=2` returns an EMPTY files array — a migration beyond entry 300 would otherwise
    be invisible to the one thing this gate exists to catch.
    """
    commit_payload = json.dumps(
        {
            "files": [
                {"filename": "compose/local/database/migrations/V9__late.sql", "status": "added"}
            ]
        }
    )
    monkeypatch.setattr(rrc, "_run_gh", lambda args, **kw: _completed(0, commit_payload))
    compare = {"files": _padding_files(rrc.COMPARE_FILES_CAP), "commits": [{"sha": "deadbeef"}]}
    assert rrc.collect_migration_files(REPO, compare) == [
        "compose/local/database/migrations/V9__late.sql"
    ]


def test_a_truncated_commit_list_is_warned_about_but_still_walked(monkeypatch, capsys):
    """The commits array has its own 250 limit — say so rather than implying full coverage."""
    monkeypatch.setattr(rrc, "_run_gh", lambda args, **kw: _completed(0, json.dumps({"files": []})))
    compare = {
        "files": _padding_files(rrc.COMPARE_FILES_CAP),
        "commits": [{"sha": "a1"}],
        "total_commits": 300,
    }
    assert rrc.collect_migration_files(REPO, compare) == []
    assert "commit list truncated" in capsys.readouterr().out


def test_a_truncated_compare_keeps_what_the_visible_files_already_proved(monkeypatch):
    """An unreadable per-commit read never erases a migration the compare payload already named."""
    monkeypatch.setattr(rrc, "_run_gh", lambda args, **kw: _completed(1, "", "boom"))
    files = _padding_files(rrc.COMPARE_FILES_CAP - 1)
    files.append({"filename": "compose/local/database/migrations/V1__x.sql", "status": "added"})
    compare = {"files": files, "commits": [{"sha": "deadbeef"}]}
    assert rrc.collect_migration_files(REPO, compare) == [
        "compose/local/database/migrations/V1__x.sql"
    ]


def test_a_commit_without_a_sha_is_skipped_not_crashed_on(monkeypatch):
    monkeypatch.setattr(rrc, "_run_gh", lambda args, **kw: _completed(0, json.dumps({"files": []})))
    compare = {"files": _padding_files(rrc.COMPARE_FILES_CAP), "commits": [{}]}
    assert rrc.collect_migration_files(REPO, compare) == []


def test_fetch_commit_files_is_none_when_the_shape_is_wrong(monkeypatch):
    monkeypatch.setattr(rrc, "_run_gh", lambda args, **kw: _completed(0, json.dumps({"sha": "a"})))
    assert rrc.fetch_commit_files(REPO, "a") is None


# ---------------------------------------------------------------- decide


def test_a_migration_alone_is_enough_to_flag():
    verdict = rrc.decide(migration_files=["compose/local/database/migrations/V1__x.sql"])
    assert verdict.flagged


def test_the_common_case_is_unflagged():
    verdict = rrc.decide(migration_files=[])
    assert not verdict.flagged


def test_summarize_counts_the_migration_files():
    verdict = rrc.decide(migration_files=["a.sql", "b.sql"])
    assert rrc.summarize(verdict) == "2 new migration file(s)"


def test_summarize_says_nothing_found_when_clean():
    assert rrc.summarize(rrc.decide(migration_files=[])) == "nothing found"


# ---------------------------------------------------------------- format_decision_comment


def test_comment_names_the_specific_migration_files():
    verdict = rrc.decide(migration_files=["compose/local/database/migrations/V1__x.sql"])
    body = rrc.format_decision_comment("v1.0.2", "v1.0.1", verdict)
    assert "compose/local/database/migrations/V1__x.sql" in body


def test_comment_says_why_a_migration_is_the_thing_gated():
    """The reason is the whole justification for the narrowed scope — it must be in the comment."""
    verdict = rrc.decide(migration_files=["x"])
    body = rrc.format_decision_comment("v1.0.2", "v1.0.1", verdict)
    assert "one-way" in body
    assert "rolling the image back" in body


def test_comment_states_plainly_there_is_no_automated_unblock():
    verdict = rrc.decide(migration_files=["x"])
    body = rrc.format_decision_comment("v1.0.2", "v1.0.1", verdict)
    assert "audit / notification only" in body
    assert "Nothing in this repo watches replies" in body


def test_comment_gives_the_exact_manual_unblock_command():
    verdict = rrc.decide(migration_files=["x"])
    body = rrc.format_decision_comment("v1.0.2", "v1.0.1", verdict)
    assert "gh workflow run deploy-vps.yml -f tag=v1.0.2" in body


# ---------------------------------------------------------------- gh I/O layer (mocked)


def test_fetch_releases_returns_the_parsed_list(monkeypatch):
    payload = json.dumps([{"tagName": "v1.0.0", "createdAt": "2026-08-14T00:00:00Z"}])
    monkeypatch.setattr(rrc, "_run_gh", lambda args, **kw: _completed(0, payload))
    assert rrc.fetch_releases(REPO, 20) == json.loads(payload)


def test_fetch_releases_fails_open_on_a_gh_error(monkeypatch):
    monkeypatch.setattr(rrc, "_run_gh", lambda args, **kw: _completed(1, "", "rate limited"))
    assert rrc.fetch_releases(REPO, 20) is None


def test_fetch_compare_returns_files_and_commits(monkeypatch):
    payload = json.dumps({"files": [{"filename": "a", "status": "added"}], "commits": []})
    monkeypatch.setattr(rrc, "_run_gh", lambda args, **kw: _completed(0, payload))
    got = rrc.fetch_compare(REPO, "v1.0.0", "v1.0.1")
    assert got["files"][0]["filename"] == "a"


def test_fetch_release_pr_number_prefers_the_release_please_head_ref(monkeypatch):
    payload = json.dumps(
        [{"number": 1585, "head": {"ref": "release-please--branches--main"}}]
    )
    monkeypatch.setattr(rrc, "_run_gh", lambda args, **kw: _completed(0, payload))
    assert rrc.fetch_release_pr_number(REPO, "v1.0.2") == 1585


def test_fetch_release_pr_number_is_none_when_no_pr_is_associated(monkeypatch):
    monkeypatch.setattr(rrc, "_run_gh", lambda args, **kw: _completed(0, "[]"))
    assert rrc.fetch_release_pr_number(REPO, "v1.0.2") is None


def test_post_decision_comment_pipes_the_body_over_stdin(monkeypatch):
    captured = {}

    def fake_gh(args, **kw):
        captured["args"] = args
        captured["input_text"] = kw.get("input_text")
        return _completed(0, "")

    monkeypatch.setattr(rrc, "_run_gh", fake_gh)
    assert rrc.post_decision_comment(REPO, 1585, "the body") is True
    assert captured["input_text"] == "the body"
    assert "--body-file" in captured["args"]
    assert "-" in captured["args"]


def test_post_decision_comment_failure_is_reported_not_raised(monkeypatch):
    monkeypatch.setattr(rrc, "_run_gh", lambda args, **kw: _completed(1, "", "no permission"))
    assert rrc.post_decision_comment(REPO, 1585, "the body") is False


# ---------------------------------------------------------------- write_github_outputs


def test_writes_key_value_lines(tmp_path):
    out = tmp_path / "gh_output"
    with patch.dict("os.environ", {"GITHUB_OUTPUT": str(out)}):
        rrc.write_github_outputs({"flagged": "true", "previous_tag": "v1.0.1"})
    content = out.read_text()
    assert "flagged=true" in content
    assert "previous_tag=v1.0.1" in content


def test_a_missing_github_output_path_is_a_silent_no_op(monkeypatch):
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    rrc.write_github_outputs({"flagged": "true"})  # must not raise


# ---------------------------------------------------------------- main() orchestration


def _fake_gh_router(responses: dict[str, str]):
    """Map a substring of the joined gh args to a canned stdout payload."""

    def _router(args, **kw):
        joined = " ".join(args)
        for needle, payload in responses.items():
            if needle in joined:
                return _completed(0, payload)
        raise AssertionError(f"unexpected gh call, no matching stub: {args}")

    return _router


def test_main_fails_open_with_no_token(monkeypatch, tmp_path):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    out = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    rc = rrc.main(["release_risk_check.py", "--tag", "v1.0.2"])
    assert rc == 0
    assert "flagged=false" in out.read_text()


def test_main_passes_an_unflagged_release_with_no_comment_posted(monkeypatch, tmp_path):
    monkeypatch.setenv("GH_TOKEN", "x")
    out = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    releases = json.dumps(
        [
            {"tagName": "v1.0.2", "createdAt": "2026-08-16T00:00:00Z"},
            {"tagName": "v1.0.1", "createdAt": "2026-08-15T00:00:00Z"},
        ]
    )
    compare = json.dumps({"files": [], "commits": []})
    router = _fake_gh_router({"release list": releases, "compare": compare})
    posted = []
    monkeypatch.setattr(rrc, "_run_gh", router)
    monkeypatch.setattr(rrc, "post_decision_comment", lambda *a, **kw: posted.append(a) or True)

    rc = rrc.main(["release_risk_check.py", "--tag", "v1.0.2"])
    assert rc == 0
    assert "flagged=false" in out.read_text()
    assert posted == []


def test_a_risk_labelled_pr_alone_no_longer_flags_a_release(monkeypatch, tmp_path):
    """The narrowed scope, made concrete (owner decision on PR #1590).

    A release whose only notable content is a PR closing a `risk:*` issue deploys automatically —
    `stage-pr.sh` already held that PR for a human to merge, so re-asking here parked ~71% of real
    releases on a comment nothing watches. No issue/PR label lookup is even attempted: the router
    below has no stub for one, so an attempt would fail the test.
    """
    monkeypatch.setenv("GH_TOKEN", "x")
    out = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    releases = json.dumps(
        [
            {"tagName": "v1.0.2", "createdAt": "2026-08-16T00:00:00Z"},
            {"tagName": "v1.0.1", "createdAt": "2026-08-15T00:00:00Z"},
        ]
    )
    compare = json.dumps(
        {
            "files": [{"filename": "src/cqc_lem/app/engagement/feed.py", "status": "modified"}],
            "commits": [{"sha": "a1", "commit": {"message": "feat: a thing (#1554)"}}],
            "total_commits": 1,
        }
    )
    router = _fake_gh_router({"release list": releases, "compare": compare})
    monkeypatch.setattr(rrc, "_run_gh", router)

    rc = rrc.main(["release_risk_check.py", "--tag", "v1.0.2"])
    assert rc == 0
    assert "flagged=false" in out.read_text()


def test_main_flags_a_migration_and_posts_the_decision_comment(monkeypatch, tmp_path):
    monkeypatch.setenv("GH_TOKEN", "x")
    out = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    releases = json.dumps(
        [
            {"tagName": "v1.0.2", "createdAt": "2026-08-16T00:00:00Z"},
            {"tagName": "v1.0.1", "createdAt": "2026-08-15T00:00:00Z"},
        ]
    )
    compare = json.dumps(
        {
            "files": [
                {
                    "filename": "compose/local/database/migrations/V1__x.sql",
                    "status": "added",
                }
            ],
            "commits": [],
        }
    )
    pulls = json.dumps([{"number": 1585, "head": {"ref": "release-please--branches--main"}}])
    router = _fake_gh_router({"release list": releases, "compare": compare, "pulls": pulls})
    monkeypatch.setattr(rrc, "_run_gh", router)

    posted = {}

    def fake_post(repo, pr_number, body):
        posted["repo"] = repo
        posted["pr_number"] = pr_number
        posted["body"] = body
        return True

    monkeypatch.setattr(rrc, "post_decision_comment", fake_post)

    rc = rrc.main(["release_risk_check.py", "--tag", "v1.0.2"])
    assert rc == 0
    assert "flagged=true" in out.read_text()
    assert posted["pr_number"] == 1585
    assert "compose/local/database/migrations/V1__x.sql" in posted["body"]


def test_main_stays_exit_zero_while_flagged(monkeypatch, tmp_path):
    """The verdict lives in the OUTPUT, never the exit code.

    A non-zero exit would turn the workflow run red for behavior working exactly as designed.
    """
    monkeypatch.setenv("GH_TOKEN", "x")
    out = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    releases = json.dumps(
        [
            {"tagName": "v1.0.2", "createdAt": "2026-08-16T00:00:00Z"},
            {"tagName": "v1.0.1", "createdAt": "2026-08-15T00:00:00Z"},
        ]
    )
    compare = json.dumps(
        {
            "files": [
                {"filename": "compose/local/database/migrations/V1__x.sql", "status": "added"}
            ],
            "commits": [],
        }
    )
    router = _fake_gh_router({"release list": releases, "compare": compare})
    monkeypatch.setattr(rrc, "_run_gh", router)
    monkeypatch.setattr(rrc, "fetch_release_pr_number", lambda repo, tag: None)

    assert rrc.main(["release_risk_check.py", "--tag", "v1.0.2"]) == 0
    assert "flagged=true" in out.read_text()


def test_main_flags_but_skips_the_comment_when_no_comment_is_passed(monkeypatch, tmp_path):
    monkeypatch.setenv("GH_TOKEN", "x")
    out = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    releases = json.dumps(
        [
            {"tagName": "v1.0.2", "createdAt": "2026-08-16T00:00:00Z"},
            {"tagName": "v1.0.1", "createdAt": "2026-08-15T00:00:00Z"},
        ]
    )
    compare = json.dumps(
        {
            "files": [
                {"filename": "compose/local/database/migrations/V1__x.sql", "status": "added"}
            ],
            "commits": [],
        }
    )
    router = _fake_gh_router({"release list": releases, "compare": compare})
    monkeypatch.setattr(rrc, "_run_gh", router)
    posted = []
    monkeypatch.setattr(rrc, "post_decision_comment", lambda *a, **kw: posted.append(a) or True)

    rc = rrc.main(["release_risk_check.py", "--tag", "v1.0.2", "--no-comment"])
    assert rc == 0
    assert "flagged=true" in out.read_text()
    assert posted == []


def test_main_fails_open_when_no_previous_release_is_resolvable(monkeypatch, tmp_path):
    monkeypatch.setenv("GH_TOKEN", "x")
    out = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    releases = json.dumps([{"tagName": "v1.0.2", "createdAt": "2026-08-16T00:00:00Z"}])
    router = _fake_gh_router({"release list": releases})
    monkeypatch.setattr(rrc, "_run_gh", router)

    rc = rrc.main(["release_risk_check.py", "--tag", "v1.0.2"])
    assert rc == 0
    assert "flagged=false" in out.read_text()


def test_main_fails_open_when_the_compare_call_is_unreadable(monkeypatch, tmp_path):
    monkeypatch.setenv("GH_TOKEN", "x")
    out = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    releases = json.dumps(
        [
            {"tagName": "v1.0.2", "createdAt": "2026-08-16T00:00:00Z"},
            {"tagName": "v1.0.1", "createdAt": "2026-08-15T00:00:00Z"},
        ]
    )

    def router(args, **kw):
        joined = " ".join(args)
        if "release list" in joined:
            return _completed(0, releases)
        if "compare" in joined:
            return _completed(1, "", "boom")
        raise AssertionError(args)

    monkeypatch.setattr(rrc, "_run_gh", router)
    rc = rrc.main(["release_risk_check.py", "--tag", "v1.0.2"])
    assert rc == 0
    assert "flagged=false" in out.read_text()
