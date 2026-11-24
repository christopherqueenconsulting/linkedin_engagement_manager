"""Call-ingest tasks and upload/review API (issue #2629, Phase 1b)."""

import asyncio
import io
import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException, UploadFile

from cqc_lem.api.routers import user as user_router
from cqc_lem.app import run_call_ingest as rci
from cqc_lem.utilities.ai import call_ingest as ci

pytestmark = pytest.mark.unit

_M = "cqc_lem.app.run_call_ingest"
OTTER = "Chris  0:01\nWe cut churn from 8% to 5% last quarter.\n\nPat Lee  0:20\nNice, call me at 555-123-4567.\n"


def _reply(items):
    r = MagicMock()
    r.choices = [MagicMock()]
    r.choices[0].message.content = json.dumps({"items": items})
    return r


class TestPrepareUpload:
    def test_only_own_redacted_turns_survive(self):
        out = ci.prepare_upload(OTTER, "otter", "Chris")
        assert out["state"] == ci.OWN_KNOWN
        assert "churn" in out["text"] and "555" not in out["text"] and "Pat" not in out["text"]

    def test_unknown_speaker_is_asked_not_guessed(self):
        out = ci.prepare_upload(OTTER, "otter", None)
        assert out["state"] == ci.OWN_UNKNOWN and out["speakers"] == ["Chris", "Pat Lee"]

    def test_swapped_speaker_yields_the_other_persons_words_only_when_told(self):
        out = ci.prepare_upload(OTTER, "otter", "Pat Lee")
        assert "churn" not in out["text"]

    def test_wispr_is_always_known(self):
        out = ci.prepare_upload("I shipped it.\n\nThen I slept.", "wispr")
        assert out["state"] == ci.OWN_KNOWN and out["turn_count"] == 2

    def test_parse_extraction_tolerates_garbage(self):
        assert ci.parse_extraction("nope") == []
        assert ci.parse_extraction('{"items": "x"}') == []
        got = ci.parse_extraction('{"items": [{"body": "b", "confidence": 7}, 3]}')
        assert got[0]["confidence"] == 1.0 and len(got) == 1


class TestIngestTask:
    def test_flag_off_does_nothing(self, monkeypatch):
        monkeypatch.delenv("CALL_INGEST_ENABLED", raising=False)
        with patch(f"{_M}.get_call_transcript") as get:
            assert rci.ingest_call_transcript.run(1, 2) == "disabled"
        get.assert_not_called()

    def test_invented_fact_is_dropped_real_one_stored_pending(self, monkeypatch):
        monkeypatch.setenv("CALL_INGEST_ENABLED", "true")
        items = [{"kind": "number", "title": "Churn", "body": "Cut churn 8% to 5%",
                  "source_quote": "We cut churn from 8% to 5% last quarter", "confidence": 0.9},
                 {"kind": "client_win", "title": "Big", "body": "Won Acme",
                  "source_quote": "We signed Acme for a million dollars", "confidence": 0.95}]
        with patch(f"{_M}.get_call_transcript",
                   return_value="We cut churn from 8% to 5% last quarter."), \
             patch(f"{_M}.client") as cl, \
             patch(f"{_M}.insert_extract_candidates", return_value=1) as ins, \
             patch(f"{_M}.set_call_ingest_status") as st:
            cl.chat.completions.create.return_value = _reply(items)
            out = rci.ingest_call_transcript.run(1, 2)
        assert out == "1 candidates, 1 dropped"
        assert [c["title"] for c in ins.call_args.args[2]] == ["Churn"]
        st.assert_called_once_with(1, 2, "extracted")
        assert cl.chat.completions.create.call_args.kwargs["model"] == "lem-simple"

    def test_provider_fault_marks_failed(self, monkeypatch):
        monkeypatch.setenv("CALL_INGEST_ENABLED", "true")
        with patch(f"{_M}.get_call_transcript", return_value="x y z"), \
             patch(f"{_M}.client") as cl, patch(f"{_M}.set_call_ingest_status") as st:
            cl.chat.completions.create.side_effect = RuntimeError("boom")
            assert rci.ingest_call_transcript.run(1, 2) == "failed"
        st.assert_called_once_with(1, 2, "failed")

    def test_purge_beat_ignores_the_flag(self, monkeypatch):
        monkeypatch.delenv("CALL_INGEST_ENABLED", raising=False)
        with patch(f"{_M}.purge_call_transcripts", return_value=3), \
             patch(f"{_M}.list_expired_call_audio", return_value=[]):
            assert rci.purge_expired_call_transcripts.run() == "purged 3, audio 0"

    def test_purge_beat_is_scheduled(self):
        from cqc_lem.app.my_celery import app
        tasks = [v["task"] for v in app.conf.beat_schedule.values()]
        assert "cqc_lem.app.run_call_ingest.purge_expired_call_transcripts" in tasks


def _upload(name, body):
    return UploadFile(filename=name, file=io.BytesIO(body))


def _call(**kw):
    args = dict(session_token="t", source_id=4, source_type="otter", own_speaker="Chris",
                title=None, file=_upload("c.txt", OTTER.encode()))
    args.update(kw)
    return asyncio.run(user_router.upload_call_transcript_endpoint(**args))


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv("CALL_INGEST_ENABLED", "true")
    monkeypatch.setattr(user_router._main, "get_session_user_id", lambda t: 7)


