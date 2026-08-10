"""Tests for the daemon's event-draining, including defects found by LIVE webhook traffic.

Everything here is about matching a delivery to the queue item it refers to. That sounds trivial
and is not: GitHub's payloads identify their subject three different ways depending on the event,
and each mismatch fails SILENTLY — the event is consumed, no item is marked dirty, and the wait
state it should have ended expires on its TTL instead. The symptom is not an error; it is the
pipeline quietly reverting to polling.
"""

from __future__ import annotations

import sys
from pathlib import Path

_V2 = Path(__file__).resolve().parents[2] / "scripts" / "agent-pipeline" / "v2"
sys.path.insert(0, str(_V2))

from lemd import daemon, db  # noqa: E402
from lemd.config import load  # noqa: E402


def _cfg(tmp_path):
    """A config rooted in a temp dir, with the daemon kept in shadow mode."""
    (tmp_path / "config.env").write_text(
        f"LEMD_DB={tmp_path}/queue.db\nLEMD_SHADOW=1\n"
        "SLUG=christopherqueenconsulting/linkedin_engagement_manager\n"
    )
    return load(tmp_path)


def test_numberless_check_suite_is_matched_by_head_sha(tmp_path):
    """Live traffic exposed this: merge-queue check_suite deliveries carry NO pull_requests array.

    The suite belongs to a `gh-readonly-queue/...` ref rather than the PR branch, so keying only on
    number dropped every one of them — and check_suite is the CI-green edge, so the wait state it
    should end would instead have expired on its TTL, quietly turning the event architecture back
    into polling for the lane that matters most.
    """
    dm = daemon.Daemon(_cfg(tmp_path))
    db.upsert_item(dm.conn, kind="pr", number=1234, state=db.STATE_WAIT_CI,
                   branch="feature/x", head_sha="deadbeefcafe")
    db.record_event(dm.conn, delivery_id="cs-1", event="check_suite", action="completed",
                    number=None, head_sha="deadbeefcafe", payload="{}")
    assert dm.drain_events() == 1
    assert db.get_item(dm.conn, "pr", 1234)["dirty"] == 1


def test_head_sha_matching_ignores_a_superseded_commit(tmp_path):
    """A suite for an old head tells us nothing actionable; re-observing on it wastes a read."""
    dm = daemon.Daemon(_cfg(tmp_path))
    db.upsert_item(dm.conn, kind="pr", number=1235, state=db.STATE_WAIT_CI, head_sha="newsha")
    db.record_event(dm.conn, delivery_id="cs-2", event="check_suite", action="completed",
                    number=None, head_sha="oldsha", payload="{}")
    dm.drain_events()
    assert db.get_item(dm.conn, "pr", 1235)["dirty"] == 0


def test_head_sha_matching_skips_finished_items(tmp_path):
    """A merged PR's queue suite must not resurrect it."""
    dm = daemon.Daemon(_cfg(tmp_path))
    db.upsert_item(dm.conn, kind="pr", number=1236, state=db.STATE_MERGED, head_sha="mergedsha")
    db.record_event(dm.conn, delivery_id="cs-3", event="check_suite", action="completed",
                    number=None, head_sha="mergedsha", payload="{}")
    dm.drain_events()
    assert db.get_item(dm.conn, "pr", 1236)["dirty"] == 0


def test_numbered_events_still_match_by_number(tmp_path):
    """The head_sha path is a fallback, not a replacement."""
    dm = daemon.Daemon(_cfg(tmp_path))
    db.upsert_item(dm.conn, kind="pr", number=42, state=db.STATE_WAIT_CI, head_sha="abc")
    db.record_event(dm.conn, delivery_id="pr-1", event="pull_request", action="synchronize",
                    number=42, head_sha="abc", payload="{}")
    dm.drain_events()
    assert db.get_item(dm.conn, "pr", 42)["dirty"] == 1


def test_delivery_for_an_unknown_item_is_consumed_not_retried(tmp_path):
    """A webhook for something the queue never admitted must not accumulate forever."""
    dm = daemon.Daemon(_cfg(tmp_path))
    db.record_event(dm.conn, delivery_id="unknown-1", event="pull_request", action="opened",
                    number=99999, head_sha="zzz", payload="{}")
    dm.drain_events()
    assert db.unprocessed_events(dm.conn) == []


def test_events_are_marked_processed_even_when_nothing_matched(tmp_path):
    dm = daemon.Daemon(_cfg(tmp_path))
    db.record_event(dm.conn, delivery_id="ping-1", event="ping", action=None,
                    number=None, head_sha=None, payload="{}")
    dm.drain_events()
    assert db.unprocessed_events(dm.conn) == []
