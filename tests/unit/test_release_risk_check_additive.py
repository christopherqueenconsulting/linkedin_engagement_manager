"""`release_risk_check.main()` with the REAL classifier: additive auto-deploys, anything else holds.

`test_release_risk_check.py` pins the classifier to "hold" so it can grade the diff-base logic on its
own; this file grades the other half end to end against a throwaway checkout in `tmp_path`.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "scripts"))

import release_risk_check as rrc  # noqa: E402

MIG_DIR = "compose/local/database/migrations"
RELEASES = json.dumps(
    [
        {"tagName": "v1.0.2", "createdAt": "2026-08-16T00:00:00Z"},
        {"tagName": "v1.0.1", "createdAt": "2026-08-15T00:00:00Z"},
    ]
)


def _checkout(tmp_path: Path, new: dict[str, str]) -> Path:
    migrations = tmp_path / MIG_DIR
    migrations.mkdir(parents=True)
    (migrations / "V1__base.sql").write_text(
        "CREATE TABLE logs (id INT PRIMARY KEY, action_type ENUM('comment','dm') NOT NULL);", encoding="utf-8"
    )
    for name, sql in new.items():
        (migrations / name).write_text(sql, encoding="utf-8")
    return tmp_path


def _router(new_names: list[str]):
    compare = json.dumps(
        {"files": [{"filename": f"{MIG_DIR}/{n}", "status": "added"} for n in new_names], "commits": []}
    )
    pulls = json.dumps([{"number": 7, "head": {"ref": "release-please--branches--main"}}])

    def route(args, **kw):
        joined = " ".join(args)
        if "release list" in joined:
            return subprocess.CompletedProcess(args, 0, RELEASES, "")
        if "compare/" in joined:
            return subprocess.CompletedProcess(args, 0, compare, "")
        if "/pulls" in joined:
            return subprocess.CompletedProcess(args, 0, pulls, "")
        raise AssertionError(f"unexpected gh call: {args}")

    return route


def _run(monkeypatch, tmp_path, new: dict[str, str], *extra: str):
    root = _checkout(tmp_path, new)
    out = tmp_path / "gh_output"
    monkeypatch.setenv("GH_TOKEN", "x")
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setattr(rrc, "_run_gh", _router(list(new)))
    monkeypatch.setattr(rrc, "fetch_deployed_version", lambda url: "1.0.1")
    holds, comments = [], []
    monkeypatch.setattr(rrc, "report_hold", lambda *a: holds.append(a))
    monkeypatch.setattr(rrc, "post_decision_comment", lambda repo, pr, body: comments.append(body) or True)
    argv = ["release_risk_check.py", "--tag", "v1.0.2", "--app-url", "https://x", "--repo-root", str(root)]
    rc = rrc.main([*argv, *extra])
    assert rc == 0
    return out.read_text(), holds, comments


def test_an_additive_only_release_auto_deploys_with_a_notice(monkeypatch, tmp_path, capsys):
    new = {
        "V2__widen.sql": "ALTER TABLE logs MODIFY COLUMN action_type ENUM('comment','dm','invite') NOT NULL;",
        "V3__col.sql": (
            "ALTER TABLE logs ADD COLUMN approved_by VARCHAR(64) NULL, ADD COLUMN approved_at DATETIME NULL;"
        ),
    }
    outputs, holds, comments = _run(monkeypatch, tmp_path, new)
    assert "flagged=false" in outputs
    assert holds == [] and comments == []
    log = capsys.readouterr().out
    assert "AUTO-DEPLOY" in log
    assert "::notice title=Additive migration auto-deployed::" in log
    assert "only appends ['invite']" in log


def test_one_destructive_migration_holds_the_whole_release_and_files_the_issue(monkeypatch, tmp_path, capsys):
    new = {
        "V2__widen.sql": "ALTER TABLE logs MODIFY COLUMN action_type ENUM('comment','dm','invite') NOT NULL;",
        "V3__drop.sql": "ALTER TABLE logs DROP COLUMN action_type;",
    }
    outputs, holds, comments = _run(monkeypatch, tmp_path, new)
    assert "flagged=true" in outputs
    (repo, tag, deployed, held), = holds
    assert (tag, deployed) == ("v1.0.2", "v1.0.1")
    assert [path for path, _ in held] == [f"{MIG_DIR}/V3__drop.sql"]
    assert "DROP clause" in held[0][1][0]
    assert "Not provably additive" in comments[0]
    assert "DROP clause" in comments[0]
    assert "HELD compose/local/database/migrations/V3__drop.sql" in capsys.readouterr().out


def test_a_dry_run_holds_without_filing_or_commenting(monkeypatch, tmp_path):
    outputs, holds, comments = _run(monkeypatch, tmp_path, {"V2__u.sql": "UPDATE logs SET id = id;"}, "--no-comment")
    assert "flagged=true" in outputs
    assert holds == [] and comments == []


def test_a_migration_missing_from_the_checkout_holds(monkeypatch, tmp_path):
    root = _checkout(tmp_path, {})
    out = tmp_path / "gh_output"
    monkeypatch.setenv("GH_TOKEN", "x")
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setattr(rrc, "_run_gh", _router(["V9__elsewhere.sql"]))
    monkeypatch.setattr(rrc, "fetch_deployed_version", lambda url: "1.0.1")
    rc = rrc.main(["release_risk_check.py", "--tag", "v1.0.2", "--repo-root", str(root), "--no-comment"])
    assert rc == 0
    assert "flagged=true" in out.read_text()


def test_report_hold_never_raises(monkeypatch, capsys):
    import deploy_hold_notify

    def boom(*a):
        raise RuntimeError("gh exploded")

    monkeypatch.setattr(deploy_hold_notify, "report_gate_hold", boom)
    rrc.report_hold("o/r", "v1", None, [])
    assert "hold issue/email failed::RuntimeError: gh exploded" in capsys.readouterr().out


def test_report_hold_delegates_to_the_notifier(monkeypatch):
    import deploy_hold_notify

    seen = []
    monkeypatch.setattr(deploy_hold_notify, "report_gate_hold", lambda *a: seen.append(a))
    rrc.report_hold("o/r", "v1", "v0", [("p", ["why"])])
    assert seen == [("o/r", "v1", "v0", [("p", ["why"])])]
