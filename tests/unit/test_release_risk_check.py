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


def test_a_malformed_release_row_is_dropped_not_raised_on():
    """A row missing `createdAt`/`tagName` must degrade to fail-open, never to a traceback.

    A raise here exits the script non-zero, which reds the job and makes `deploy`'s `needs:` skip a
    release nothing flagged — the exact single point of failure this gate must not become.
    """
    releases = [
        {"tagName": "v1.0.2", "createdAt": "2026-08-16T00:00:00Z"},
        {"tagName": "v1.0.1"},  # no createdAt
        {"createdAt": "2026-08-15T00:00:00Z"},  # no tagName
        {"tagName": "v1.0.0", "createdAt": "2026-08-14T00:00:00Z"},
    ]
    assert rrc.resolve_previous_release(releases, "v1.0.2") == "v1.0.0"


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


def test_summarize_names_the_origin_release_of_a_purely_carried_hold():
    """#1893: a hold with nothing new of its own must say where the migration entered, not 'new'."""
    verdict = rrc.merge_carried_hold(rrc.decide(migration_files=[]), ("a.sql",), "v0.172.6")
    reason = rrc.summarize(verdict)
    assert "new migration file(s)" not in reason
    assert "inherited from v0.172.6" in reason
    assert "still undeployed" in reason


def test_summarize_distinguishes_new_from_inherited_when_both_are_present():
    verdict = rrc.merge_carried_hold(rrc.decide(migration_files=["b.sql"]), ("a.sql",), "v0.172.6")
    reason = rrc.summarize(verdict)
    assert "1 new" in reason
    assert "inherited from v0.172.6" in reason


# ---------------------------------------------------------------- describe_comparison_base


def test_describe_comparison_base_names_the_primary_path():
    desc = rrc.describe_comparison_base("v0.172.5", primary_path_used=True)
    assert "production v0.172.5" in desc
    assert "/api/app-info" in desc
    assert "DEGRADED" not in desc


def test_describe_comparison_base_names_the_degraded_fallback():
    desc = rrc.describe_comparison_base("v0.172.7", primary_path_used=False)
    assert "DEGRADED" in desc
    assert "v0.172.7" in desc


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


def test_comment_names_the_primary_path_when_it_was_used():
    verdict = rrc.decide(migration_files=["x"])
    body = rrc.format_decision_comment("v1.0.2", "v1.0.1", verdict, primary_path_used=True)
    assert "read from `GET /api/app-info`" in body
    assert "DEGRADED" not in body


def test_comment_names_the_degraded_fallback_when_it_was_used():
    verdict = rrc.decide(migration_files=["x"])
    body = rrc.format_decision_comment("v1.0.2", "v1.0.1", verdict, primary_path_used=False)
    assert "DEGRADED" in body
    assert "unreadable" in body


def test_comment_says_which_release_a_carried_migration_entered_in():
    """#1893 acceptance: an inherited migration must be distinguishable from one this release added."""
    verdict = rrc.merge_carried_hold(rrc.decide(migration_files=[]), ("x.sql",), "v0.172.6")
    body = rrc.format_decision_comment("v0.172.7", "v0.172.5", verdict)
    assert "introduced in" in body
    assert "`v0.172.6`" in body
    assert "not** `v0.172.7`" in body or "not `v0.172.7`" in body
    assert "`v0.172.5`" in body


# ---------------------------------------------------------------- gh I/O layer (mocked)


def test_fetch_releases_returns_the_parsed_list(monkeypatch):
    payload = json.dumps([{"tagName": "v1.0.0", "createdAt": "2026-08-14T00:00:00Z"}])
    monkeypatch.setattr(rrc, "_run_gh", lambda args, **kw: _completed(0, payload))
    assert rrc.fetch_releases(REPO, 20) == json.loads(payload)


def test_fetch_releases_fails_open_on_a_gh_error(monkeypatch):
    monkeypatch.setattr(rrc, "_run_gh", lambda args, **kw: _completed(1, "", "rate limited"))
    assert rrc.fetch_releases(REPO, 20) is None


def test_a_hung_gh_call_is_a_failed_result_not_a_raised_timeout(monkeypatch):
    """`subprocess.run(timeout=...)` RAISES — and an escaping raise is fail-CLOSED.

    The script promises every read fails open; a traceback exits non-zero, reds the job, and
    `deploy`'s `needs:` then skips a release that was never flagged.
    """

    def hang(*a, **kw):
        raise subprocess.TimeoutExpired(cmd=["gh"], timeout=rrc.GH_TIMEOUT_SECONDS)

    monkeypatch.setattr(rrc.subprocess, "run", hang)
    result = rrc._run_gh(["gh", "api", "whatever"])
    assert result.returncode != 0
    assert "timed out" in result.stderr