class TestUploadApi:
    def test_flag_off_is_404_and_writes_nothing(self, monkeypatch):
        monkeypatch.delenv("CALL_INGEST_ENABLED", raising=False)
        with patch.object(user_router._call_db, "insert_call_ingest") as ins:
            with pytest.raises(HTTPException) as e:
                _call()
        assert e.value.status_code == 404
        ins.assert_not_called()

    def test_stores_and_queues(self, on):
        with patch.object(user_router._call_db, "is_call_source_enabled", return_value=True), \
             patch.object(user_router._call_db, "insert_call_ingest", return_value=9) as ins, \
             patch("cqc_lem.app.run_call_ingest.ingest_call_transcript") as task:
            out = _call()
        assert out.detail == {"ingest_id": 9}
        assert "Pat" not in ins.call_args.args[1]
        task.apply_async.assert_called_once_with(kwargs={"user_id": 7, "ingest_id": 9})

    def test_wrong_extension_and_disabled_source_refused(self, on):
        with pytest.raises(HTTPException) as e:
            _call(file=_upload("c.pdf", b"x"))
        assert e.value.status_code == 400
        with patch.object(user_router._call_db, "is_call_source_enabled", return_value=False):
            with pytest.raises(HTTPException) as e:
                _call()
        assert e.value.status_code == 403

    def test_unknown_speaker_is_409(self, on):
        with patch.object(user_router._call_db, "is_call_source_enabled", return_value=True), \
             patch.object(user_router._call_db, "insert_call_ingest") as ins:
            with pytest.raises(HTTPException) as e:
                _call(own_speaker=None)
        assert e.value.status_code == 409 and "Chris" in e.value.detail["speakers"]
        ins.assert_not_called()

    def test_oversize_refused(self, on, monkeypatch):
        monkeypatch.setattr(user_router, "_CALL_UPLOAD_MAX_BYTES", 5)
        with patch.object(user_router._call_db, "is_call_source_enabled", return_value=True):
            with pytest.raises(HTTPException) as e:
                _call()
        assert e.value.status_code == 400


class TestReviewApi:
    def test_source_needs_consent(self, on):
        with pytest.raises(HTTPException) as e:
            user_router.create_call_source_endpoint(
                user_router.CallSourceRequest(session_token="t", source_type="otter"))
        assert e.value.status_code == 400

    def test_accept_promotes_and_purges_reviewed(self, on):
        req = user_router.CallReviewRequest(session_token="t", candidate_id=3, decision="accept")
        with patch.object(user_router._call_db, "review_candidate", return_value=11) as rv, \
             patch.object(user_router._call_db, "purge_reviewed_call_transcripts",
                          return_value=1):
            out = user_router.review_call_candidate_endpoint(req)
        rv.assert_called_once_with(7, 3, "accepted", None)
        assert out.detail == {"story_bank_id": 11, "purged": 1}

    def test_reject_and_missing_and_bad_decision(self, on):
        mk = user_router.CallReviewRequest
        with patch.object(user_router._call_db, "review_candidate", return_value=None), \
             patch.object(user_router._call_db, "purge_reviewed_call_transcripts",
                          return_value=0):
            assert user_router.review_call_candidate_endpoint(
                mk(session_token="t", candidate_id=1, decision="reject")).status_code == 200
            with pytest.raises(HTTPException) as e:
                user_router.review_call_candidate_endpoint(
                    mk(session_token="t", candidate_id=1, decision="accept"))
            assert e.value.status_code == 404
        for bad in (mk(session_token="t", candidate_id=1, decision="x"),
                    mk(session_token="t", candidate_id=1, decision="edit")):
            with pytest.raises(HTTPException) as e:
                user_router.review_call_candidate_endpoint(bad)
            assert e.value.status_code == 400

    def test_bulk_lists_and_purge_all_work_with_flag_off(self, monkeypatch):
        monkeypatch.delenv("CALL_INGEST_ENABLED", raising=False)
        monkeypatch.setattr(user_router._main, "get_session_user_id", lambda t: 7)
        d = user_router._call_db
        with patch.object(d, "accept_candidates_above", return_value=4) as acc, \
             patch.object(d, "purge_reviewed_call_transcripts", return_value=0), \
             patch.object(d, "list_call_ingests", return_value=[]), \
             patch.object(d, "get_candidates_for_call", return_value=[1]), \
             patch.object(d, "get_pending_candidates", return_value=[2]), \
             patch.object(d, "list_call_audio_paths", return_value=[]), \
             patch.object(d, "purge_all_call_data", return_value=True) as purge:
            bulk = user_router.accept_call_candidates_bulk_endpoint(
                user_router.CallBulkAcceptRequest(session_token="t"))
            assert bulk.detail["accepted"] == 4
            acc.assert_called_once_with(7, 0.8, None)
            assert user_router.list_call_ingests_endpoint("t").detail == {"calls": []}
            assert user_router.list_call_candidates_endpoint("t", 5).detail == {"candidates": [1]}
            assert user_router.list_call_candidates_endpoint("t").detail == {"candidates": [2]}
            user_router.purge_all_call_data_endpoint(user_router.CallPurgeRequest(session_token="t"))
        purge.assert_called_once_with(7)

    def test_unauthenticated_is_401(self, monkeypatch):
        monkeypatch.setattr(user_router._main, "get_session_user_id", lambda t: None)
        with pytest.raises(HTTPException) as e:
            user_router.list_call_ingests_endpoint("bad")
        assert e.value.status_code == 401


class TestSelectorsNeverReadPending:
    def test_generation_selectors_require_accepted_active(self):
        import inspect

        from cqc_lem.platform.db.repositories import feedback
        src = inspect.getsource(feedback.get_story_bank_entries)
        assert "active=1 AND review_status='accepted'" in src
