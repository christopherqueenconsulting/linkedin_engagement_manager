"""Every `/api` payload has to be uncacheable (issue #1527).

The app is served through a Cloudflare tunnel that caches a GET carrying no `Cache-Control` of its
own, which made writes invisible: a group-post draft was skipped, restored and given a generated
image, both PUTs answered 200, and the SPA's refetch was answered from the edge copy taken before
either write.
"""

from unittest.mock import patch

import pytest

from cqc_lem.api.spa_assets import NO_STORE_CACHE_CONTROL

pytestmark = pytest.mark.unit


@pytest.fixture
def no_session():
    """A caller the API resolves as signed OUT.

    So the group-post routes answer 401 instead of reaching a database this lane does not have.
    """
    with patch("cqc_lem.api.main.get_session_user_id", return_value=None):
        yield


def test_public_api_get_is_no_store(api_client):
    """A public read still gets the header — the cache does not know which payloads are per-user."""
    response = api_client.get("/api/app-info")

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == NO_STORE_CACHE_CONTROL
    assert response.headers["Pragma"] == "no-cache"


def test_group_post_draft_read_is_no_store(api_client, no_session):
    """The endpoint from the report.

    A 401 is enough: the header is set on the response, not on the happy path, so a refusal can
    never be cached and replayed either.
    """
    response = api_client.get("/api/user/group-post-draft", params={"session_token": "nope"})

    assert response.status_code == 401
    assert response.headers["Cache-Control"] == NO_STORE_CACHE_CONTROL


def test_api_write_is_no_store(api_client, no_session):
    """A write's own response, so a cache in front can never hold the answer to a PUT."""
    response = api_client.put("/api/user/group-post-draft",
                              json={"session_token": "nope", "status": "ready"})

    assert response.status_code == 401
    assert response.headers["Cache-Control"] == NO_STORE_CACHE_CONTROL


def test_assets_route_stays_cacheable(api_client):
    """`/api/assets` is the ONE exemption.

    Public by design (LinkedIn fetches it) and every stored name carries a random token, so the
    bytes behind a URL never change.
    """
    response = api_client.get("/api/assets", params={"file_name": "no-such-file.png"})

    assert response.status_code == 404
    assert "Cache-Control" not in response.headers


def test_non_api_route_is_left_alone(api_client):
    """The health probe is not `/api`.

    The HTML shell owns its own no-store contract, so this middleware decides for neither.
    """
    response = api_client.get("/health")

    assert response.status_code == 200
    assert "Cache-Control" not in response.headers