def test_a_missing_gh_binary_is_a_failed_result_not_a_raised_oserror(monkeypatch):
    def missing(*a, **kw):
        raise FileNotFoundError("gh")

    monkeypatch.setattr(rrc.subprocess, "run", missing)
    result = rrc._run_gh(["gh", "api", "whatever"])
    assert result.returncode != 0
    assert rrc._gh_json(["gh", "api", "whatever"]) is None


def test_main_fails_open_when_every_gh_call_hangs(monkeypatch, tmp_path):
    """End to end: a hung GitHub API deploys unflagged, exit 0, never a red job."""
    monkeypatch.setenv("GH_TOKEN", "x")
    out = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))

    def hang(*a, **kw):
        raise subprocess.TimeoutExpired(cmd=["gh"], timeout=rrc.GH_TIMEOUT_SECONDS)

    monkeypatch.setattr(rrc.subprocess, "run", hang)
    assert rrc.main(["release_risk_check.py", "--tag", "v1.0.2"]) == 0
    assert "flagged=false" in out.read_text()


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


# ---------------------------------------------------------------- version_to_tag


def test_version_to_tag_prefixes_a_bare_version():
    assert rrc.version_to_tag("0.172.1") == "v0.172.1"


def test_version_to_tag_leaves_an_already_prefixed_one_alone():
    assert rrc.version_to_tag("v0.172.1") == "v0.172.1"


# ---------------------------------------------------------------- merge_carried_hold


def test_merge_carried_hold_passes_through_when_nothing_is_carried():
    verdict = rrc.decide(migration_files=["b.sql"])
    assert rrc.merge_carried_hold(verdict, ()) is verdict


def test_merge_carried_hold_unions_the_file_sets():
    verdict = rrc.decide(migration_files=["b.sql"])
    merged = rrc.merge_carried_hold(verdict, ("a.sql",))
    assert merged.flagged
    assert merged.migration_files == ("a.sql", "b.sql")


def test_merge_carried_hold_flags_even_when_this_releases_own_diff_is_clean():
    """The whole point (#1859): a clean own-range diff must not clear a still-open hold."""
    verdict = rrc.decide(migration_files=[])
    merged = rrc.merge_carried_hold(verdict, ("a.sql",))
    assert merged.flagged
    assert merged.migration_files == ("a.sql",)


def test_merge_carried_hold_records_the_carried_subset_and_its_origin_tag():
    """#1893: the merged `Verdict` must remember what was carried and where it entered."""
    verdict = rrc.decide(migration_files=[])
    merged = rrc.merge_carried_hold(verdict, ("a.sql",), "v0.172.6")
    assert merged.carried_migration_files == ("a.sql",)
    assert merged.carried_introduced_tag == "v0.172.6"


def test_merge_carried_hold_with_no_introduced_tag_defaults_to_none():
    """Back-compat: existing 2-arg callers still work, with no origin tag recorded."""
    verdict = rrc.decide(migration_files=["b.sql"])
    merged = rrc.merge_carried_hold(verdict, ("a.sql",))
    assert merged.carried_introduced_tag is None


# ---------------------------------------------------------------- fetch_deployed_version


class _FakeHTTPResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_fetch_deployed_version_is_none_for_an_empty_app_url():
    assert rrc.fetch_deployed_version("") is None


def test_fetch_deployed_version_reads_the_version_field(monkeypatch):
    body = json.dumps({"detail": {"version": "0.172.1", "show_version": True}}).encode()
    monkeypatch.setattr(rrc.urllib.request, "urlopen", lambda *a, **kw: _FakeHTTPResponse(body))
    assert rrc.fetch_deployed_version("https://app.example.com") == "0.172.1"


def test_fetch_deployed_version_fails_open_on_a_network_error(monkeypatch):
    def boom(*a, **kw):
        raise rrc.urllib.error.URLError("no route to host")

    monkeypatch.setattr(rrc.urllib.request, "urlopen", boom)
    assert rrc.fetch_deployed_version("https://app.example.com") is None


