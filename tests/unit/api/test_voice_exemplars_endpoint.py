"""POST /api/user/voice-exemplars: session-resolved, flag-gated CSV upload (issue #2627)."""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_M = "cqc_lem.api.main"
_VE = "cqc_lem.utilities.ai.voice_exemplars"
_CSV = b"Date,Link,Message\n2026-01-01,u,a long enough comment written by the user themselves\n"


def _post(api_client, *, kind="comments", data=_CSV, enabled=True, user=42, ingest=None):
    ingest = ingest or {"received": 1, "stored": 1}
    with patch(f"{_M}.get_session_user_id", return_value=user), \
         patch(f"{_VE}.flag_enabled", return_value=enabled), \
         patch(f"{_VE}.ingest_exemplars", return_value=ingest) as ing:
        resp = api_client.post("/api/user/voice-exemplars",
                               data={"session_token": "tok", "kind": kind},
                               files={"file": ("Comments.csv", data, "text/csv")})
    return resp, ing


def test_ingests_for_the_session_user(api_client):
    resp, ing = _post(api_client)
    assert resp.status_code == 200
    assert resp.json()["detail"]["stored"] == 1
    assert ing.call_args.args[:2] == (42, "comments")
    assert ing.call_args.args[2] == ["a long enough comment written by the user themselves"]


def test_unauthenticated_is_401_and_nothing_ingested(api_client):
    resp, ing = _post(api_client, user=None)
    assert resp.status_code == 401
    ing.assert_not_called()


def test_flag_off_is_404(api_client):
    resp, ing = _post(api_client, enabled=False)
    assert resp.status_code == 404
    ing.assert_not_called()


def test_bad_kind_is_400(api_client):
    resp, ing = _post(api_client, kind="posts")
    assert resp.status_code == 400
    ing.assert_not_called()


def test_file_without_a_text_column_is_400(api_client):
    resp, ing = _post(api_client, data=b"a,b\n1,2\n")
    assert resp.status_code == 400
    ing.assert_not_called()


def test_oversize_upload_is_400(api_client):
    with patch(f"{_VE}.MAX_UPLOAD_BYTES", 10):
        resp, ing = _post(api_client)
    assert resp.status_code == 400
    ing.assert_not_called()
