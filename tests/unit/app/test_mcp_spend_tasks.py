"""The Celery side of the MCP spend tools (W2b, issue #2689), and its two SQL-guarded writes.

`generate_agent_post*` run the SAME generation paths the studio uses and must leave every post
PENDING: text through `regenerate_post`, an image through `generate_image_for_post`, a video through
`_generate_video_src` + `_store_video_asset`. These tests patch those paths at their boundaries, so
no LLM, render or database is reached.
"""

from datetime import datetime, timezone
from typing import Any, Iterator, Optional
from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.app import run_content_plan as rcp
from cqc_lem.platform.db.enums import PostStatus, PostType
from cqc_lem.platform.db.repositories import posts as repo
from cqc_lem.utilities.ai_spend_cap import DailyAICapReached

pytestmark = pytest.mark.unit

_DB = "cqc_lem.utilities.db"
_UID = 77
_TEXT = "A finished draft about weekly reporting."


def _cap() -> DailyAICapReached:
    return DailyAICapReached(_UID, 0.6, 0.5, datetime(2099, 1, 1, tzinfo=timezone.utc))


@pytest.fixture
def row(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict]:
    """The post the task reads: owner, status and content, editable per test."""
    state = {"user_id": _UID, "status": PostStatus.PENDING.value, "content": _TEXT}
    with patch(f"{_DB}.get_post_user_id", side_effect=lambda _p: state["user_id"]), \
            patch(f"{_DB}.get_post_status", side_effect=lambda _p: state["status"]), \
            patch.object(rcp, "get_post_content", side_effect=lambda _p: state["content"]), \
            patch("cqc_lem.utilities.ai_spend_cap.enforce"):
        yield state


class TestGenerateAgentPost:
    def test_success_leaves_the_regenerated_post_as_written(self, row: dict) -> None:
        with patch.object(rcp, "regenerate_post", return_value="New draft") as regen, \
                patch.object(rcp, "update_db_post_status") as status:
            assert rcp.generate_agent_post(5, "the topic") == "New draft"
        regen.assert_called_once_with(5, "the topic")
        status.assert_not_called()  # regenerate_post's own close-out sets PENDING

    def test_nothing_written_marks_the_placeholder_error(self, row: dict) -> None:
        with patch.object(rcp, "regenerate_post", return_value=None), \
                patch.object(rcp, "update_db_post_status") as status:
            assert rcp.generate_agent_post(5, "the topic") is None
        status.assert_called_once_with(5, PostStatus.ERROR)

    def test_the_daily_cap_at_entry_marks_it_error_and_writes_nothing(self, row: dict) -> None:
        with patch("cqc_lem.utilities.ai_spend_cap.enforce", side_effect=_cap()), \
                patch.object(rcp, "regenerate_post") as regen, \
                patch.object(rcp, "update_db_post_status") as status:
            assert rcp.generate_agent_post(5, "t") is None
        regen.assert_not_called()
        status.assert_called_once_with(5, PostStatus.ERROR)

    @pytest.mark.parametrize("status_value", ["approved", "rejected", "error"])
    def test_a_post_that_left_pending_is_not_generated(self, row: dict, status_value: str) -> None:
        row["status"] = status_value
        with patch.object(rcp, "regenerate_post") as regen, \
                patch.object(rcp, "update_db_post_status") as status:
            assert rcp.generate_agent_post(5, "t") is None
        regen.assert_not_called()
        status.assert_not_called()

    def test_a_placeholder_approved_by_mistake_is_still_written(self, row: dict) -> None:
        """Approving a placeholder approved no content: the writer replaces it (and resets PENDING)."""
        from cqc_lem.api.mcp.spend import PLACEHOLDER_CONTENT

        row.update(status=PostStatus.APPROVED.value, content=PLACEHOLDER_CONTENT)
        with patch.object(rcp, "regenerate_post", return_value=None) as regen, \
                patch.object(rcp, "update_db_post_status") as status:
            rcp.generate_agent_post(5, "t")
        regen.assert_called_once_with(5, "t")
        status.assert_called_once_with(5, PostStatus.ERROR)  # nothing written: never left approved

    def test_the_task_wraps_the_function(self) -> None:
        with patch.object(rcp, "generate_agent_post", return_value="ok") as fn:
            assert rcp.generate_agent_post_task.run(5, "t") == "ok"
        fn.assert_called_once_with(5, "t")