def test_fetch_deployed_version_fails_open_on_unreadable_json(monkeypatch):
    monkeypatch.setattr(rrc.urllib.request, "urlopen", lambda *a, **kw: _FakeHTTPResponse(b"not json"))
    assert rrc.fetch_deployed_version("https://app.example.com") is None


def test_fetch_deployed_version_fails_open_when_the_version_is_unknown(monkeypatch):
    """`get_app_version()` returns the literal string `'unknown'` when it can't determine one."""
    body = json.dumps({"detail": {"version": "unknown"}}).encode()
    monkeypatch.setattr(rrc.urllib.request, "urlopen", lambda *a, **kw: _FakeHTTPResponse(body))
    assert rrc.fetch_deployed_version("https://app.example.com") is None


# ---------------------------------------------------------------- resolve_carried_migration_files


def test_resolve_carried_migration_files_is_empty_with_no_earlier_release():
    releases = [{"tagName": "v1.0.0", "createdAt": "2026-08-14T00:00:00Z"}]
    assert rrc.resolve_carried_migration_files(REPO, releases, "v1.0.0") == ((), None)


def test_resolve_carried_migration_files_finds_what_the_previous_release_added(monkeypatch):
    releases = [
        {"tagName": "v1.0.1", "createdAt": "2026-08-15T00:00:00Z"},
        {"tagName": "v1.0.0", "createdAt": "2026-08-14T00:00:00Z"},
    ]
    compare = json.dumps(
        {"files": [{"filename": "compose/local/database/migrations/V1__x.sql", "status": "added"}], "commits": []}
    )
    monkeypatch.setattr(rrc, "_run_gh", lambda args, **kw: _completed(0, compare))
    assert rrc.resolve_carried_migration_files(REPO, releases, "v1.0.1") == (
        ("compose/local/database/migrations/V1__x.sql",),
        "v1.0.1",
    )


def test_resolve_carried_migration_files_is_empty_when_nothing_in_the_whole_window_added_anything(monkeypatch):
    releases = [
        {"tagName": "v1.0.1", "createdAt": "2026-08-15T00:00:00Z"},
        {"tagName": "v1.0.0", "createdAt": "2026-08-14T00:00:00Z"},
    ]
    compare = json.dumps({"files": [], "commits": []})
    monkeypatch.setattr(rrc, "_run_gh", lambda args, **kw: _completed(0, compare))
    assert rrc.resolve_carried_migration_files(REPO, releases, "v1.0.1") == ((), None)


def test_resolve_carried_migration_files_fails_open_when_the_compare_is_unreadable(monkeypatch):
    releases = [
        {"tagName": "v1.0.1", "createdAt": "2026-08-15T00:00:00Z"},
        {"tagName": "v1.0.0", "createdAt": "2026-08-14T00:00:00Z"},
    ]
    monkeypatch.setattr(rrc, "_run_gh", lambda args, **kw: _completed(1, "", "boom"))
    assert rrc.resolve_carried_migration_files(REPO, releases, "v1.0.1") == ((), None)


def test_resolve_carried_migration_files_walks_past_a_clean_hop_to_the_real_origin(monkeypatch):
    """#1896: the immediate predecessor can be clean itself while still sitting on an inherited hold.

    v1.0.2 (this hop's `previous_tag`) added nothing of its own over v1.0.1 — it only inherited
    v1.0.1's migration. A one-hop check stops at the clean v1.0.1...v1.0.2 diff and loses the hold;
    walking one more step back to v1.0.0...v1.0.1 finds where it actually entered.
    """
    releases = [
        {"tagName": "v1.0.2", "createdAt": "2026-08-16T00:00:00Z"},
        {"tagName": "v1.0.1", "createdAt": "2026-08-15T00:00:00Z"},
        {"tagName": "v1.0.0", "createdAt": "2026-08-14T00:00:00Z"},
    ]
    compares = {
        "v1.0.1...v1.0.2": json.dumps({"files": [], "commits": []}),
        "v1.0.0...v1.0.1": json.dumps(
            {
                "files": [
                    {"filename": "compose/local/database/migrations/V1__x.sql", "status": "added"}
                ],
                "commits": [],
            }
        ),
    }

    def router(args, **kw):
        joined = " ".join(args)
        for range_key, payload in compares.items():
            if f"compare/{range_key}" in joined:
                return _completed(0, payload)
        raise AssertionError(f"unexpected gh call, no matching stub: {args}")

    monkeypatch.setattr(rrc, "_run_gh", router)
    assert rrc.resolve_carried_migration_files(REPO, releases, "v1.0.2") == (
        ("compose/local/database/migrations/V1__x.sql",),
        "v1.0.1",
    )


