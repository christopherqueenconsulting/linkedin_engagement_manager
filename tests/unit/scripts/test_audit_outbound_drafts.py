"""Pins `scripts/audit_outbound_drafts.py` (issue #1968).

The script's product is a number — how many stored outbound bodies the send-path gate would refuse
— and its one write is `--cancel`. What has to hold: the DM and invite-note surfaces are the gate's
own, the report's breakdowns derive from the same scan, `--cancel` moves ONLY rows still in a live
queue state, a run without the flag writes nothing, and an unreadable table is refused rather than
reported as clean.
"""

import importlib.util
import io
import json
import pathlib
from types import SimpleNamespace

import pytest

from cqc_lem.platform.db.enums import ConnectionRequestStatus, ScheduledDmStatus
from cqc_lem.utilities.ai import outbound_qa

pytestmark = pytest.mark.unit

_SCRIPT = pathlib.Path("scripts/audit_outbound_drafts.py")

# The 2026-09-04 incident body, verbatim from the outbound_qa module docstring.
INCIDENT_BODY = ("To assist you effectively, I need the actual message history JSON to analyze the "
                 "conversation context. Please provide the message history so I can proceed with "
                 "evaluating the new message and generating a response accordingly.")
CLEAN_DM = "Hi Jane — loved your post on retries. Would you be open to comparing notes?"
PLACEHOLDER_BODY = "Great to connect! Here is the guide: [link]"


