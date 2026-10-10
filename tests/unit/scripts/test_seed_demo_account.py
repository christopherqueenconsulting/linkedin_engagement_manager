"""Pins `scripts/seed_demo_account.py` (issue #2371): the demo seed for product recordings.

Three acceptance criteria, each asserted here against a mocked database:

* it refuses unless the database host is local or a docker-compose service in this repository;
* it makes no LinkedIn (or any network) call;
* each scene is idempotent: running it twice leaves the same rows, and no other account's.

Plus the fail-closed database fingerprint (production MySQL answers on 127.0.0.1 on the VPS, so the
host check alone cannot tell them apart), the connect-specific exit, and `--teardown`.
"""

import importlib.util
import json
import os
import pathlib
import socket
import subprocess
import sys
from datetime import date, datetime, timezone
from unittest.mock import MagicMock

import mysql.connector
import pytest

from cqc_lem.platform.db.enums import PostStatus, PostType

pytestmark = pytest.mark.unit

_SCRIPT = pathlib.Path("scripts/seed_demo_account.py")
_ANCHOR = date(2026, 10, 10)  # a Saturday: next slots are Mon 12, Wed 14, Fri 16
_OTHER_USER = 99


@pytest.fixture
def tool(monkeypatch):
    """Load the script with its two process-wide env writes undone at teardown."""
    monkeypatch.setenv("LEM_TELEMETRY_MUTED", "1")
    monkeypatch.setenv("POSTHOG_FLAGS_ENABLED", "1")
    spec = importlib.util.spec_from_file_location("seed_demo_account", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    # Registered first: a dataclass resolves its own module through sys.modules while it is built.
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


class FakeDb:
    """An in-memory stand-in for the repository functions the seed calls, with another user's rows."""

    def __init__(self, other_status=PostStatus.PENDING):
        self.users = {_OTHER_USER: {"email": "someone@real-company.test"}}
        self.posts = {1: {"user_id": _OTHER_USER, "content": "a local test user's draft",
                          "status": other_status.value, "scheduled_time": None,
                          "authenticity_score": None, "gate_reason": None,
                          "rejection_reason": None, "buyer_stage": None, "content_mix": None}}
        self.prefs = {_OTHER_USER: {"tone": "theirs"}}
        self.next_post_id = 2
        self.calls: list = []

    def ensure_demo_user(self, email):
        for uid, row in self.users.items():
            if row["email"] == email:
                return uid
        self.users[7] = {"email": email}
        return 7

    def reset_demo_user_rows(self, user_id, email):
        assert self.users[user_id]["email"] == email
        doomed = [pid for pid, p in self.posts.items() if p["user_id"] == user_id]
        for pid in doomed:
            del self.posts[pid]
        had_prefs = self.prefs.pop(user_id, None) is not None
        return {"posts": len(doomed), "engagement_preferences": int(had_prefs)}

    def _add(self, user_id, content, status, when, stage, mix):
        pid = self.next_post_id
        self.next_post_id += 1
        self.posts[pid] = {"user_id": user_id, "content": content, "status": status,
                           "scheduled_time": when, "authenticity_score": None, "gate_reason": None,
                           "rejection_reason": None, "buyer_stage": stage, "content_mix": mix}
        return pid

    def insert_demo_post(self, user_id, email, content, status, when, post_type, stage, mix):
        assert self.users[user_id]["email"] == email and post_type is PostType.TEXT
        return self._add(user_id, content, status.value, when, stage, mix)

    def insert_planned_post(self, user_id, when, post_type, stage, mix):
        self._add(user_id, "TBD", PostStatus.PLANNING.value, when, stage, mix)
        return True

    def update_db_post_authenticity_score(self, post_id, score):
        self.posts[post_id]["authenticity_score"] = score
        return True

    def update_db_post_gate_reason(self, post_id, findings):
        self.posts[post_id]["gate_reason"] = json.dumps(findings) if findings else None
        return True

    def soft_delete_posts(self, post_ids, rejection_reason=None, user_id=None):
        for pid in post_ids:
            assert self.posts[pid]["user_id"] == user_id
            self.posts[pid]["status"] = PostStatus.REJECTED.value
            self.posts[pid]["rejection_reason"] = rejection_reason
        return True

    def update_engagement_preferences(self, user_id, prefs):
        self.prefs[user_id] = dict(prefs)
        return True

    def update_user_linkedin_display_name(self, user_id, name):
        self.users[user_id]["name"] = name
        return True

    def update_user_timezone(self, user_id, tz):
        self.users[user_id]["timezone"] = tz
        return True

    def mark_email_verified(self, user_id):
        return True

    def get_demo_db_fingerprint(self):
        demo = {uid for uid, u in self.users.items() if u["email"].endswith("@example.com")}
        return {"non_demo_users": len(self.users) - len(demo),
                "non_demo_posted_posts": sum(1 for p in self.posts.values()
                                             if p["status"] == PostStatus.POSTED.value
                                             and p["user_id"] not in demo)}

    def teardown_demo_user(self, email):
        uid = next((u for u, row in self.users.items() if row["email"] == email), None)
        if uid is None:
            return {"users": 0}
        self.reset_demo_user_rows(uid, email)
        del self.users[uid]
        return {"users": 1}

    def install(self, module, monkeypatch):
        for name in ("ensure_demo_user", "reset_demo_user_rows", "insert_demo_post",
                     "insert_planned_post", "update_db_post_authenticity_score",
                     "update_db_post_gate_reason", "soft_delete_posts",
                     "update_engagement_preferences", "update_user_linkedin_display_name",
                     "update_user_timezone", "mark_email_verified", "get_demo_db_fingerprint",
                     "teardown_demo_user"):
            monkeypatch.setattr(module, name, getattr(self, name))
        return self

    def snapshot(self, user_id):
        """The demo account's rows without their auto-increment ids — what "same state" means."""
        rows = sorted((p["status"], str(p["scheduled_time"]), p["content"], p["authenticity_score"],
                       p["gate_reason"], p["rejection_reason"], p["buyer_stage"], p["content_mix"])
                      for p in self.posts.values() if p["user_id"] == user_id)
        return rows, json.dumps(self.prefs.get(user_id), sort_keys=True, default=str)


@pytest.fixture
def db(tool, monkeypatch):
    return FakeDb().install(tool, monkeypatch)


@pytest.fixture
def local_target(tool, monkeypatch):
    """127.0.0.1 with no production marker and a connection that opens.

    Every NAME check passes, which is exactly the production VPS's position, so only the
    fingerprint is left to decide.
    """
    for var in ("ENVIRONMENT", "APP_ENV", "ENV", "ENCRYPTION_REQUIRED"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(tool.db_connection, "MYSQL_HOST", "127.0.0.1")
    monkeypatch.setattr(tool.db_connection, "AWS_MYSQL_SECRET_NAME", None)
    connect = MagicMock()
    monkeypatch.setattr(tool.db_connection, "get_db_connection", connect)
    return connect


def _spy_writes(tool, monkeypatch):
    spies = {name: MagicMock(side_effect=AssertionError(f"{name} ran")) for name in
             ("ensure_demo_user", "reset_demo_user_rows", "insert_demo_post", "teardown_demo_user",
              "insert_planned_post", "update_engagement_preferences")}
    for name, spy in spies.items():
        monkeypatch.setattr(tool, name, spy)
    return spies


def _run(tool, scene):
    return tool.apply_scene(tool.build_scene(scene, _ANCHOR))


def _demo_posts(db, status=None):
    return [p for p in db.posts.values()
            if p["user_id"] == 7 and (status is None or p["status"] == status)]


# --------------------------------------------------------------------------- host guard


class TestHostGuard:
    @pytest.mark.parametrize("host", ["localhost", "LOCALHOST", "127.0.0.1", "::1", "mysql",
                                      "mysql_db", "redis"])
    def test_local_and_compose_hosts_are_accepted(self, tool, host):
        assert tool.guard({}, host, None) is None

    @pytest.mark.parametrize("host", ["db.example.com", "10.0.0.5", "203.0.113.7",
                                      "mysql.internal", "lem-prod.cluster-abc.rds.amazonaws.com",
                                      "", None])
    def test_remote_or_unset_hosts_are_refused(self, tool, host):
        assert tool.guard({}, host, None)

    def test_a_host_from_an_aws_secret_is_refused(self, tool):
        assert "AWS secret" in tool.guard({}, "localhost", "prod/mysql")

    @pytest.mark.parametrize("env", [{"ENVIRONMENT": "production"}, {"APP_ENV": "prod"},
                                     {"ENV": "Production"}, {"ENCRYPTION_REQUIRED": "true"}])
    def test_a_production_marker_is_refused_even_on_localhost(self, tool, env):
        assert tool.guard(env, "localhost", None)

    @pytest.mark.parametrize("env", [{"ENVIRONMENT": "development"}, {"ENCRYPTION_REQUIRED": "false"},
                                     {"ENV": "/home/me/.shrc"}])
    def test_a_non_production_value_is_not_a_marker(self, tool, env):
        assert tool.guard(env, "localhost", None) is None

    def test_compose_names_come_from_the_repo_files(self, tool):
        names, has_placeholder = tool.compose_db_hosts()
        assert "mysql" in names and has_placeholder

    def test_compose_parser_reads_services_hostnames_and_placeholder_defaults(self, tool, tmp_path):
        (tmp_path / "docker-compose.yml").write_text(
            "services:\n"
            "  db:\n"
            "    hostname: db-host\n"
            "    image: mysql\n"
            "  cache:\n"
            "    container_name: ${CACHE_HOST:-cache_box}\n"
            "volumes:\n"
            "  not_a_service:\n", encoding="utf-8")
        names, has_placeholder = tool.compose_db_hosts(tmp_path)
        assert names == {"db", "db-host", "cache", "cache_box"}
        assert has_placeholder

    def test_without_a_placeholder_only_listed_names_pass(self, tool, tmp_path):
        (tmp_path / "docker-compose.yml").write_text("services:\n  db:\n    image: mysql\n",
                                                     encoding="utf-8")
        assert tool.guard({}, "db", None, tmp_path) is None
        assert tool.guard({}, "otherdb", None, tmp_path)

    def test_a_refused_run_writes_nothing(self, tool, db, monkeypatch, capsys):
        monkeypatch.setattr(tool.db_connection, "MYSQL_HOST", "db.example.com")
        before = db.snapshot(_OTHER_USER), dict(db.posts)
        assert tool.main(["--scene", "demo1"]) == 2
        assert (db.snapshot(_OTHER_USER), dict(db.posts)) == before
        assert 7 not in db.users
        assert "refused" in capsys.readouterr().err


# --------------------------------------------------------------------------- fingerprint + connect


class TestDatabaseFingerprint:
    """The real control: on the VPS, 127.0.0.1 is production, and every name-based check passes."""

    def test_non_demo_posted_rows_refuse_before_any_write(self, tool, local_target, monkeypatch,
                                                           capsys):
        spies = _spy_writes(tool, monkeypatch)
        monkeypatch.setattr(tool, "get_demo_db_fingerprint",
                            lambda: {"non_demo_users": 1, "non_demo_posted_posts": 5})
        assert tool.main(["--scene", "demo1"]) == 2
        for spy in spies.values():
            spy.assert_not_called()
        assert "POSTED" in capsys.readouterr().err

    def test_many_non_demo_accounts_refuse(self, tool, local_target, monkeypatch):
        spies = _spy_writes(tool, monkeypatch)
        monkeypatch.setattr(tool, "get_demo_db_fingerprint",
                            lambda: {"non_demo_users": 4, "non_demo_posted_posts": 0})
        assert tool.main(["--scene", "demo2"]) == 2
        assert not any(spy.called for spy in spies.values())

    def test_an_unreadable_fingerprint_refuses(self, tool, local_target, monkeypatch, capsys):
        spies = _spy_writes(tool, monkeypatch)

        def _boom():
            raise mysql.connector.Error("1146: Table 'posts' doesn't exist")

        monkeypatch.setattr(tool, "get_demo_db_fingerprint", _boom)
        assert tool.main(["--scene", "demo3"]) == 2
        assert not any(spy.called for spy in spies.values())
        assert "fingerprint could not be read" in capsys.readouterr().err

    def test_teardown_is_behind_the_same_fingerprint(self, tool, local_target, monkeypatch):
        spies = _spy_writes(tool, monkeypatch)
        monkeypatch.setattr(tool, "get_demo_db_fingerprint",
                            lambda: {"non_demo_users": 0, "non_demo_posted_posts": 1})
        assert tool.main(["--teardown"]) == 2
        spies["teardown_demo_user"].assert_not_called()

    def test_a_clean_or_empty_database_proceeds(self, tool, db, local_target):
        assert tool.main(["--scene", "demo1", "--anchor-date", "2026-10-10"]) == 0
        assert len(_demo_posts(db, PostStatus.PENDING.value)) == 3

    def test_a_real_posted_post_in_the_fake_db_refuses(self, tool, local_target, monkeypatch):
        fake = FakeDb(other_status=PostStatus.POSTED).install(tool, monkeypatch)
        before = dict(fake.posts)
        assert tool.main(["--scene", "demo1"]) == 2
        assert fake.posts == before and 7 not in fake.users

    @pytest.mark.parametrize("fingerprint, refused", [
        ({"non_demo_users": 0, "non_demo_posted_posts": 0}, False),
        ({"non_demo_users": 3, "non_demo_posted_posts": 0}, False),
        ({"non_demo_users": 4, "non_demo_posted_posts": 0}, True),
        ({"non_demo_users": 0, "non_demo_posted_posts": 1}, True),
    ])
    def test_the_thresholds(self, tool, fingerprint, refused):
        assert bool(tool.fingerprint_refusal(fingerprint)) is refused


class TestConnectionProbe:
    def test_connection_refused_has_its_own_message_and_exit_code(self, tool, local_target,
                                                                   monkeypatch, capsys):
        spies = _spy_writes(tool, monkeypatch)
        fingerprint = MagicMock()
        monkeypatch.setattr(tool, "get_demo_db_fingerprint", fingerprint)
        local_target.side_effect = mysql.connector.errors.InterfaceError(
            msg="Can't connect to MySQL server on '127.0.0.1:3306' (111)", errno=2003)
        assert tool.main(["--scene", "demo1"]) == 3
        err = capsys.readouterr().err
        assert "could not connect to MySQL at 127.0.0.1" in err and "errno 2003" in err
        assert "nothing was written" in err and "run it again" not in err
        fingerprint.assert_not_called()
        assert not any(spy.called for spy in spies.values())


# --------------------------------------------------------------------------- teardown


class TestTeardown:
    def test_teardown_leaves_no_demo_rows_and_touches_nothing_else(self, tool, db, local_target):
        other = db.snapshot(_OTHER_USER), db.posts[1].copy(), dict(db.users[_OTHER_USER])
        assert tool.main(["--scene", "demo2", "--anchor-date", "2026-10-10"]) == 0
        assert _demo_posts(db)
        assert tool.main(["--teardown"]) == 0
        assert 7 not in db.users and not _demo_posts(db) and 7 not in db.prefs
        assert (db.snapshot(_OTHER_USER), db.posts[1], db.users[_OTHER_USER]) == other

    def test_teardown_with_no_demo_account_is_a_clean_no_op(self, tool, db, local_target):
        assert tool.main(["--teardown"]) == 0

    def test_a_failed_teardown_exits_one(self, tool, db, local_target, monkeypatch):
        monkeypatch.setattr(tool, "teardown_demo_user", lambda email: None)
        assert tool.main(["--teardown"]) == 1

    def test_scene_and_teardown_are_exclusive(self, tool):
        with pytest.raises(SystemExit):
            tool.parse_args(["--scene", "demo1", "--teardown"])


# --------------------------------------------------------------------------- scenes


class TestScenes:
    def test_the_plan_is_mon_wed_fri_for_thirty_days(self, tool):
        slots = tool.plan_slots(_ANCHOR)
        assert len(slots) == 13
        assert {s.astimezone(tool.ZoneInfo(tool.DEMO_TIMEZONE)).weekday() for s in slots} == {0, 2, 4}
        assert slots[0] == datetime(2026, 10, 12, 13, tzinfo=timezone.utc)  # 09:00 EDT

    def test_every_scene_sets_the_account_and_cadence(self, tool, db):
        for scene in tool.SCENES:
            _run(tool, scene)
            prefs = db.prefs[7]
            assert (prefs["posts_per_week"], prefs["posting_days"]) == (3, [0, 2, 4])
            assert db.users[7] == {"email": "dana.reyes@example.com", "name": "Dana Reyes",
                                   "timezone": "America/New_York"}
            # Written posts plus skeletons fill all 13 plan slots (demo2 also has one past post).
            future = [p for p in _demo_posts(db) if p["status"] != PostStatus.POSTED.value]
            assert len(future) == 13

    def test_demo1_three_pending_drafts_for_the_coming_week(self, tool, db):
        _run(tool, "demo1")
        pending = sorted(_demo_posts(db, PostStatus.PENDING.value), key=lambda p: p["scheduled_time"])
        assert len(pending) == 3
        assert all(p["content"].startswith(o) for p, o in zip(pending, tool.DEMO1_OPENINGS))
        assert [p["scheduled_time"].day for p in pending] == [12, 14, 16]
        assert db.prefs[7]["tone"] == tool.BASE_VOICE["tone"]
        assert len(_demo_posts(db, PostStatus.PLANNING.value)) == 10

    def test_demo2_gate_holds_posted_overlap_and_rejection(self, tool, db):
        _run(tool, "demo2")
        prefs = db.prefs[7]
        assert (prefs["authenticity_score_min"], prefs["post_similarity_max_pct"]) == (70, 40)

        posted = _demo_posts(db, PostStatus.POSTED.value)
        assert len(posted) == 1
        assert posted[0]["content"].startswith("What I'd tell my first fixed-scope client")
        assert posted[0]["scheduled_time"] < datetime(2026, 10, 10, tzinfo=timezone.utc)

        pending = _demo_posts(db, PostStatus.PENDING.value)
        assert len(pending) == 2
        by_gate = {json.loads(p["gate_reason"])[0]["gate"]: p for p in pending}
        auth = json.loads(by_gate["authenticity"]["gate_reason"])[0]
        assert by_gate["authenticity"]["authenticity_score"] == 58
        assert (auth["score"], auth["threshold"], auth["demoted"]) == (58, 70, True)
        sim = json.loads(by_gate["similarity"]["gate_reason"])[0]
        assert (sim["score"], sim["threshold"], sim["demoted"]) == (0.62, 0.4, True)
        assert "62%" in sim["explanation"] and "40%" in sim["explanation"]
        assert "What I'd tell my first fixed-scope client" in sim["details"][0]

        rejected = _demo_posts(db, PostStatus.REJECTED.value)
        assert [p["rejection_reason"] for p in rejected] == ["Too salesy, no example"]

    def test_demo3_empty_voice_then_one_draft(self, tool, db):
        _run(tool, "demo3")
        prefs = db.prefs[7]
        for field in tool.VOICE_FIELDS:
            assert not prefs[field], field
        pending = _demo_posts(db, PostStatus.PENDING.value)
        assert len(pending) == 1
        assert pending[0]["content"].startswith("A client asked me")

    def test_unknown_scene_is_rejected(self, tool):
        with pytest.raises(ValueError):
            tool.build_scene("demo4", _ANCHOR)

    def test_a_failed_write_exits_nonzero(self, tool, db, local_target, monkeypatch):
        monkeypatch.setattr(tool, "update_db_post_gate_reason", lambda *a: False)
        assert tool.main(["--scene", "demo2", "--anchor-date", "2026-10-10"]) == 1


# --------------------------------------------------------------------------- idempotency


class TestIdempotency:
    @pytest.mark.parametrize("scene", ["demo1", "demo2", "demo3"])
    def test_running_a_scene_twice_gives_the_same_rows(self, tool, db, scene):
        _run(tool, scene)
        first = db.snapshot(7)
        _run(tool, scene)
        assert db.snapshot(7) == first

    def test_switching_scenes_leaves_nothing_from_the_previous_one(self, tool, db):
        _run(tool, "demo3")
        expected = db.snapshot(7)
        _run(tool, "demo2")
        _run(tool, "demo3")
        assert db.snapshot(7) == expected

    def test_another_accounts_rows_are_never_touched(self, tool, db):
        before = db.snapshot(_OTHER_USER), db.posts[1].copy()
        for scene in tool.SCENES:
            _run(tool, scene)
        assert (db.snapshot(_OTHER_USER), db.posts[1]) == before


# --------------------------------------------------------------------------- no LinkedIn


class TestNoLinkedInCalls:
    def test_a_full_run_opens_no_socket_and_no_browser(self, tool, db, local_target, monkeypatch):
        """Every socket is blocked and the browser/LinkedIn entry points raise if touched."""
        from cqc_lem.utilities import selenium_util

        def _no_network(*args, **kwargs):
            raise AssertionError("the demo seed opened a network connection")

        monkeypatch.setattr(socket.socket, "connect", _no_network)
        monkeypatch.setattr(socket, "create_connection", _no_network)
        driver = MagicMock(side_effect=AssertionError("get_docker_driver was called"))
        monkeypatch.setattr(selenium_util, "get_docker_driver", driver)

        for scene in tool.SCENES:
            assert tool.main(["--scene", scene, "--anchor-date", "2026-10-10"]) == 0
        assert tool.main(["--teardown"]) == 0
        driver.assert_not_called()

    def test_importing_the_script_loads_no_browser_or_linkedin_client(self):
        """In a fresh interpreter: the import graph holds no Selenium and no LinkedIn client."""
        probe = (
            "import importlib.util, json, sys\n"
            f"spec = importlib.util.spec_from_file_location('s', {str(_SCRIPT)!r})\n"
            "m = importlib.util.module_from_spec(spec); sys.modules['s'] = m\n"
            "spec.loader.exec_module(m)\n"
            "print(json.dumps(sorted(sys.modules)))\n"
        )
        env = {**os.environ, "PYTHONPATH": "src", "LEM_TELEMETRY_MUTED": "1"}
        out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                             env=env, timeout=120, check=True)
        loaded = json.loads(out.stdout.strip().splitlines()[-1])
        assert not [m for m in loaded if m == "selenium" or m.startswith("selenium.")]
        assert "cqc_lem.utilities.selenium_util" not in loaded
        linkedin = [m for m in loaded if m.startswith("cqc_lem.utilities.linkedin.")]
        # The profile MODEL is a pydantic type the users repository imports; it makes no call.
        assert linkedin == ["cqc_lem.utilities.linkedin.profile"]
