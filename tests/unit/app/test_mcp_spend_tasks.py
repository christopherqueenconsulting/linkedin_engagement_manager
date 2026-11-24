"""The Celery side of the MCP spend tools (W2b, issue #2689), and its SQL-guarded writes.

`generate_agent_post*` run the SAME generation paths the studio uses, and must never move a row a
human acted on: text through `create_text_post` + `_apply_guidance_to_text_post`, an image through
`generate_image_for_post`, a video through `_generate_video_src` + `_store_video_asset`. These tests
patch those paths at their boundaries, so no LLM, render, credit or database is reached. Redis is an
in-memory fake, so the once-per-dispatch claims are real.
"""

from datetime import datetime, timezone
from typing import Any, Iterator, Optional
from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.api.mcp import spend
from cqc_lem.api.mcp.spend import PLACEHOLDER_CONTENT
from cqc_lem.app import run_content_plan as rcp
from cqc_lem.platform.db.enums import PostStatus, PostType
from cqc_lem.platform.db.repositories import posts as repo
from cqc_lem.utilities.ai_spend_cap import DailyAICapReached

pytestmark = pytest.mark.unit

_DB = "cqc_lem.utilities.db"
_UID = 77
_TEXT = "A finished draft about weekly reporting."


class _Redis:
    """Just enough Redis for the claims: SET NX EX, GET, DELETE."""

    def __init__(self) -> None:
        self.store: dict[str, Any] = {}

    def set(self, key: str, value: Any, ex: Optional[int] = None, nx: bool = False) -> bool:
        if nx and key in self.store:
            return False
        self.store[key] = value
        return True

    def get(self, key: str) -> Any:
        return self.store.get(key)

    def delete(self, key: str) -> int:
        return int(self.store.pop(key, None) is not None)