# ---------------------------------------------------------------- main(): #1859 acceptance criteria
#
# One shared fixture, the real sequence from the issue: v0.172.0 (clean) -> v0.172.1 (adds
# compose/local/database/migrations/V20260901042458__add_dm_followups_unreadable_reads.sql, from
# #1825, correctly skipped) -> v0.172.2 (clean on its own) -> v0.172.3 (clean on its own). Each test
# below varies only which tag is "this run" and what `/api/app-info` answers.

_V0172_MIGRATION = "compose/local/database/migrations/V20260901042458__add_dm_followups_unreadable_reads.sql"

_V0172_RELEASES = json.dumps(
    [
        {"tagName": "v0.172.3", "createdAt": "2026-09-01T08:00:00Z"},
        {"tagName": "v0.172.2", "createdAt": "2026-09-01T06:00:00Z"},
        {"tagName": "v0.172.1", "createdAt": "2026-09-01T04:00:00Z"},
        {"tagName": "v0.172.0", "createdAt": "2026-08-31T00:00:00Z"},
    ]
)

_V0172_COMPARES = {
    "v0.172.0...v0.172.1": json.dumps(
        {"files": [{"filename": _V0172_MIGRATION, "status": "added"}], "commits": []}
    ),
    "v0.172.0...v0.172.2": json.dumps(
        {"files": [{"filename": _V0172_MIGRATION, "status": "added"}], "commits": []}
    ),
    "v0.172.1...v0.172.2": json.dumps({"files": [], "commits": []}),
    "v0.172.2...v0.172.3": json.dumps({"files": [], "commits": []}),
}


def _v0172_router(**extra_compares):
    compares = {**_V0172_COMPARES, **extra_compares}

    def _router(args, **kw):
        joined = " ".join(args)
        if "release list" in joined:
            return _completed(0, _V0172_RELEASES)
        for range_key, payload in compares.items():
            if f"compare/{range_key}" in joined:
                return _completed(0, payload)
        raise AssertionError(f"unexpected gh call, no matching stub: {args}")

    return _router


def test_ac1_deployed_version_still_behind_the_flagged_release_flags_the_next_one(monkeypatch, tmp_path):
    """#1859 AC1: production on v0.172.0, v0.172.1 was flagged and never deployed by hand.

    v0.172.2's OWN tag-to-tag range (v0.172.1...v0.172.2) is clean — this is exactly the bug: the
    old tag-diff would have deployed it. Reading `/api/app-info` diffs v0.172.0...v0.172.2 instead
    and still sees the migration v0.172.1 added.
    """
    monkeypatch.setenv("GH_TOKEN", "x")
    out = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setattr(rrc, "_run_gh", _v0172_router())
    monkeypatch.setattr(rrc, "fetch_deployed_version", lambda app_url: "0.172.0")

    rc = rrc.main(
        ["release_risk_check.py", "--tag", "v0.172.2", "--app-url", "https://app.example.com", "--no-comment"]
    )
    assert rc == 0
    assert "flagged=true" in out.read_text()


def test_ac2_deployed_version_caught_up_by_hand_clears_the_hold(monkeypatch, tmp_path):
    """#1859 AC2: the owner ran the manual deploy of v0.172.1; the hold clears for v0.172.2."""
    monkeypatch.setenv("GH_TOKEN", "x")
    out = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setattr(rrc, "_run_gh", _v0172_router())
    monkeypatch.setattr(rrc, "fetch_deployed_version", lambda app_url: "0.172.1")

    rc = rrc.main(
        ["release_risk_check.py", "--tag", "v0.172.2", "--app-url", "https://app.example.com", "--no-comment"]
    )
    assert rc == 0
    assert "flagged=false" in out.read_text()