@pytest.fixture(scope="module")
def tool():
    spec = importlib.util.spec_from_file_location("audit_outbound_drafts", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _dm(row_id, message, status="pending", source="nurture", user_id=1):
    return {"id": row_id, "user_id": user_id, "message": message, "status": status, "source": source}


def _req(row_id, message, status="pending", source="profile_viewer", user_id=1):
    return {"id": row_id, "user_id": user_id, "message": message, "status": status, "source": source}


class RecordingIO:
    """Fake `AuditIO`: fixed rows in, every write recorded."""

    def __init__(self, dms=None, requests=None, cancel_ok=True):
        self._dms = dms
        self._requests = requests
        self._cancel_ok = cancel_ok
        self.cancelled_dms = []
        self.cancelled_requests = []

    def read_scheduled_dms(self):
        return self._dms

    def read_connection_requests(self):
        return self._requests

    def cancel_scheduled_dm(self, dm_id):
        self.cancelled_dms.append(dm_id)
        return self._cancel_ok

    def cancel_connection_request(self, request_id, reason):
        self.cancelled_requests.append((request_id, reason))
        return self._cancel_ok


def _run(tool, io_, *flags):
    out = io.StringIO()
    args = tool.build_parser().parse_args(list(flags))
    rc = tool.run(args, io_, out=out)
    return rc, out.getvalue()


class TestSurfaces:
    def test_each_table_is_graded_on_the_gates_own_surface(self, tool):
        # A DM is a DM; a connection request's `message` is the invite note, and the gate has a
        # surface for exactly that — never the DM surface wearing the wrong name.
        assert tool.SURFACE_BY_TABLE[tool.TABLE_SCHEDULED_DMS] == outbound_qa.SURFACE_DM
        assert tool.SURFACE_BY_TABLE[tool.TABLE_CONNECTION_REQUESTS] == outbound_qa.SURFACE_INVITE_NOTE

    def test_the_scan_uses_the_gates_predicate_not_a_copy(self, tool):
        # Same function object, so the scan and the send-path gate cannot disagree.
        assert tool.outbound_violations is outbound_qa.outbound_violations

    def test_an_overlong_invite_note_trips_the_same_budget_as_a_dm(self, tool):
        long_note = "word " * 200
        scan = tool.scan_table(tool.TABLE_CONNECTION_REQUESTS, [_req(1, long_note)])
        assert scan.findings[0].checks == (outbound_qa.VIOLATION_OVERLONG,)


class TestScanTable:
    def test_a_clean_body_does_not_trip(self, tool):
        scan = tool.scan_table(tool.TABLE_SCHEDULED_DMS, [_dm(1, CLEAN_DM)])
        assert (scan.scanned, scan.findings) == (1, [])

    def test_the_incident_body_trips_with_the_check_names(self, tool):
        scan = tool.scan_table(tool.TABLE_SCHEDULED_DMS, [_dm(7, INCIDENT_BODY, status="approved")])
        finding = scan.findings[0]
        assert finding.row_id == 7
        assert finding.status == "approved"
        assert outbound_qa.VIOLATION_INPUT_REQUEST in finding.checks

    def test_a_missing_invite_note_is_counted_not_tripped(self, tool):
        # `connection_requests.message` is nullable and the note is OPTIONAL: a bare invite is a
        # normal invite. Grading "" would file every one of them as `empty_body`.
        scan = tool.scan_table(tool.TABLE_CONNECTION_REQUESTS,
                               [_req(1, None), _req(2, "   "), _req(3, CLEAN_DM)])
        assert (scan.scanned, scan.no_note, scan.findings) == (3, 2, [])

    def test_an_empty_dm_body_is_a_defect(self, tool):
        # `scheduled_dms.message` is NOT NULL by schema, so an empty one IS the model returning
        # nothing — graded, not skipped.
        scan = tool.scan_table(tool.TABLE_SCHEDULED_DMS, [_dm(1, "")])
        assert scan.findings[0].checks == (outbound_qa.VIOLATION_EMPTY,)

    def test_a_null_source_groups_under_its_own_label(self, tool):
        scan = tool.scan_table(tool.TABLE_SCHEDULED_DMS, [_dm(1, INCIDENT_BODY, source=None)])
        assert scan.findings[0].source == tool.NULL_LABEL

    def test_an_unknown_table_is_refused(self, tool):
        with pytest.raises(ValueError):
            tool.scan_table("posts", [])


class TestExcerpt:
    def test_never_more_than_the_first_80_chars(self, tool):
        body = "x" * 200
        excerpt = tool.sanitise_excerpt(body)
        assert len(excerpt) == tool.EXCERPT_CHARS + 1 and excerpt.endswith("…")

    def test_flattened_and_masked(self, tool):
        body = "Hi\n\nsee https://example.com/x?y=1 or mail me at jane@example.com\tthanks"
        assert tool.sanitise_excerpt(body) == "Hi see <url> or mail me at <email> thanks"

    def test_the_report_never_carries_a_full_body(self, tool):
        scan = tool.scan_table(tool.TABLE_SCHEDULED_DMS, [_dm(1, INCIDENT_BODY)])
        report = tool.build_report([scan])
        assert INCIDENT_BODY not in json.dumps(report)
        assert report["rows"][0]["excerpt"] == INCIDENT_BODY[:80].rstrip() + "…"


class TestReport:
    def _scans(self, tool):
        dms = [
            _dm(1, INCIDENT_BODY, status="pending", source="nurture"),
            _dm(2, PLACEHOLDER_BODY, status="sent", source="profile_viewer"),
            _dm(3, CLEAN_DM, status="approved", source="nurture"),
            _dm(4, INCIDENT_BODY, status="scheduled", source=None),
        ]
        reqs = [
            _req(10, PLACEHOLDER_BODY, status="approved"),
            _req(11, None, status="pending"),
            _req(12, CLEAN_DM, status="sent"),
        ]
        return [tool.scan_table(tool.TABLE_SCHEDULED_DMS, dms),
                tool.scan_table(tool.TABLE_CONNECTION_REQUESTS, reqs)]

    def test_totals_and_breakdowns(self, tool):
        report = tool.build_report(self._scans(tool))
        assert report["scanned"] == 7
        assert report["tripped"] == 4
        assert report["live"] == 3  # dm#1 pending, dm#4 scheduled, req#10 approved
        dms = report["by_table"]["scheduled_dms"]
        assert dms["surface"] == outbound_qa.SURFACE_DM
        assert (dms["scanned"], dms["tripped"], dms["live"]) == (4, 3, 2)
        assert dms["by_status"] == {"pending": 1, "scheduled": 1, "sent": 1}
        assert dms["by_source"] == {tool.NULL_LABEL: 1, "nurture": 1, "profile_viewer": 1}
        assert dms["by_check"][outbound_qa.VIOLATION_INPUT_REQUEST] == 2
        assert dms["by_check"][outbound_qa.VIOLATION_PLACEHOLDER] == 1
        reqs = report["by_table"]["connection_requests"]
        assert reqs["surface"] == outbound_qa.SURFACE_INVITE_NOTE
        assert (reqs["scanned"], reqs["no_note"], reqs["tripped"]) == (3, 1, 1)
        assert reqs["by_status"] == {"approved": 1}
        assert report["by_check"][outbound_qa.VIOLATION_PLACEHOLDER] == 2

    def test_rows_carry_coordinates_and_liveness(self, tool):
        report = tool.build_report(self._scans(tool))
        ids = [(r["table"], r["id"], r["live"]) for r in report["rows"]]
        assert ids == [("scheduled_dms", 1, True), ("scheduled_dms", 2, False),
                       ("scheduled_dms", 4, True), ("connection_requests", 10, True)]

    def test_an_empty_scan_is_a_well_formed_zero(self, tool):
        report = tool.build_report([tool.scan_table(tool.TABLE_SCHEDULED_DMS, []),
                                    tool.scan_table(tool.TABLE_CONNECTION_REQUESTS, [])])
        assert (report["scanned"], report["tripped"], report["rows"]) == (0, 0, [])

    def test_text_rendering_names_every_tripped_row(self, tool):
        report = tool.build_report(self._scans(tool))
        text = tool.render_text(report, tool.plan_cancellations(self._scans(tool)), None)
        assert "scanned: 7   tripped: 4" in text
        assert "scheduled_dms#4" in text and "connection_requests#10" in text
        assert "3 row(s) would be cancelled by --cancel; nothing was written." in text
        assert INCIDENT_BODY not in text


class TestPlanCancellations:
    @pytest.mark.parametrize("status", ["pending", "approved", "scheduled"])
    def test_a_live_dm_is_planned(self, tool, status):
        scan = tool.scan_table(tool.TABLE_SCHEDULED_DMS, [_dm(1, INCIDENT_BODY, status=status)])
        assert [f.row_id for f in tool.plan_cancellations([scan])] == [1]

    @pytest.mark.parametrize("status", ["sent", "failed", "canceled"])
    def test_a_finished_dm_is_never_planned(self, tool, status):
        scan = tool.scan_table(tool.TABLE_SCHEDULED_DMS, [_dm(1, INCIDENT_BODY, status=status)])
        assert tool.plan_cancellations([scan]) == []

    @pytest.mark.parametrize("status", ["pending", "approved"])
    def test_a_live_connection_request_is_planned(self, tool, status):
        scan = tool.scan_table(tool.TABLE_CONNECTION_REQUESTS,
                               [_req(1, PLACEHOLDER_BODY, status=status)])
        assert [f.row_id for f in tool.plan_cancellations([scan])] == [1]

    @pytest.mark.parametrize("status", ["sending", "sent", "failed", "canceled"])
    def test_an_in_flight_or_finished_request_is_never_planned(self, tool, status):
        # `sending` is the one that differs from a DM's `scheduled`: the dispatch is in flight and
        # the task, not an audit, writes its outcome.
        scan = tool.scan_table(tool.TABLE_CONNECTION_REQUESTS,
                               [_req(1, PLACEHOLDER_BODY, status=status)])
        assert tool.plan_cancellations([scan]) == []

    def test_a_clean_live_row_is_never_planned(self, tool):
        scan = tool.scan_table(tool.TABLE_SCHEDULED_DMS, [_dm(1, CLEAN_DM, status="pending")])
        assert tool.plan_cancellations([scan]) == []


class TestRun:
    def _io(self, **kwargs):
        dms = [_dm(1, INCIDENT_BODY, status="pending"), _dm(2, INCIDENT_BODY, status="sent"),
               _dm(3, CLEAN_DM, status="pending")]
        reqs = [_req(10, PLACEHOLDER_BODY, status="approved"),
                _req(11, PLACEHOLDER_BODY, status="sending")]
        return RecordingIO(dms=dms, requests=reqs, **kwargs)

    def test_read_only_by_default(self, tool):
        io_ = self._io()
        rc, text = _run(tool, io_)
        assert rc == tool.EXIT_OK
        assert io_.cancelled_dms == [] and io_.cancelled_requests == []
        assert "nothing was written" in text

    def test_cancel_moves_only_live_tripped_rows(self, tool):
        io_ = self._io()
        rc, text = _run(tool, io_, "--cancel")
        assert rc == tool.EXIT_OK
        assert io_.cancelled_dms == [1]  # not #2 (sent), not #3 (clean)
        assert [rid for rid, _ in io_.cancelled_requests] == [10]  # not #11 (sending)
        reason = io_.cancelled_requests[0][1]
        assert reason.startswith(tool.CANCEL_REASON_PREFIX)
        assert outbound_qa.VIOLATION_PLACEHOLDER in reason
        assert "--cancel: 2/2 row(s) moved to canceled." in text

    def test_a_refused_cancel_is_reported_and_fails_the_run(self, tool):
        io_ = self._io(cancel_ok=False)
        rc, text = _run(tool, io_, "--cancel")
        assert rc == tool.EXIT_CANCEL_FAILED
        assert "FAILED to cancel scheduled_dms#1" in text
        assert "FAILED to cancel connection_requests#10" in text

    @pytest.mark.parametrize("dms,reqs", [(None, []), ([], None), (None, None)])
    def test_an_unreadable_table_is_refused_and_never_written(self, tool, dms, reqs, capsys):
        io_ = RecordingIO(dms=dms, requests=reqs)
        rc, text = _run(tool, io_, "--cancel")
        assert rc == tool.EXIT_UNREADABLE
        assert text == ""  # no report, so no "0 tripped" to misread
        assert io_.cancelled_dms == [] and io_.cancelled_requests == []
        assert "refusing to report" in capsys.readouterr().err

    def test_json_output(self, tool):
        io_ = self._io()
        rc, text = _run(tool, io_, "--json")
        payload = json.loads(text)
        assert rc == tool.EXIT_OK
        # Four trip (#1, #2, #10, #11); only the two in a live state are planned.
        assert payload["report"]["tripped"] == 4
        assert payload["cancel"] == {
            "requested": False,
            "planned": [{"table": "scheduled_dms", "id": 1}, {"table": "connection_requests", "id": 10}],
            "results": None,
        }

    def test_json_output_with_cancel_carries_results(self, tool):
        io_ = self._io()
        _, text = _run(tool, io_, "--json", "--cancel")
        payload = json.loads(text)
        assert payload["cancel"]["requested"] is True
        assert payload["cancel"]["results"] == [
            {"table": "scheduled_dms", "id": 1, "ok": True},
            {"table": "connection_requests", "id": 10, "ok": True},
        ]

    def test_main_binds_the_injected_io(self, tool):
        io_ = self._io()
        assert tool.main(["--json"], io=io_) == tool.EXIT_OK
        assert io_.cancelled_dms == []

    def test_help_exits_zero(self, tool):
        with pytest.raises(SystemExit) as exc:
            tool.main(["--help"])
        assert exc.value.code == 0


class TestProductionIO:
    def test_writers_use_the_existing_updaters_with_the_proper_enum(self, tool):
        calls = []
        facade = SimpleNamespace(
            list_scheduled_dms_for_audit=lambda: [],
            list_connection_requests_for_audit=lambda: [],
            update_scheduled_dm_status=lambda dm_id, status: calls.append(("dm", dm_id, status)) or True,
            update_connection_request_status=lambda rid, status, failure_reason=None: calls.append(
                ("req", rid, status, failure_reason)) or True,
        )
        io_ = tool.production_io(facade)
        assert io_.cancel_scheduled_dm(5) is True
        assert io_.cancel_connection_request(6, "outbound_qa audit (#1968): unfilled_placeholder") is True
        assert calls == [
            ("dm", 5, ScheduledDmStatus.CANCELED),
            ("req", 6, ConnectionRequestStatus.CANCELED, "outbound_qa audit (#1968): unfilled_placeholder"),
        ]

    def test_readers_are_the_facades_audit_readers(self, tool):
        facade = SimpleNamespace(
            list_scheduled_dms_for_audit=lambda: "dms",
            list_connection_requests_for_audit=lambda: "reqs",
            update_scheduled_dm_status=None, update_connection_request_status=None,
        )
        io_ = tool.production_io(facade)
        assert (io_.read_scheduled_dms(), io_.read_connection_requests()) == ("dms", "reqs")

    def test_the_default_facade_exposes_both_readers(self, tool):
        # The facade is the stable import name (#1154): the readers must be re-exported there.
        from cqc_lem.utilities import db
        assert "list_scheduled_dms_for_audit" in db.__all__
        assert "list_connection_requests_for_audit" in db.__all__
        io_ = tool.production_io()
        assert io_.read_scheduled_dms is db.list_scheduled_dms_for_audit
        assert io_.read_connection_requests is db.list_connection_requests_for_audit
