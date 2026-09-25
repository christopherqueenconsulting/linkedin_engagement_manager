"""`scripts/deploy_hold_notify.py`: ONE hold issue, email on a state change, auto-close, drift.

Covers the `needs-human` hold issue, the email sent only on a state change, auto-close once
production catches up, and the hourly drift backstop. Every `gh` call and the SendGrid POST are
mocked — nothing here touches the network.
"""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "scripts"))

import deploy_hold_notify as dhn  # noqa: E402
import release_risk_check as rrc  # noqa: E402

REPO = "o/r"
NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)


class FakeGh:
    """Records every `gh` call; answers `issue list` from `issues`, everything else from `replies`."""

    def __init__(self, issues=None, releases=None, fail=()):
        self.issues = issues
        self.releases = releases
        self.fail = set(fail)
        self.calls: list[tuple[list[str], str | None]] = []

    def __call__(self, args, input_text=None):
        self.calls.append((args, input_text))
        verb = " ".join(args[1:3])
        if verb in self.fail:
            return subprocess.CompletedProcess(args, 1, "", "boom")
        if verb == "issue list":
            if self.issues is None:
                return subprocess.CompletedProcess(args, 1, "", "api down")
            return subprocess.CompletedProcess(args, 0, json.dumps(self.issues), "")
        if verb == "release list":
            return subprocess.CompletedProcess(args, 0, json.dumps(self.releases), "")
        if verb == "issue create":
            return subprocess.CompletedProcess(args, 0, f"https://github.com/{REPO}/issues/42\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    def verbs(self):
        return [" ".join(a[1:3]) for a, _ in self.calls]

    def call(self, verb):
        return next((a, i) for a, i in self.calls if " ".join(a[1:3]) == verb)


@pytest.fixture
def gh(monkeypatch):
    def install(**kw):
        fake = FakeGh(**kw)
        monkeypatch.setattr(rrc, "_run_gh", fake)
        return fake

    return install


@pytest.fixture
def mails(monkeypatch):
    sent = []
    monkeypatch.setattr(dhn, "send_owner_email", lambda subject, text: sent.append((subject, text)) or True)
    return sent


def _issue(number, target, files=(), extra=""):
    state = dhn.HoldState(target_tag=target, files=frozenset(files))
    return {"number": number, "body": dhn.render_body(state, "", "hold", "old hold section") + extra}


# ---------------------------------------------------------------- pure helpers


def test_version_helpers():
    assert dhn.parse_version("v0.178.1") == (0, 178, 1)
    assert dhn.parse_version("0.9") == (0, 9)
    assert dhn.parse_version("latest") is None
    assert dhn.parse_version(None) is None
    assert dhn.version_reached("v0.178.1", "v0.178.1")
    assert dhn.version_reached("v0.179.0", "v0.178.1")
    assert not dhn.version_reached("v0.176.5", "v0.178.1")
    assert not dhn.version_reached(None, "v0.178.1")
    assert dhn.newer_tag("v0.177.0", "v0.178.1") == "v0.178.1"
    assert dhn.newer_tag("v0.178.1", "v0.10.0") == "v0.178.1"
    assert dhn.newer_tag(None, "v1") == "v1"
    assert dhn.newer_tag("v1", "junk") == "v1"
    assert dhn.newer_tag("junk", "also") == "junk"


def test_marker_round_trips():
    state = dhn.HoldState(target_tag="v0.178.1", files=frozenset({"V2__b.sql", "V1__a.sql"}))
    marker = dhn.render_marker(state)
    assert marker == "<!-- lem-deploy-hold target=v0.178.1 files=V1__a.sql,V2__b.sql -->"
    assert dhn.parse_marker(f"text\n{marker}\nmore") == state
    assert dhn.parse_marker("<!-- lem-deploy-hold target= files= -->") == dhn.HoldState()
    assert dhn.parse_marker("no marker here") is None


def test_upsert_section_replaces_only_its_own_section():
    body = dhn.upsert_section("head", "hold", "reasons A")
    body = dhn.upsert_section(body, "drift", "lag 1h")
    body = dhn.upsert_section(body, "drift", "lag 5h")
    assert "reasons A" in body and "lag 5h" in body and "lag 1h" not in body
    assert body.count("<!-- section:drift -->") == 1


def test_render_body_keeps_the_other_writers_section_and_names_the_newest_command():
    first = dhn.render_body(dhn.HoldState("v1.0.1"), "", "hold", "why it held")
    second = dhn.render_body(dhn.HoldState("v1.0.3"), first, "drift", "3.5h behind")
    assert "why it held" in second and "3.5h behind" in second
    assert "gh workflow run deploy-vps.yml -f tag=v1.0.3" in second
    assert "tag=v1.0.1" not in second
    assert second.count("lem-deploy-hold") == 1


def test_format_sections():
    hold = dhn.format_hold_section("v1.0.2", None, [("m/V3__drop.sql", ["DROP clause"])])
    assert "unknown (/api/app-info unreadable)" in hold
    assert "- `m/V3__drop.sql`\n  - DROP clause" in hold
    drift = dhn.format_drift_section("v1.0.0", "v1.0.3", "v1.0.1", 4.25, 3)
    assert "**4.2h**" in drift and "threshold 3h" in drift


def test_undeployed_releases_and_hours_since():
    releases = [
        {"tagName": "v1.0.3", "createdAt": "2026-09-25T10:00:00Z"},
        {"tagName": "v1.0.1", "createdAt": "2026-09-25T02:00:00Z"},
        {"tagName": "v1.0.0", "createdAt": "2026-09-24T00:00:00Z"},
        {"tagName": "nightly", "createdAt": "2026-09-25T11:00:00Z"},
        {"tagName": "v1.0.2"},
        "junk",
    ]
    assert [r["tagName"] for r in dhn.undeployed_releases(releases, "v1.0.0")] == ["v1.0.1", "v1.0.3"]
    assert dhn.undeployed_releases(releases, "unknown") == []
    assert dhn.hours_since("2026-09-25T02:00:00Z", NOW) == 10
    assert dhn.hours_since("2026-09-25T09:00:00", NOW) == 3
    assert dhn.hours_since("yesterday", NOW) is None


# ---------------------------------------------------------------- the one issue


def test_first_hold_creates_the_issue_labelled_needs_human_and_notifies(gh):
    fake = gh(issues=[])
    result = dhn.upsert_hold_issue(
        REPO, target_tag="v1.0.2", new_files=["m/V3__drop.sql"], section="hold",
        section_markdown="why", create_when_unreadable=True,
    )
    assert result == dhn.UpsertResult("created", 42, True)
    args, body = fake.call("issue create")
    assert args[args.index("--label") + 1] == "needs-human"
    assert "v1.0.2" in args[args.index("--title") + 1]
    assert dhn.parse_marker(body) == dhn.HoldState("v1.0.2", frozenset({"V3__drop.sql"}))
    assert "gh workflow run deploy-vps.yml -f tag=v1.0.2" in body


def test_a_later_release_re_flagging_the_same_file_updates_silently(gh):
    fake = gh(issues=[_issue(7, "v1.0.2", ["V3__drop.sql"])])
    result = dhn.upsert_hold_issue(
        REPO, target_tag="v1.0.3", new_files=["m/V3__drop.sql"], section="hold",
        section_markdown="again", create_when_unreadable=True,
    )
    assert result == dhn.UpsertResult("updated", 7, False)
    args, body = fake.call("issue edit")
    assert args[:4] == ["gh", "issue", "edit", "7"]
    assert "v1.0.3" in args[args.index("--title") + 1]
    assert dhn.parse_marker(body).target_tag == "v1.0.3"
    assert "issue create" not in fake.verbs()


def test_a_new_destructive_file_joining_the_hold_notifies_again(gh):
    gh(issues=[_issue(7, "v1.0.2", ["V3__drop.sql"])])
    result = dhn.upsert_hold_issue(
        REPO, target_tag="v1.0.3", new_files=["m/V4__rename.sql"], section="hold",
        section_markdown="more", create_when_unreadable=True,
    )
    assert result.notify


def test_the_target_never_moves_backwards_and_an_unchanged_target_keeps_the_title(gh):
    fake = gh(issues=[_issue(7, "v1.0.5")])
    dhn.upsert_hold_issue(
        REPO, target_tag="v1.0.3", new_files=[], section="drift", section_markdown="x",
        create_when_unreadable=False,
    )
    args, body = fake.call("issue edit")
    assert "--title" not in args
    assert dhn.parse_marker(body).target_tag == "v1.0.5"


def test_only_marked_issues_count_and_the_oldest_wins(gh):
    fake = gh(issues=[{"number": 3, "body": "unrelated needs-human issue"}, _issue(9, "v1"), _issue(8, "v1"), "junk"])
    assert dhn.find_hold_issue(REPO)[0] == 8
    assert fake.verbs() == ["issue list"]


def test_an_unreadable_issue_list_creates_for_the_gate_but_not_for_drift(gh, capsys):
    fake = gh(issues=None)
    drift = dhn.upsert_hold_issue(
        REPO, target_tag="v1", new_files=[], section="drift", section_markdown="x", create_when_unreadable=False
    )
    assert drift == dhn.UpsertResult("failed", None, False)
    assert "issue create" not in fake.verbs()
    gate = dhn.upsert_hold_issue(
        REPO, target_tag="v1", new_files=[], section="hold", section_markdown="x", create_when_unreadable=True
    )
    assert gate.action == "created"


def test_create_and_edit_failures_are_reported_not_raised(gh, capsys):
    gh(issues=[], fail={"issue create"})
    assert dhn.upsert_hold_issue(
        REPO, target_tag="v1", new_files=[], section="hold", section_markdown="x", create_when_unreadable=True
    ) == dhn.UpsertResult("failed", None, False)
    gh(issues=[_issue(7, "v1")], fail={"issue edit"})
    assert dhn.upsert_hold_issue(
        REPO, target_tag="v2", new_files=["a"], section="hold", section_markdown="x", create_when_unreadable=True
    ) == dhn.UpsertResult("failed", 7, False)
    assert "hold issue not created" in capsys.readouterr().out


def test_close_if_deployed(gh):
    fake = gh(issues=[_issue(7, "v1.0.3")])
    assert dhn.close_if_deployed(REPO, "v1.0.2") is None
    assert dhn.close_if_deployed(REPO, "v1.0.3") == 7
    args, _ = fake.call("issue close")
    assert args[3] == "7" and "Closing automatically" in args[-1]
    gh(issues=[_issue(7, "v1.0.3")], fail={"issue close"})
    assert dhn.close_if_deployed(REPO, "v1.0.4") is None
    gh(issues=[])
    assert dhn.close_if_deployed(REPO, "v9") is None


# ---------------------------------------------------------------- email


class _Resp:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _mail_env(monkeypatch):
    monkeypatch.setenv("SENDGRID_API_KEY", "SG.key")
    monkeypatch.setenv("SENDGRID_FROM_EMAIL", "alerts@example.com")
    monkeypatch.setenv("DEPLOY_ALERT_EMAIL", "owner@example.com")


def test_email_is_skipped_and_named_when_unconfigured(monkeypatch, capsys):
    for name in ("SENDGRID_API_KEY", "SENDGRID_FROM_EMAIL", "DEPLOY_ALERT_EMAIL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(dhn.urllib.request, "urlopen", lambda *a, **k: pytest.fail("sent without config"))
    assert dhn.send_owner_email("s", "t") is False
    assert "SENDGRID_API_KEY, SENDGRID_FROM_EMAIL, DEPLOY_ALERT_EMAIL" in capsys.readouterr().out


def test_email_posts_to_sendgrid_with_the_configured_recipient(monkeypatch):
    _mail_env(monkeypatch)
    seen = {}

    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["auth"] = request.get_header("Authorization")
        seen["payload"] = json.loads(request.data)
        return _Resp(202)

    monkeypatch.setattr(dhn.urllib.request, "urlopen", fake_urlopen)
    assert dhn.send_owner_email("subj", "body") is True
    assert seen["url"] == dhn.SENDGRID_URL
    assert seen["auth"] == "Bearer SG.key"
    assert seen["payload"]["personalizations"][0]["to"] == [{"email": "owner@example.com"}]
    assert seen["payload"]["from"]["email"] == "alerts@example.com"
    assert seen["payload"]["content"][0]["value"] == "body"


def test_email_failure_is_reported_not_raised(monkeypatch, capsys):
    _mail_env(monkeypatch)

    def refuse(request, timeout):
        raise urllib.error.URLError("403 forbidden")

    monkeypatch.setattr(dhn.urllib.request, "urlopen", refuse)
    assert dhn.send_owner_email("s", "t") is False
    monkeypatch.setattr(dhn.urllib.request, "urlopen", lambda r, timeout: _Resp(500))
    assert dhn.send_owner_email("s", "t") is False
    assert "hold email not sent" in capsys.readouterr().out


def test_notify_only_on_a_state_change(mails):
    assert dhn.notify(REPO, dhn.UpsertResult("updated", 7, False), "s", "t") is False
    assert dhn.notify(REPO, dhn.UpsertResult("created", 42, True), "s", "summary") is True
    assert dhn.notify(REPO, dhn.UpsertResult("created", None, True), "s", "summary") is True
    assert mails[0][1].endswith(f"https://github.com/{REPO}/issues/42\n")
    assert mails[1][1].endswith(f"https://github.com/{REPO}/issues\n")


def test_report_gate_hold_files_and_emails_the_first_time_only(gh, mails):
    fake = gh(issues=[])
    held = [("compose/local/database/migrations/V3__drop.sql", ["DROP clause"])]
    dhn.report_gate_hold(REPO, "v1.0.2", "v1.0.1", held)
    _, body = fake.call("issue create")
    assert "DROP clause" in body and "Production runs `v1.0.1`" in body
    (subject, text), = mails
    assert "v1.0.2" in subject and "V3__drop.sql" in text
    assert "gh workflow run deploy-vps.yml -f tag=v1.0.2" in text

    gh(issues=[{"number": 42, "body": body}])
    dhn.report_gate_hold(REPO, "v1.0.3", "v1.0.1", held)
    assert len(mails) == 1


# ---------------------------------------------------------------- drift check


def test_drift_is_blind_when_app_info_is_unreadable(gh, monkeypatch, capsys):
    fake = gh(issues=[])
    monkeypatch.setattr(rrc, "fetch_deployed_version", lambda url: None)
    assert dhn.run_drift_check(REPO, "https://x", 3, NOW) == 0
    assert fake.calls == []
    assert "deploy drift unknown" in capsys.readouterr().out


def test_drift_closes_a_resolved_hold_and_passes_when_current(gh, monkeypatch, mails, capsys):
    fake = gh(issues=[_issue(7, "v1.0.3")], releases=[{"tagName": "v1.0.3", "createdAt": "2026-09-24T00:00:00Z"}])
    monkeypatch.setattr(rrc, "fetch_deployed_version", lambda url: "1.0.3")
    assert dhn.run_drift_check(REPO, "https://x", 3, NOW) == 0
    assert "issue close" in fake.verbs()
    assert "on the latest release" in capsys.readouterr().out
    assert mails == []


def test_drift_within_the_threshold_files_nothing(gh, monkeypatch, mails):
    fake = gh(issues=[], releases=[{"tagName": "v1.0.3", "createdAt": "2026-09-25T10:00:00Z"}])
    monkeypatch.setattr(rrc, "fetch_deployed_version", lambda url: "1.0.2")
    dhn.run_drift_check(REPO, "https://x", 3, NOW)
    assert "issue create" not in fake.verbs() and mails == []


def test_drift_past_the_threshold_files_the_issue_and_emails_once(gh, monkeypatch, mails):
    releases = [
        {"tagName": "v0.178.1", "createdAt": "2026-09-25T06:00:00Z"},
        {"tagName": "v0.177.0", "createdAt": "2026-09-24T12:00:00Z"},
        {"tagName": "v0.176.5", "createdAt": "2026-09-24T00:00:00Z"},
    ]
    fake = gh(issues=[], releases=releases)
    monkeypatch.setattr(rrc, "fetch_deployed_version", lambda url: "0.176.5")
    dhn.run_drift_check(REPO, "https://x", 3, NOW)
    args, body = fake.call("issue create")
    assert dhn.parse_marker(body).target_tag == "v0.178.1"
    assert "`v0.177.0`, was published **24.0h** ago" in body
    assert "--exclude-drafts" in fake.call("release list")[0]
    (subject, _), = mails
    assert "24h behind" in subject

    gh(issues=[{"number": 42, "body": body}], releases=releases)
    dhn.run_drift_check(REPO, "https://x", 3, NOW)
    assert len(mails) == 1


def test_drift_with_an_unreadable_release_list_does_nothing(gh, monkeypatch):
    fake = gh(issues=[], releases=None)
    monkeypatch.setattr(rrc, "fetch_deployed_version", lambda url: "1.0.0")
    assert dhn.run_drift_check(REPO, "https://x", 3, NOW) == 0
    assert "issue create" not in fake.verbs()


def test_max_hours_parsing(capsys):
    assert dhn._max_hours("") == 3
    assert dhn._max_hours(None) == 3
    assert dhn._max_hours("6.5") == 6.5
    assert dhn._max_hours("0") == 3
    assert dhn._max_hours("soon") == 3
    assert "bad DEPLOY_DRIFT_MAX_HOURS" in capsys.readouterr().out


def test_main_runs_the_drift_check_with_env_defaults(monkeypatch):
    seen = {}
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://app")
    monkeypatch.setenv("DEPLOY_DRIFT_MAX_HOURS", "5")
    monkeypatch.setenv("GITHUB_REPOSITORY", "a/b")
    monkeypatch.setattr(dhn, "run_drift_check", lambda repo, url, hours: seen.update(r=repo, u=url, h=hours) or 0)
    assert dhn.main(["deploy_hold_notify.py", "drift"]) == 0
    assert seen == {"r": "a/b", "u": "https://app", "h": 5.0}