def test_ac3_unreadable_deployed_version_walks_past_a_clean_predecessor_to_the_still_open_hold(
    monkeypatch, tmp_path
):
    """#1859 AC3, corrected by #1896: a clean IMMEDIATE predecessor is not the same as no open hold.

    `/api/app-info` is unreadable for v0.172.3, and its own predecessor v0.172.2 is itself clean
    (`v0.172.1...v0.172.2`) — but v0.172.2 never actually resolved v0.172.1's migration, it only
    inherited it, and there is still no evidence (no readable `/api/app-info`) that anyone ever
    manually deployed it. The one-hop version of this check stopped at v0.172.2's own clean range
    and deployed v0.172.3 unflagged (the original, since-corrected AC3); walking one hop further
    back to `v0.172.0...v0.172.1` finds the hold is still open, so v0.172.3 stays held too.
    """
    monkeypatch.setenv("GH_TOKEN", "x")
    out = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setattr(rrc, "_run_gh", _v0172_router())
    monkeypatch.setattr(rrc, "fetch_deployed_version", lambda app_url: None)

    rc = rrc.main(
        ["release_risk_check.py", "--tag", "v0.172.3", "--app-url", "https://app.example.com", "--no-comment"]
    )
    assert rc == 0
    assert "flagged=true" in out.read_text()


def test_ac4_unreadable_deployed_version_with_a_flagged_previous_release_carries_the_hold_forward(
    monkeypatch, tmp_path
):
    """#1859 AC4: `/api/app-info` unreadable, and the previous release (v0.172.1) WAS flagged.

    v0.172.2's own diff against v0.172.1 is clean, exactly like the bug — but this time the
    fallback path itself recognizes the still-open hold and flags anyway.
    """
    monkeypatch.setenv("GH_TOKEN", "x")
    out = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setattr(rrc, "_run_gh", _v0172_router())
    monkeypatch.setattr(rrc, "fetch_deployed_version", lambda app_url: None)

    rc = rrc.main(
        ["release_risk_check.py", "--tag", "v0.172.2", "--app-url", "https://app.example.com", "--no-comment"]
    )
    assert rc == 0
    assert "flagged=true" in out.read_text()


def test_ac5_carried_hold_message_names_the_real_bound_and_origin_release(monkeypatch, tmp_path, capsys):
    """#1893: the message must name what was actually diffed against and where the hold entered.

    Same fixture as AC4 (`/api/app-info` unreadable, deployed version two releases behind the
    release being gated) — the exact case that produced #1893's misdiagnosis on v0.172.7: the log
    named the previous *tag* (`v0.172.1`) as if this release's own range found the migration, when
    it was actually inherited from `v0.172.1` over `v0.172.0`.
    """
    monkeypatch.setenv("GH_TOKEN", "x")
    out = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setattr(rrc, "_run_gh", _v0172_router())
    monkeypatch.setattr(rrc, "fetch_deployed_version", lambda app_url: None)

    rc = rrc.main(
        ["release_risk_check.py", "--tag", "v0.172.2", "--app-url", "https://app.example.com", "--no-comment"]
    )
    assert rc == 0
    log = capsys.readouterr().out
    # Named the release the migration actually entered in, not the release being gated.
    assert "inherited from v0.172.1" in log
    # Named the real lower bound (the last release before the still-open hold), not v0.172.1.
    assert "v0.172.0" in log
    assert "between v0.172.1 and v0.172.2" not in log


def test_ac5_failed_own_diff_names_the_range_actually_attempted_not_the_carried_bound(
    monkeypatch, tmp_path, capsys
):
    """A still-open hold must not make the fail-open message lie about what was diffed.

    Same fixture as AC4/AC5, except this release's OWN diff (`v0.172.1...v0.172.2`) is itself
    unreadable. `base_tag` gets provisionally reassigned to the carried bound (`v0.172.0`) once a
    hold is found on `v0.172.1` — but that reassignment must not survive into the "could not diff"
    message, because the comparison that actually failed was `v0.172.1...v0.172.2`, never
    `v0.172.0...v0.172.2`. Naming the wrong bound there is exactly #1893's bug, reintroduced.
    """
    monkeypatch.setenv("GH_TOKEN", "x")
    out = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))

    def router(args, **kw):
        joined = " ".join(args)
        if "release list" in joined:
            return _completed(0, _V0172_RELEASES)
        if "compare/v0.172.0...v0.172.1" in joined:
            return _completed(0, _V0172_COMPARES["v0.172.0...v0.172.1"])
        if "compare/v0.172.1...v0.172.2" in joined:
            return _completed(1, "", "rate limited")
        raise AssertionError(f"unexpected gh call, no matching stub: {args}")

    monkeypatch.setattr(rrc, "_run_gh", router)
    monkeypatch.setattr(rrc, "fetch_deployed_version", lambda app_url: None)

    rc = rrc.main(["release_risk_check.py", "--tag", "v0.172.2", "--app-url", "", "--no-comment"])
    assert rc == 0
    assert "flagged=false" in out.read_text()
    log = capsys.readouterr().out
    assert "could not diff v0.172.1...v0.172.2" in log
    assert "v0.172.0...v0.172.2" not in log