@pytest.fixture(autouse=True)
def writes_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every task re-reads the kill switch and `mcp-server-writes` at start: on, and not killed."""
    monkeypatch.setenv("MCP_SERVER_WRITES_ENABLED", "true")
    monkeypatch.delenv("MCP_SERVER_KILL", raising=False)


@pytest.fixture(autouse=True)
def redis() -> Iterator[_Redis]:
    fake = _Redis()
    with patch("cqc_lem.api.mcp.limits._redis", return_value=fake):
        yield fake


def _cap() -> DailyAICapReached:
    return DailyAICapReached(_UID, 0.6, 0.5, datetime(2099, 1, 1, tzinfo=timezone.utc))


def _row(**overrides: Any) -> dict:
    row = {"id": 5, "status": PostStatus.PENDING.value, "post_type": PostType.TEXT.value,
           "content": _TEXT, "image_url": None, "video_url": None}
    row.update(overrides)
    return row


@pytest.fixture
def row() -> Iterator[dict]:
    """The post the task reads, owner-scoped; edit it per test."""
    state = _row()
    with patch(f"{_DB}.get_post_user_id", return_value=_UID), \
            patch(f"{_DB}.get_post_for_user", side_effect=lambda _u, _p: dict(state)), \
            patch("cqc_lem.utilities.ai_spend_cap.enforce"):
        yield state


def _run_held(kind: str, redis: _Redis, post_id: int = 5) -> bool:
    return f"mcp:spend:run:{kind}:{post_id}" in redis.store


def _inflight_held(kind: str, redis: _Redis, post_id: int = 5) -> bool:
    return f"mcp:spend:inflight:{kind}:{post_id}" in redis.store


def _stop(stop: str, redis: _Redis, monkeypatch: pytest.MonkeyPatch) -> None:
    """Throw one of the four things that must stop a queued spend task."""
    if stop == "env_kill":
        monkeypatch.setenv("MCP_SERVER_KILL", "1")
    elif stop == "key_kill":
        redis.store["mcp:kill"] = b"1"
    elif stop == "key_unreadable":
        monkeypatch.setattr(redis, "get", MagicMock(side_effect=ConnectionError("down")))
    else:
        monkeypatch.setenv("MCP_SERVER_WRITES_ENABLED", "false")


# --- text ----------------------------------------------------------------------------------------


@pytest.fixture
def writer(row: dict) -> Iterator[dict[str, MagicMock]]:
    row["content"] = PLACEHOLDER_CONTENT
    with patch.object(rcp, "create_text_post", return_value="Draft") as create, \
            patch.object(rcp, "_apply_guidance_to_text_post", side_effect=lambda *a: a[2] + "!"), \
            patch.object(rcp, "_needs_ai_disclosure", return_value=False), \
            patch.object(rcp, "load_profile_for_user", return_value=None), \
            patch.object(rcp, "_post_content_mix", return_value=None), \
            patch(f"{_DB}.get_post_buyer_stage", return_value="awareness"), \
            patch.object(rcp, "_score_and_persist_dwell") as dwell, \
            patch.object(rcp, "_gate_findings_for_post", return_value=[]), \
            patch.object(rcp, "_persist_gate_findings") as gates, \
            patch.object(rcp, "forget_supplied_material"), \
            patch.object(rcp, "update_db_post_status") as status, \
            patch.object(rcp, "update_db_post_content") as content, \
            patch(f"{_DB}.write_agent_post_draft", return_value=True) as write, \
            patch(f"{_DB}.fail_agent_post_placeholder", return_value=True) as fail:
        yield {"create": create, "dwell": dwell, "gates": gates, "status": status,
               "content": content, "write": write, "fail": fail}


class TestGenerateAgentPost:
    def test_the_draft_lands_through_the_guarded_write_and_no_status_is_set(
            self, writer: dict, redis: _Redis) -> None:
        assert rcp.generate_agent_post(5, "the topic") == "Draft!"
        writer["write"].assert_called_once_with(5, _UID, "Draft!", PLACEHOLDER_CONTENT)
        writer["status"].assert_not_called()
        writer["content"].assert_not_called()
        writer["dwell"].assert_called_once()
        writer["gates"].assert_called_once()
        writer["fail"].assert_not_called()

    def test_a_human_edit_while_queued_is_left_alone(self, writer: dict, row: dict) -> None:
        row["content"] = "The human's own words."
        assert rcp.generate_agent_post(5, "t") is None
        writer["create"].assert_not_called()
        writer["write"].assert_not_called()
        writer["fail"].assert_not_called()

    @pytest.mark.parametrize("status", ["rejected", "approved", "scheduled", "posted", "error"])
    def test_a_reject_approve_or_publish_while_queued_is_left_alone(
            self, writer: dict, row: dict, status: str) -> None:
        row["status"] = status
        assert rcp.generate_agent_post(5, "t") is None
        writer["create"].assert_not_called()
        writer["write"].assert_not_called()
        writer["fail"].assert_not_called()
        writer["status"].assert_not_called()

    def test_a_change_while_the_draft_was_written_wins(self, writer: dict) -> None:
        writer["write"].return_value = False
        assert rcp.generate_agent_post(5, "t") is None
        writer["dwell"].assert_not_called()
        writer["gates"].assert_not_called()
        writer["fail"].assert_not_called()
        writer["status"].assert_not_called()

    def test_nothing_written_fails_only_the_untouched_placeholder(self, writer: dict,
                                                                   redis: _Redis) -> None:
        writer["create"].return_value = None
        assert rcp.generate_agent_post(5, "t") is None
        writer["fail"].assert_called_once_with(5, _UID, PLACEHOLDER_CONTENT)
        writer["status"].assert_not_called()
        assert not _run_held("text", redis)  # a terminal failure frees the claim

    def test_the_daily_cap_at_entry_writes_nothing(self, writer: dict) -> None:
        with patch("cqc_lem.utilities.ai_spend_cap.enforce", side_effect=_cap()):
            assert rcp.generate_agent_post(5, "t") is None
        writer["create"].assert_not_called()
        writer["fail"].assert_called_once_with(5, _UID, PLACEHOLDER_CONTENT)

    def test_a_crash_fails_the_placeholder_frees_the_claim_and_raises(
            self, writer: dict, redis: _Redis) -> None:
        writer["create"].side_effect = RuntimeError("llm down")
        with pytest.raises(RuntimeError):
            rcp.generate_agent_post(5, "t")
        writer["fail"].assert_called_once()
        assert not _run_held("text", redis)

    def test_a_redelivery_on_a_placeholder_never_writes_again_and_ends_in_error(
            self, writer: dict, redis: _Redis) -> None:
        assert spend.claim_run(5, spend.SPEND_TEXT) is True  # the first delivery's claim
        assert rcp.generate_agent_post(5, "t") is None
        writer["create"].assert_not_called()
        writer["write"].assert_not_called()
        # Guarded on PENDING + placeholder: only a row still holding it becomes `error`.
        writer["fail"].assert_called_once_with(5, _UID, PLACEHOLDER_CONTENT)

    def test_a_redelivery_after_the_text_was_written_leaves_the_row_untouched(
            self, writer: dict, row: dict) -> None:
        spend.claim_run(5, spend.SPEND_TEXT)
        row["content"] = "The draft the first run wrote."
        assert rcp.generate_agent_post(5, "t") is None
        writer["create"].assert_not_called()
        writer["fail"].assert_not_called()
        writer["write"].assert_not_called()

    def test_an_unreadable_claim_spends_nothing_and_fails_the_placeholder(
            self, writer: dict) -> None:
        with patch.object(rcp, "_agent_spend_halted", return_value=False), \
                patch("cqc_lem.api.mcp.limits._redis", return_value=None):
            assert rcp.generate_agent_post(5, "t") is None
        writer["create"].assert_not_called()
        writer["fail"].assert_called_once_with(5, _UID, PLACEHOLDER_CONTENT)

    @pytest.mark.parametrize("stop", ["env_kill", "key_kill", "key_unreadable", "writes_off"])
    def test_a_kill_or_flag_off_after_queueing_stops_the_writer(
            self, writer: dict, redis: _Redis, monkeypatch: pytest.MonkeyPatch,
            stop: str) -> None:
        _stop(stop, redis, monkeypatch)
        assert rcp.generate_agent_post(5, "t") is None
        writer["create"].assert_not_called()
        writer["fail"].assert_called_once_with(5, _UID, PLACEHOLDER_CONTENT)
        assert not _run_held("text", redis)

    def test_an_unreadable_row_spends_nothing(self, writer: dict) -> None:
        from cqc_lem.platform.db.shared import OwnershipUnprovable

        with patch(f"{_DB}.get_post_for_user", side_effect=OwnershipUnprovable("db")):
            assert rcp.generate_agent_post(5, "t") is None
        writer["create"].assert_not_called()

    def test_the_disclosure_decision_is_the_shared_one(self, writer: dict) -> None:
        with patch.object(rcp, "_needs_ai_disclosure", return_value=True), \
                patch.object(rcp, "_apply_ai_disclosure", side_effect=lambda c: c + " [AI]"):
            assert rcp.generate_agent_post(5, "t") == "Draft! [AI]"

    def test_the_task_wraps_the_function(self) -> None:
        with patch.object(rcp, "generate_agent_post", return_value="ok") as fn:
            assert rcp.generate_agent_post_task.run(5, "t") == "ok"
        fn.assert_called_once_with(5, "t")


# --- image ---------------------------------------------------------------------------------------


@pytest.fixture
def image_engine(row: dict) -> Iterator[dict[str, MagicMock]]:
    with patch("cqc_lem.utilities.post_image.generate_image_for_post",
               return_value=("/api/assets/new.png", None)) as render, \
            patch("cqc_lem.utilities.post_image.remove_post_image_file") as remove, \
            patch(f"{_DB}.attach_image_to_pending_post", return_value=True) as attach:
        yield {"render": render, "remove": remove, "attach": attach}


class TestGenerateAgentPostImage:
    def test_the_render_is_the_post_image_engine_and_attaches_guarded(
            self, image_engine: dict, redis: _Redis) -> None:
        spend.claim_inflight(5, spend.SPEND_IMAGE)
        assert rcp.generate_agent_post_image(5) == "/api/assets/new.png"
        image_engine["render"].assert_called_once_with(_UID, _TEXT, post_id=5)
        image_engine["attach"].assert_called_once_with(5, _UID, "/api/assets/new.png")
        image_engine["remove"].assert_not_called()
        assert "mcp:spend:inflight:image:5" not in redis.store  # the task releases it
        assert _run_held("image", redis)  # kept: a redelivery must not render again

    def test_a_redelivered_task_never_renders_again(self, image_engine: dict) -> None:
        assert rcp.generate_agent_post_image(5) == "/api/assets/new.png"
        image_engine["render"].reset_mock()
        assert rcp.generate_agent_post_image(5) is None
        image_engine["render"].assert_not_called()

    def test_an_unreadable_claim_frees_the_routes_in_flight_marker(
            self, image_engine: dict, redis: _Redis) -> None:
        spend.claim_inflight(5, spend.SPEND_IMAGE)
        with patch.object(rcp, "_agent_spend_halted", return_value=False), \
                patch.object(redis, "set", side_effect=ConnectionError("down")):
            assert rcp.generate_agent_post_image(5) is None
        image_engine["render"].assert_not_called()
        assert not _inflight_held("image", redis)

    def test_a_held_claim_keeps_the_marker_until_its_ttl(self, image_engine: dict,
                                                        redis: _Redis) -> None:
        """The first run may still be rendering, so a redelivery leaves the lock to expire (2h)."""
        spend.claim_inflight(5, spend.SPEND_IMAGE)
        spend.claim_run(5, spend.SPEND_IMAGE)
        assert rcp.generate_agent_post_image(5) is None
        image_engine["render"].assert_not_called()
        assert _inflight_held("image", redis)
        assert spend._CLAIM_TTL_SECONDS == {"text": 7200, "image": 7200, "video": 14400}

    @pytest.mark.parametrize("stop", ["env_kill", "key_kill", "key_unreadable", "writes_off"])
    def test_a_kill_or_flag_off_after_queueing_renders_nothing(
            self, image_engine: dict, redis: _Redis, monkeypatch: pytest.MonkeyPatch,
            stop: str) -> None:
        spend.claim_inflight(5, spend.SPEND_IMAGE)
        _stop(stop, redis, monkeypatch)
        assert rcp.generate_agent_post_image(5) is None
        image_engine["render"].assert_not_called()
        assert not _inflight_held("image", redis)
        assert not _run_held("image", redis)

    def test_a_post_that_already_has_an_image_is_not_rendered(self, image_engine: dict,
                                                              row: dict) -> None:
        row["image_url"] = "/api/assets/old.png"
        assert rcp.generate_agent_post_image(5) is None
        image_engine["render"].assert_not_called()

    @pytest.mark.parametrize("change", [{"status": "approved"}, {"status": "rejected"},
                                        {"post_type": "video"}, {"content": PLACEHOLDER_CONTENT}])
    def test_a_post_that_changed_is_not_rendered(self, image_engine: dict, row: dict,
                                                 change: dict) -> None:
        row.update(change)
        assert rcp.generate_agent_post_image(5) is None
        image_engine["render"].assert_not_called()

    def test_an_approve_mid_render_leaves_the_row_and_deletes_the_file(
            self, image_engine: dict, redis: _Redis) -> None:
        image_engine["attach"].return_value = False
        assert rcp.generate_agent_post_image(5) is None
        image_engine["remove"].assert_called_once_with("/api/assets/new.png")

    def test_a_failed_render_frees_the_claim(self, image_engine: dict, redis: _Redis) -> None:
        image_engine["render"].return_value = (None, "gate rejected")
        assert rcp.generate_agent_post_image(5) is None
        image_engine["attach"].assert_not_called()
        assert not _run_held("image", redis)

    def test_the_cap_at_entry_renders_nothing(self, image_engine: dict, redis: _Redis) -> None:
        with patch("cqc_lem.utilities.ai_spend_cap.enforce", side_effect=_cap()):
            assert rcp.generate_agent_post_image(5) is None
        image_engine["render"].assert_not_called()
        assert not _run_held("image", redis)

    def test_a_crash_frees_the_claim_and_raises(self, image_engine: dict, redis: _Redis) -> None:
        image_engine["render"].side_effect = RuntimeError("render down")
        with pytest.raises(RuntimeError):
            rcp.generate_agent_post_image(5)
        assert not _run_held("image", redis)

    def test_the_task_wraps_the_function(self) -> None:
        with patch.object(rcp, "generate_agent_post_image", return_value="u") as fn:
            assert rcp.generate_agent_post_image_task.run(5) == "u"
        fn.assert_called_once_with(5)


# --- video ---------------------------------------------------------------------------------------

_VIDEO_URL = "https://lem.example/api/assets?file_name=videos/runwayml/clip.mp4"


def _store(*_args: Any, signing: dict, captions: dict, **_kwargs: Any) -> str:
    signing.update(ai=True, signed=False)
    captions.update(caption_text="Hook", caption_srt_url="/api/assets/c.srt")
    return _VIDEO_URL


@pytest.fixture
def video_engine(row: dict) -> Iterator[dict[str, MagicMock]]:
    with patch.object(rcp, "_generate_video_src", return_value="https://runway/clip.mp4") as src, \
            patch.object(rcp, "_store_video_asset", side_effect=_store) as store, \
            patch.object(rcp, "load_profile_for_user", return_value=None), \
            patch.object(rcp, "_apply_ai_disclosure", side_effect=lambda c: c + " [AI]"), \
            patch.object(rcp, "_needs_ai_disclosure", return_value=True) as decide, \
            patch.object(rcp, "_remove_stored_video") as remove, \
            patch(f"{_DB}.convert_pending_post_to_video", return_value=True) as convert:
        yield {"src": src, "store": store, "convert": convert, "decide": decide, "remove": remove}


class TestGenerateAgentPostVideo:
    def test_everything_lands_in_the_one_guarded_conversion(self, video_engine: dict) -> None:
        assert rcp.generate_agent_post_video(5) == _VIDEO_URL
        assert video_engine["src"].call_args.args[:2] == (_UID, _TEXT)
        assert "captions" in video_engine["store"].call_args.kwargs  # store persists nothing
        video_engine["convert"].assert_called_once_with(
            5, _UID, _TEXT + " [AI]", _VIDEO_URL, "Hook", "/api/assets/c.srt")
        video_engine["remove"].assert_not_called()

    def test_the_one_disclosure_decision_is_fed_what_storage_reported(
            self, video_engine: dict) -> None:
        rcp.generate_agent_post_video(5)
        video_engine["decide"].assert_called_once_with(5, ai_video=True, ai_video_signed=False)

    def test_no_line_when_the_decision_says_none(self, video_engine: dict) -> None:
        video_engine["decide"].return_value = False
        rcp.generate_agent_post_video(5)
        assert video_engine["convert"].call_args.args[2] == _TEXT

    def test_an_approve_mid_render_leaves_the_row_and_deletes_the_mp4(
            self, video_engine: dict) -> None:
        video_engine["convert"].return_value = False
        assert rcp.generate_agent_post_video(5) is None
        video_engine["remove"].assert_called_once_with(_VIDEO_URL, post_id=5, user_id=_UID)

    def test_a_redelivered_task_never_renders_or_deducts_credits_again(
            self, video_engine: dict) -> None:
        assert rcp.generate_agent_post_video(5) == _VIDEO_URL
        video_engine["src"].reset_mock()
        assert rcp.generate_agent_post_video(5) is None
        video_engine["src"].assert_not_called()  # `_generate_video_src` is where credits go

    @pytest.mark.parametrize("stop", ["env_kill", "key_kill", "key_unreadable", "writes_off"])
    def test_a_kill_or_flag_off_after_queueing_never_reaches_credits(
            self, video_engine: dict, redis: _Redis, monkeypatch: pytest.MonkeyPatch,
            stop: str) -> None:
        spend.claim_inflight(5, spend.SPEND_VIDEO)
        _stop(stop, redis, monkeypatch)
        assert rcp.generate_agent_post_video(5) is None
        video_engine["src"].assert_not_called()  # where premium credits are deducted
        assert not _inflight_held("video", redis)

    def test_an_unreadable_claim_frees_the_marker_and_a_held_one_keeps_it(
            self, video_engine: dict, redis: _Redis) -> None:
        spend.claim_inflight(5, spend.SPEND_VIDEO)
        spend.claim_run(5, spend.SPEND_VIDEO)
        assert rcp.generate_agent_post_video(5) is None
        assert _inflight_held("video", redis)  # held: left for its 4h TTL
        with patch.object(rcp, "_agent_spend_halted", return_value=False), \
                patch.object(redis, "set", side_effect=ConnectionError("down")):
            assert rcp.generate_agent_post_video(5) is None
        assert not _inflight_held("video", redis)
        video_engine["src"].assert_not_called()

    @pytest.mark.parametrize("change", [{"status": "approved"}, {"post_type": "video"},
                                        {"video_url": "/x.mp4"}, {"content": None}])
    def test_a_post_that_changed_is_not_rendered(self, video_engine: dict, row: dict,
                                                 change: dict) -> None:
        row.update(change)
        assert rcp.generate_agent_post_video(5) is None
        video_engine["src"].assert_not_called()

    def test_no_clip_frees_the_claim(self, video_engine: dict, redis: _Redis) -> None:
        video_engine["src"].return_value = None
        assert rcp.generate_agent_post_video(5) is None
        video_engine["store"].assert_not_called()
        assert not _run_held("video", redis)

    def test_a_storage_failure_is_warned_and_frees_the_claim(self, video_engine: dict,
                                                             redis: _Redis) -> None:
        video_engine["store"].side_effect = RuntimeError("probe failed")
        with patch.object(rcp, "log_warning") as warn:
            assert rcp.generate_agent_post_video(5) is None
        warn.assert_called_once()
        video_engine["convert"].assert_not_called()
        assert not _run_held("video", redis)

    def test_an_unstored_clip_converts_nothing(self, video_engine: dict) -> None:
        video_engine["store"].side_effect = None
        video_engine["store"].return_value = None
        assert rcp.generate_agent_post_video(5) is None
        video_engine["convert"].assert_not_called()

    def test_the_cap_at_entry_renders_nothing(self, video_engine: dict) -> None:
        with patch("cqc_lem.utilities.ai_spend_cap.enforce", side_effect=_cap()):
            assert rcp.generate_agent_post_video(5) is None
        video_engine["src"].assert_not_called()

    def test_the_task_wraps_the_function(self) -> None:
        with patch.object(rcp, "generate_agent_post_video", return_value="v") as fn:
            assert rcp.generate_agent_post_video_task.run(5) == "v"
        fn.assert_called_once_with(5)


class TestStoredVideoFiles:
    def test_a_discarded_mp4_is_deleted(self, tmp_path: Any) -> None:
        folder = tmp_path / "videos" / "runwayml"
        folder.mkdir(parents=True)
        clip = folder / "clip.mp4"
        clip.write_bytes(b"x")
        with patch.object(rcp, "assets_dir", str(tmp_path)):
            rcp._remove_stored_video(_VIDEO_URL, post_id=5, user_id=_UID)
            rcp._remove_stored_video(_VIDEO_URL, post_id=5, user_id=_UID)  # already gone: quiet
        assert not clip.exists()

    @pytest.mark.parametrize("url", [None, "", "https://elsewhere/clip.mp4",
                                     "https://x/api/assets?file_name=videos/runwayml/"])
    def test_a_url_naming_no_stored_video_deletes_nothing(self, url: Optional[str]) -> None:
        with patch.object(rcp.os, "remove") as remove:
            rcp._remove_stored_video(url, post_id=5, user_id=_UID)
        remove.assert_not_called()

    def test_a_delete_fault_is_warned(self, tmp_path: Any) -> None:
        with patch.object(rcp, "assets_dir", str(tmp_path)), \
                patch.object(rcp.os, "remove", side_effect=PermissionError("ro")), \
                patch.object(rcp, "log_warning") as warn:
            rcp._remove_stored_video(_VIDEO_URL, post_id=5, user_id=_UID)
        warn.assert_called_once()

    def test_store_with_captions_persists_no_url_and_no_caption_row(self, tmp_path: Any) -> None:
        """The opt-out the MCP path uses: the caller persists, `_store_video_asset` does not."""
        result = MagicMock(srt_path=str(tmp_path / "c.srt"), caption_text="Hook")
        captions: dict = {}
        with patch.object(rcp, "save_video_url_to_dir", return_value=str(tmp_path / "clip.mp4")), \
                patch.object(rcp, "create_folder_if_not_exists"), \
                patch.object(rcp, "_accept_probed_video", return_value=True), \
                patch("cqc_lem.utilities.video_captions.apply_captions_to_video",
                      return_value=result), \
                patch("cqc_lem.utilities.video_captions.caption_srt_asset_url",
                      return_value="/api/assets/c.srt"), \
                patch(f"{_DB}.post_avatar_media_state", return_value=False), \
                patch(f"{_DB}.update_db_post_captions") as caption_row, \
                patch.object(rcp, "_sign_ai_video", return_value=True), \
                patch.object(rcp, "_record_video_asset_measures"), \
                patch.object(rcp, "write_brief_receipt"), \
                patch.object(rcp, "update_db_post_video_url") as url_row:
            url = rcp._store_video_asset(5, "https://runway/clip.mp4", content=_TEXT, user_id=_UID,
                                         captions=captions)
        assert url and url.endswith("videos/runwayml/clip.mp4")
        url_row.assert_not_called()
        caption_row.assert_not_called()
        assert captions == {"caption_text": "Hook", "caption_srt_url": "/api/assets/c.srt"}

    def test_store_without_captions_still_persists_as_before(self, tmp_path: Any) -> None:
        with patch.object(rcp, "save_video_url_to_dir", return_value=str(tmp_path / "clip.mp4")), \
                patch.object(rcp, "create_folder_if_not_exists"), \
                patch.object(rcp, "_accept_probed_video", return_value=True), \
                patch.object(rcp, "_caption_video_asset"), \
                patch.object(rcp, "_sign_ai_video", return_value=False), \
                patch.object(rcp, "_record_video_asset_measures"), \
                patch.object(rcp, "write_brief_receipt"), \
                patch.object(rcp, "update_db_post_video_url") as url_row:
            url = rcp._store_video_asset(5, "https://runway/clip.mp4", content=_TEXT, user_id=_UID)
        url_row.assert_called_once_with(5, url)


# --- the SQL-guarded writes ----------------------------------------------------------------------


def _cursor(rowcount: int = 1, raises: Optional[Exception] = None) -> tuple[MagicMock, MagicMock]:
    cur = MagicMock(rowcount=rowcount)
    if raises:
        cur.execute.side_effect = raises
    ctx = MagicMock()
    ctx.__enter__.return_value = cur
    return ctx, cur


def _sql(call: Any) -> tuple[str, tuple]:
    ctx, cur = _cursor()
    with patch.object(repo, "db_cursor", return_value=ctx):
        assert call() is True
    return cur.execute.call_args.args


class TestGuardedWrites:
    def test_the_draft_write_needs_pending_and_the_untouched_placeholder(self) -> None:
        sql, params = _sql(lambda: repo.write_agent_post_draft(5, _UID, "Draft", "PH"))
        assert sql.startswith("UPDATE posts SET content = %s WHERE")
        assert "status = %s AND content = %s" in sql and "status = %s," not in sql
        assert params == ("Draft", 5, _UID, PostStatus.PENDING.value, "PH")

    def test_the_error_fallback_only_moves_an_untouched_pending_placeholder(self) -> None:
        sql, params = _sql(lambda: repo.fail_agent_post_placeholder(5, _UID, "PH"))
        assert "SET status = %s WHERE" in sql and "AND content = %s" in sql
        assert params == (PostStatus.ERROR.value, 5, _UID, PostStatus.PENDING.value, "PH")

    def test_the_image_attach_needs_a_pending_text_post_without_an_image(self) -> None:
        sql, params = _sql(lambda: repo.attach_image_to_pending_post(5, _UID, "/x.png"))
        assert "status = %s AND post_type = %s" in sql and "image_url IS NULL" in sql
        assert params == ("/x.png", 5, _UID, PostStatus.PENDING.value, PostType.TEXT.value)

    def test_the_video_conversion_writes_the_url_inside_the_guard(self) -> None:
        sql, params = _sql(lambda: repo.convert_pending_post_to_video(
            5, _UID, "caption", "/v.mp4", "Hook", "/c.srt"))
        assert "video_url = %s" in sql.split("WHERE")[0]
        assert "status = %s AND post_type = %s" in sql and "video_url IS NULL" in sql
        assert params == (PostType.VIDEO.value, "caption", "/v.mp4", "Hook", "/c.srt", 5, _UID,
                          PostStatus.PENDING.value, PostType.TEXT.value)

    @pytest.mark.parametrize("call", [
        lambda: repo.write_agent_post_draft(5, _UID, "c", "PH"),
        lambda: repo.fail_agent_post_placeholder(5, _UID, "PH"),
        lambda: repo.attach_image_to_pending_post(5, _UID, "/x.png"),
        lambda: repo.convert_pending_post_to_video(5, _UID, "c", "/v.mp4"),
    ])
    def test_no_matching_row_is_false(self, call: Any) -> None:
        ctx, _cur = _cursor(rowcount=0)
        with patch.object(repo, "db_cursor", return_value=ctx):
            assert call() is False

    def test_a_database_fault_is_logged_and_false(self) -> None:
        import mysql.connector

        ctx, _cur = _cursor(raises=mysql.connector.Error("gone"))
        with patch.object(repo, "db_cursor", return_value=ctx), \
                patch.object(repo, "log_error") as err:
            assert repo.attach_image_to_pending_post(5, _UID, "/x.png") is False
        err.assert_called_once()


def test_no_spend_path_ever_sets_a_row_back_to_pending() -> None:
    """Read straight off the source: the only status the MCP spend writes set is ERROR."""
    import inspect

    for fn in (repo.write_agent_post_draft, repo.fail_agent_post_placeholder,
               repo.attach_image_to_pending_post, repo.convert_pending_post_to_video):
        source = inspect.getsource(fn)
        assert "SET status = %s" not in source or "PostStatus.ERROR.value" in source, fn.__name__
    for fn in (rcp.generate_agent_post, rcp.generate_agent_post_image,
               rcp.generate_agent_post_video):
        assert "update_db_post_status" not in inspect.getsource(fn), fn.__name__


# --- a placeholder can never reach the posting lane -----------------------------------------------


class TestPlaceholderNeverPublishes:
    def test_the_ready_to_post_query_excludes_the_placeholder(self) -> None:
        ctx, cur = _cursor()
        cur.fetchall.return_value = []
        with patch.object(repo, "db_cursor", return_value=ctx):
            assert repo.get_ready_to_post_posts() == []
        sql, params = cur.execute.call_args.args
        assert "p.content <> %s" in sql and "status = 'approved'" in sql
        assert params[-1] == PLACEHOLDER_CONTENT

    @pytest.mark.parametrize("status", ["approved", "scheduled"])
    def test_post_to_linkedin_refuses_it_before_anything_is_sent(self, status: str) -> None:
        from cqc_lem.app.engagement import posting

        with patch.object(posting, "get_post_status", return_value=status), \
                patch.object(posting, "get_post_content", return_value=PLACEHOLDER_CONTENT), \
                patch.object(posting, "update_db_post_status") as set_status, \
                patch.object(posting, "sandbox_dry_run") as dry_run, \
                patch.object(posting, "get_user_password_pair_by_id") as credentials:
            result = posting.post_to_linkedin.run(user_id=_UID, post_id=5)
        assert "agent placeholder" in result
        set_status.assert_called_once_with(5, PostStatus.ERROR)
        dry_run.assert_not_called()  # not even a simulated publish
        credentials.assert_not_called()

    def test_a_written_post_is_not_refused(self) -> None:
        from cqc_lem.app.engagement import posting

        with patch.object(posting, "get_post_status", return_value="scheduled"), \
                patch.object(posting, "get_post_content", return_value=_TEXT), \
                patch.object(posting, "update_db_post_status") as set_status, \
                patch.object(posting, "sandbox_dry_run", return_value=True):
            result = posting.post_to_linkedin.run(user_id=_UID, post_id=5)
        assert "agent placeholder" not in result
        set_status.assert_called_once_with(5, PostStatus.POSTED)  # the sandbox dry run's own write