@pytest.fixture
def image_engine() -> Iterator[dict[str, MagicMock]]:
    with patch("cqc_lem.utilities.post_image.generate_image_for_post",
               return_value=("/api/assets/new.png", None)) as render, \
            patch("cqc_lem.utilities.post_image.remove_post_image_file") as remove, \
            patch(f"{_DB}.get_post_image_url", return_value=None) as previous, \
            patch(f"{_DB}.attach_image_to_pending_post", return_value=True) as attach:
        yield {"render": render, "remove": remove, "previous": previous, "attach": attach}


class TestGenerateAgentPostImage:
    def test_the_render_is_the_post_image_engine_and_attaches_guarded(
            self, row: dict, image_engine: dict) -> None:
        assert rcp.generate_agent_post_image(5) == "/api/assets/new.png"
        image_engine["render"].assert_called_once_with(_UID, _TEXT, post_id=5)
        image_engine["attach"].assert_called_once_with(5, _UID, "/api/assets/new.png")
        image_engine["remove"].assert_not_called()

    def test_a_replaced_image_file_is_removed(self, row: dict, image_engine: dict) -> None:
        image_engine["previous"].return_value = "/api/assets/old.png"
        rcp.generate_agent_post_image(5)
        image_engine["remove"].assert_called_once_with("/api/assets/old.png")

    def test_a_post_approved_mid_render_never_gets_the_image(self, row: dict,
                                                             image_engine: dict) -> None:
        image_engine["attach"].return_value = False
        assert rcp.generate_agent_post_image(5) is None
        image_engine["remove"].assert_called_once_with("/api/assets/new.png")

    def test_a_failed_render_attaches_nothing(self, row: dict, image_engine: dict) -> None:
        image_engine["render"].return_value = (None, "gate rejected")
        assert rcp.generate_agent_post_image(5) is None
        image_engine["attach"].assert_not_called()

    def test_the_cap_at_entry_renders_nothing(self, row: dict, image_engine: dict) -> None:
        with patch("cqc_lem.utilities.ai_spend_cap.enforce", side_effect=_cap()):
            assert rcp.generate_agent_post_image(5) is None
        image_engine["render"].assert_not_called()

    def test_a_post_no_longer_pending_renders_nothing(self, row: dict, image_engine: dict) -> None:
        row["status"] = PostStatus.APPROVED.value
        assert rcp.generate_agent_post_image(5) is None
        image_engine["render"].assert_not_called()

    def test_the_task_wraps_the_function(self) -> None:
        with patch.object(rcp, "generate_agent_post_image", return_value="u") as fn:
            assert rcp.generate_agent_post_image_task.run(5) == "u"
        fn.assert_called_once_with(5)


def _store_signed(*_args: Any, signing: dict, **_kwargs: Any) -> str:
    signing.update(ai=True, signed=False)
    return "/api/assets/v.mp4"


@pytest.fixture
def video_engine() -> Iterator[dict[str, MagicMock]]:
    with patch.object(rcp, "_generate_video_src", return_value="https://runway/clip.mp4") as src, \
            patch.object(rcp, "_store_video_asset", side_effect=_store_signed) as store, \
            patch.object(rcp, "load_profile_for_user", return_value=None), \
            patch.object(rcp, "_apply_ai_disclosure", side_effect=lambda c: c + " [AI]"), \
            patch.object(rcp, "_needs_ai_disclosure", return_value=True) as decide, \
            patch(f"{_DB}.convert_pending_post_to_video", return_value=True) as convert:
        yield {"src": src, "store": store, "convert": convert, "decide": decide}