# ---------------------------------------------------------------- main(): #1896 acceptance criteria
#
# The real sequence that produced #1896: v0.172.5 (deployed, confirmed) -> v0.172.6 (adds a
# migration, correctly flagged and held) -> v0.172.7 (clean over v0.172.6 but correctly carried one
# hop back) -> v0.172.8 (this run — clean over v0.172.7, and the OLD one-hop carry-forward also
# landed on v0.172.7's own clean range and lost the hold entirely). `/api/app-info` is unreadable
# throughout, matching production's actual state at the time (`PUBLIC_BASE_URL` unset).

_V1896_MIGRATION = "compose/local/database/migrations/V20260901000000__add_recipient_email.sql"

_V1896_RELEASES = json.dumps(
    [
        {"tagName": "v0.172.8", "createdAt": "2026-09-01T12:00:00Z"},
        {"tagName": "v0.172.7", "createdAt": "2026-09-01T09:00:00Z"},
        {"tagName": "v0.172.6", "createdAt": "2026-09-01T06:00:00Z"},
        {"tagName": "v0.172.5", "createdAt": "2026-09-01T00:00:00Z"},
    ]
)

_V1896_COMPARES = {
    "v0.172.5...v0.172.6": json.dumps(
        {"files": [{"filename": _V1896_MIGRATION, "status": "added"}], "commits": []}
    ),
    "v0.172.6...v0.172.7": json.dumps({"files": [], "commits": []}),
    "v0.172.7...v0.172.8": json.dumps({"files": [], "commits": []}),
}


def _v1896_router(**extra_compares):
    compares = {**_V1896_COMPARES, **extra_compares}

    def _router(args, **kw):
        joined = " ".join(args)
        if "release list" in joined:
            return _completed(0, _V1896_RELEASES)
        for range_key, payload in compares.items():
            if f"compare/{range_key}" in joined:
                return _completed(0, payload)
        raise AssertionError(f"unexpected gh call, no matching stub: {args}")

    return _router


def test_ac_1896_two_consecutive_held_releases_still_flags_the_third(monkeypatch, tmp_path):
    """The exact case that produced #1896: a hold two releases deep must not evaporate.

    `/api/app-info` is unreadable, so this is entirely the fallback path. v0.172.7's own carry
    (one hop) correctly caught v0.172.6's migration; v0.172.8's own diff (`v0.172.7...v0.172.8`) is
    clean, and so is a one-hop check against `v0.172.7`'s own range — only walking a second hop back
    to `v0.172.5...v0.172.6` finds the still-open migration.
    """
    monkeypatch.setenv("GH_TOKEN", "x")
    out = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setattr(rrc, "_run_gh", _v1896_router())
    monkeypatch.setattr(rrc, "fetch_deployed_version", lambda app_url: None)

    rc = rrc.main(
        ["release_risk_check.py", "--tag", "v0.172.8", "--app-url", "https://app.example.com", "--no-comment"]
    )
    assert rc == 0
    assert "flagged=true" in out.read_text()


def test_ac_1896_log_names_the_degraded_fallback_when_app_info_is_unreadable(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("GH_TOKEN", "x")
    out = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setattr(rrc, "_run_gh", _v1896_router())
    monkeypatch.setattr(rrc, "fetch_deployed_version", lambda app_url: None)

    rc = rrc.main(
        ["release_risk_check.py", "--tag", "v0.172.8", "--app-url", "https://app.example.com", "--no-comment"]
    )
    assert rc == 0
    log = capsys.readouterr().out
    assert "DEGRADED" in log
    assert "/api/app-info unreadable" in log


def test_ac_1896_log_names_the_primary_path_when_app_info_is_readable(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("GH_TOKEN", "x")
    out = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setattr(rrc, "_run_gh", _v1896_router())
    monkeypatch.setattr(rrc, "fetch_deployed_version", lambda app_url: "0.172.5")

    rc = rrc.main(
        ["release_risk_check.py", "--tag", "v0.172.6", "--app-url", "https://app.example.com", "--no-comment"]
    )
    assert rc == 0
    log = capsys.readouterr().out
    assert "production v0.172.5" in log
    assert "read from /api/app-info" in log
    assert "DEGRADED" not in log
