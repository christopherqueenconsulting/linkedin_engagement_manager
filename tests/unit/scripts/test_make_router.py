"""Pins the route selection in scripts/restructure/make_router.py (issue #1182).

The script decides which routes leave `api/main.py` for a per-area router. Getting that set wrong
does not fail a test — it changes a live URL. `startswith(f"/{prefix}")` reads correctly and is
wrong: with prefix `user` it also matches `/user_id/`, a different route, which would then be
re-served as `/api/user/user_id/` under the new `APIRouter(prefix="/api/user")` while the real one
404s. It reported 80 routes for a slice that has 79.

Goes away with the script once the last router has moved.
"""

import importlib.util
import pathlib

import pytest

pytestmark = pytest.mark.unit

_SCRIPT = pathlib.Path("scripts/restructure/make_router.py")


@pytest.fixture(scope="module")
def tool():
    spec = importlib.util.spec_from_file_location("make_router", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestPrefixIsAPathSegment:
    def test_a_sibling_route_sharing_the_first_characters_is_not_included(self, tool):
        """The actual bug: `/user_id/` is not under `/user`."""
        assert tool._under("/user_id/", "user") is False
        assert tool._under("/user_id", "user") is False

    def test_the_bare_prefix_and_its_children_are_included(self, tool):
        assert tool._under("/user", "user") is True
        assert tool._under("/user/posts", "user") is True
        assert tool._under("/user/linkedin-cookie", "user") is True

    def test_an_unrelated_route_is_not_included(self, tool):
        assert tool._under("/admin/automation-pause", "user") is False
        assert tool._under("/billing/webhook", "user") is False

    @pytest.mark.parametrize("prefix,sibling", [
        ("auth", "/authority"),
        ("post", "/posts"),
        ("avatar", "/avatars"),
    ])
    def test_any_prefix_that_is_another_routes_stem_stays_separate(self, tool, prefix, sibling):
        """Not hypothetical — `post`/`posts` and `avatar`/`avatars` are both live shapes here."""
        assert tool._under(sibling, prefix) is False