class TestGenerateAgentPostVideo:
    def test_a_rendered_clip_converts_the_post_after_it_is_stored_with_the_disclosure(
            self, row: dict, video_engine: dict) -> None:
        assert rcp.generate_agent_post_video(5) == "/api/assets/v.mp4"
        assert video_engine["src"].call_args.args[:2] == (_UID, _TEXT)
        video_engine["store"].assert_called_once()
        video_engine["convert"].assert_called_once_with(5, _UID, _TEXT + " [AI]")

    def test_the_one_disclosure_decision_is_fed_what_storage_reported(
            self, row: dict, video_engine: dict) -> None:
        rcp.generate_agent_post_video(5)
        video_engine["decide"].assert_called_once_with(5, ai_video=True, ai_video_signed=False)

    def test_no_line_when_the_decision_says_none(self, row: dict, video_engine: dict) -> None:
        video_engine["decide"].return_value = False
        rcp.generate_agent_post_video(5)
        video_engine["convert"].assert_called_once_with(5, _UID, _TEXT)

    def test_no_clip_means_no_conversion(self, row: dict, video_engine: dict) -> None:
        video_engine["src"].return_value = None
        assert rcp.generate_agent_post_video(5) is None
        video_engine["store"].assert_not_called()
        video_engine["convert"].assert_not_called()

    def test_a_storage_failure_is_warned_and_converts_nothing(self, row: dict,
                                                              video_engine: dict) -> None:
        video_engine["store"].side_effect = RuntimeError("probe failed")
        with patch.object(rcp, "log_warning") as warn:
            assert rcp.generate_agent_post_video(5) is None
        warn.assert_called_once()
        video_engine["convert"].assert_not_called()

    def test_an_unstored_clip_converts_nothing(self, row: dict, video_engine: dict) -> None:
        video_engine["store"].side_effect = None
        video_engine["store"].return_value = None
        assert rcp.generate_agent_post_video(5) is None
        video_engine["convert"].assert_not_called()

    def test_a_post_that_left_pending_is_not_converted(self, row: dict, video_engine: dict) -> None:
        video_engine["convert"].return_value = False
        assert rcp.generate_agent_post_video(5) is None

    def test_the_cap_at_entry_renders_nothing(self, row: dict, video_engine: dict) -> None:
        with patch("cqc_lem.utilities.ai_spend_cap.enforce", side_effect=_cap()):
            assert rcp.generate_agent_post_video(5) is None
        video_engine["src"].assert_not_called()

    def test_an_empty_post_renders_nothing(self, row: dict, video_engine: dict) -> None:
        row["content"] = None
        assert rcp.generate_agent_post_video(5) is None
        video_engine["src"].assert_not_called()

    def test_the_task_wraps_the_function(self) -> None:
        with patch.object(rcp, "generate_agent_post_video", return_value="v") as fn:
            assert rcp.generate_agent_post_video_task.run(5) == "v"
        fn.assert_called_once_with(5)


# --- the SQL-guarded writes ----------------------------------------------------------------------


def _cursor(rowcount: int = 1, raises: Optional[Exception] = None) -> tuple[MagicMock, MagicMock]:
    cur = MagicMock(rowcount=rowcount)
    if raises:
        cur.execute.side_effect = raises
    ctx = MagicMock()
    ctx.__enter__.return_value = cur
    return ctx, cur


class TestGuardedWrites:
    @pytest.mark.parametrize("call", [
        lambda: repo.attach_image_to_pending_post(5, _UID, "/api/assets/x.png"),
        lambda: repo.convert_pending_post_to_video(5, _UID, "caption"),
    ])
    def test_the_update_is_scoped_to_this_users_pending_row(self, call: Any) -> None:
        ctx, cur = _cursor()
        with patch.object(repo, "db_cursor", return_value=ctx):
            assert call() is True
        sql, params = cur.execute.call_args.args
        assert "AND user_id = %s AND status = %s" in sql
        assert params[-3:] == (5, _UID, PostStatus.PENDING.value)

    def test_the_conversion_sets_video_and_the_caption(self) -> None:
        ctx, cur = _cursor()
        with patch.object(repo, "db_cursor", return_value=ctx):
            repo.convert_pending_post_to_video(5, _UID, "caption")
        assert cur.execute.call_args.args[1][:2] == (PostType.VIDEO.value, "caption")

    @pytest.mark.parametrize("fn,args", [
        (repo.attach_image_to_pending_post, (5, _UID, "/x.png")),
        (repo.convert_pending_post_to_video, (5, _UID, "c")),
    ])
    def test_no_matching_row_is_false(self, fn: Any, args: tuple) -> None:
        ctx, _cur = _cursor(rowcount=0)
        with patch.object(repo, "db_cursor", return_value=ctx):
            assert fn(*args) is False

    @pytest.mark.parametrize("fn,args", [
        (repo.attach_image_to_pending_post, (5, _UID, "/x.png")),
        (repo.convert_pending_post_to_video, (5, _UID, "c")),
    ])
    def test_a_database_fault_is_logged_and_false(self, fn: Any, args: tuple) -> None:
        import mysql.connector

        ctx, _cur = _cursor(raises=mysql.connector.Error("gone"))
        with patch.object(repo, "db_cursor", return_value=ctx), \
                patch.object(repo, "log_error") as err:
            assert fn(*args) is False
        err.assert_called_once()
