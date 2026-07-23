"""Coverage tests for run_content_plan pure logic: asset guards, AI disclosure, premium
tiers, weekly-window selection, video save pipeline, sitemap/blog scraping helpers."""

from datetime import datetime as _real_datetime
from unittest.mock import MagicMock, patch

import pytest
import requests

pytestmark = pytest.mark.unit

_RCP = "cqc_lem.app.run_content_plan"


class _SaturdayDatetime(_real_datetime):
    @classmethod
    def now(cls, tz=None):
        return _real_datetime(2024, 1, 6, 12, 0)  # Saturday, weekday() == 5


class _MondayDatetime(_real_datetime):
    @classmethod
    def now(cls, tz=None):
        return _real_datetime(2024, 1, 8, 12, 0)  # Monday


class TestCarouselSlidesAreRealImages:
    def test_empty_is_not_real(self):
        from cqc_lem.app.run_content_plan import _carousel_slides_are_real_images
        assert _carousel_slides_are_real_images(None) is False
        assert _carousel_slides_are_real_images([]) is False
        assert _carousel_slides_are_real_images("") is False

    def test_plain_titles_are_not_real(self):
        from cqc_lem.app.run_content_plan import _carousel_slides_are_real_images
        assert _carousel_slides_are_real_images(["Intro", "Point 1", "CTA"]) is False

    @pytest.mark.parametrize("slides", [
        ["https://api/assets?file_name=images/carousel/9/s1.png"],
        ["/assets/images/1.jpg"],
        ["slide1.png"],
        ["photo.jpg"],
    ])
    def test_url_or_image_markers_are_real(self, slides):
        from cqc_lem.app.run_content_plan import _carousel_slides_are_real_images
        assert _carousel_slides_are_real_images(slides) is True


class TestPostMissingRequiredAsset:
    def test_video_without_url_is_missing(self):
        from cqc_lem.app.run_content_plan import _post_missing_required_asset
        assert _post_missing_required_asset(1, "video", None) is True

    def test_video_with_url_is_fine(self):
        from cqc_lem.app.run_content_plan import _post_missing_required_asset
        assert _post_missing_required_asset(1, "video", "https://x/v.mp4") is False

    def test_carousel_with_real_slides_is_fine(self):
        from cqc_lem.app.run_content_plan import _post_missing_required_asset
        with patch("cqc_lem.utilities.db.get_post_carousel_slides",
                   return_value='["https://x/s1.png"]'):
            assert _post_missing_required_asset(1, "carousel", None) is False

    def test_carousel_with_text_slides_is_missing(self):
        from cqc_lem.app.run_content_plan import _post_missing_required_asset
        with patch("cqc_lem.utilities.db.get_post_carousel_slides",
                   return_value='["Title only", "Point"]'):
            assert _post_missing_required_asset(1, "carousel", None) is True

    def test_text_post_never_missing(self):
        from cqc_lem.app.run_content_plan import _post_missing_required_asset
        assert _post_missing_required_asset(1, "text", None) is False


class TestApplyAiDisclosure:
    def test_appends_disclosure_when_enabled(self):
        import cqc_lem.app.run_content_plan as rcp
        with patch.object(rcp, "AI_DISCLOSURE_ENABLED", True), \
             patch.object(rcp, "AI_DISCLOSURE_TEXT", "\n\nAI-generated visuals."):
            assert rcp._apply_ai_disclosure("My post") == "My post\n\nAI-generated visuals."

    def test_idempotent_when_marker_present(self):
        import cqc_lem.app.run_content_plan as rcp
        with patch.object(rcp, "AI_DISCLOSURE_ENABLED", True), \
             patch.object(rcp, "AI_DISCLOSURE_TEXT", "\n\nAI-generated visuals."):
            once = rcp._apply_ai_disclosure("My post")
            assert rcp._apply_ai_disclosure(once) == once

    def test_disabled_returns_unchanged(self):
        import cqc_lem.app.run_content_plan as rcp
        with patch.object(rcp, "AI_DISCLOSURE_ENABLED", False):
            assert rcp._apply_ai_disclosure("My post") == "My post"

    def test_empty_content_passthrough(self):
        import cqc_lem.app.run_content_plan as rcp
        with patch.object(rcp, "AI_DISCLOSURE_ENABLED", True):
            assert rcp._apply_ai_disclosure("") == ""


class TestPremiumTierForQuality:
    def test_premium_maps_to_premium_model_with_audio(self):
        import cqc_lem.app.run_content_plan as rcp
        model, credits, audio = rcp._premium_tier_for_quality("premium")
        assert model == rcp.PREMIUM_VIDEO_MODEL
        assert credits == rcp.PREMIUM_VIDEO_CREDITS
        assert audio is True

    def test_premium_top(self):
        import cqc_lem.app.run_content_plan as rcp
        model, credits, audio = rcp._premium_tier_for_quality("premium_top")
        assert model == rcp.PREMIUM_TOP_VIDEO_MODEL
        assert credits == rcp.PREMIUM_TOP_VIDEO_CREDITS
        assert audio is True

    def test_standard_returns_none(self):
        import cqc_lem.app.run_content_plan as rcp
        assert rcp._premium_tier_for_quality("standard") is None


class TestSaveContentPlan:
    def test_inserts_each_planned_post_with_enum_type(self):
        from cqc_lem.app.run_content_plan import save_content_plan
        from cqc_lem.utilities.db import PostType
        when = _real_datetime(2026, 7, 10, 15, 0)
        plan = [{"scheduled_datetime": when, "post_type": "carousel", "stage": "awareness"},
                {"scheduled_datetime": when, "post_type": "text", "stage": "decision"}]
        with patch(f"{_RCP}.insert_planned_post") as ins:
            save_content_plan(3, plan)
        assert ins.call_args_list[0][0] == (3, when, PostType.CAROUSEL, "awareness")
        assert ins.call_args_list[1][0] == (3, when, PostType.TEXT, "decision")


class TestWeeklyWindowSelection:
    def test_saturday_uses_next_week_plan(self, monkeypatch):
        monkeypatch.setattr(f"{_RCP}.datetime", _SaturdayDatetime)
        from cqc_lem.app.run_content_plan import auto_create_weekly_content
        with patch(f"{_RCP}.get_planned_posts_for_next_week", return_value=[]) as nxt, \
             patch(f"{_RCP}.get_planned_posts_for_current_week") as cur:
            auto_create_weekly_content(user_id=1)
        nxt.assert_called_once_with(1)
        cur.assert_not_called()


class TestAutoCreateWeeklyContentVideoPath:
    def _post(self):
        return [{"user_id": 1, "id": 42, "post_type": "video", "buyer_stage": "awareness"}]

    def test_ai_video_saved_signed_and_disclosed(self, monkeypatch, tmp_path):
        monkeypatch.setattr(f"{_RCP}.datetime", _MondayDatetime)
        import cqc_lem.app.run_content_plan as rcp
        video_file = tmp_path / "clip.mp4"
        video_file.write_bytes(b"v")
        with patch(f"{_RCP}.get_planned_posts_for_current_week", return_value=self._post()), \
             patch(f"{_RCP}.create_content", return_value=("Post text", "http://runway/clip.mp4")), \
             patch(f"{_RCP}.create_folder_if_not_exists"), \
             patch(f"{_RCP}.save_video_url_to_dir", return_value=str(video_file)) as save, \
             patch("cqc_lem.utilities.c2pa_helper.add_ai_content_credentials") as c2pa, \
             patch(f"{_RCP}.update_db_post_video_url") as upd_video, \
             patch(f"{_RCP}.update_db_post_content") as upd_content, \
             patch(f"{_RCP}.update_db_post_status") as upd_status, \
             patch(f"{_RCP}.get_user_preferences", return_value={"auto_schedule_posts": True}), \
             patch(f"{_RCP}._post_missing_required_asset", return_value=False), \
             patch(f"{_RCP}.get_post_authenticity_score", return_value=None), \
             patch.object(rcp, "AI_DISCLOSURE_ENABLED", True), \
             patch.object(rcp, "AI_DISCLOSURE_TEXT", "\n\nAI visuals."):
            rcp.auto_create_weekly_content(user_id=1)
        save.assert_called_once()
        assert save.call_args[0][0] == "http://runway/clip.mp4"
        c2pa.assert_called_once_with(str(video_file))
        assert "file_name=videos/runwayml/clip.mp4" in upd_video.call_args[0][1]
        # AI disclosure folded into the caption
        assert upd_content.call_args[0] == (42, "Post text\n\nAI visuals.")
        from cqc_lem.utilities.db import PostStatus
        assert upd_status.call_args[0] == (42, PostStatus.APPROVED)

    def test_stock_video_skips_c2pa_and_disclosure(self, monkeypatch, tmp_path):
        monkeypatch.setattr(f"{_RCP}.datetime", _MondayDatetime)
        import cqc_lem.app.run_content_plan as rcp
        video_file = tmp_path / "stock.mp4"
        video_file.write_bytes(b"v")
        with patch(f"{_RCP}.get_planned_posts_for_current_week", return_value=self._post()), \
             patch(f"{_RCP}.create_content", return_value=("Post text", "/local/pexels/stock.mp4")), \
             patch(f"{_RCP}.create_folder_if_not_exists"), \
             patch(f"{_RCP}.save_video_url_to_dir", return_value=str(video_file)), \
             patch("cqc_lem.utilities.c2pa_helper.add_ai_content_credentials") as c2pa, \
             patch(f"{_RCP}.update_db_post_video_url"), \
             patch(f"{_RCP}.update_db_post_content") as upd_content, \
             patch(f"{_RCP}.update_db_post_status"), \
             patch(f"{_RCP}.get_user_preferences", return_value={"auto_schedule_posts": True}), \
             patch(f"{_RCP}._post_missing_required_asset", return_value=False), \
             patch(f"{_RCP}.get_post_authenticity_score", return_value=None), \
             patch.object(rcp, "AI_DISCLOSURE_ENABLED", True), \
             patch.object(rcp, "AI_DISCLOSURE_TEXT", "\n\nAI visuals."):
            rcp.auto_create_weekly_content(user_id=1)
        c2pa.assert_not_called()
        assert upd_content.call_args[0] == (42, "Post text")

    def test_missing_asset_holds_post_pending(self, monkeypatch):
        monkeypatch.setattr(f"{_RCP}.datetime", _MondayDatetime)
        import cqc_lem.app.run_content_plan as rcp
        from cqc_lem.utilities.db import PostStatus
        with patch(f"{_RCP}.get_planned_posts_for_current_week", return_value=self._post()), \
             patch(f"{_RCP}.create_content", return_value=("Post text", None)), \
             patch(f"{_RCP}.update_db_post_content"), \
             patch(f"{_RCP}.update_db_post_status") as upd_status, \
             patch(f"{_RCP}.get_user_preferences", return_value={"auto_schedule_posts": True}), \
             patch(f"{_RCP}._post_missing_required_asset", return_value=True):
            rcp.auto_create_weekly_content(user_id=1)
        assert upd_status.call_args[0] == (42, PostStatus.PENDING)

    def test_generation_exception_skips_post(self, monkeypatch):
        monkeypatch.setattr(f"{_RCP}.datetime", _MondayDatetime)
        import cqc_lem.app.run_content_plan as rcp
        with patch(f"{_RCP}.get_planned_posts_for_current_week", return_value=self._post()), \
             patch(f"{_RCP}.create_content", side_effect=RuntimeError("AI down")), \
             patch(f"{_RCP}.update_db_post_content") as upd_content:
            rcp.auto_create_weekly_content(user_id=1)
        upd_content.assert_not_called()


class TestRegenerateVideoForPost:
    def test_no_content_returns_none(self):
        from cqc_lem.app.run_content_plan import regenerate_video_for_post
        with patch(f"{_RCP}.get_post_content", return_value=None):
            assert regenerate_video_for_post(9) is None

    def test_generation_failure_returns_none(self):
        from cqc_lem.app.run_content_plan import regenerate_video_for_post
        with patch(f"{_RCP}.get_post_content", return_value="text"), \
             patch("cqc_lem.utilities.db.get_post_user_id", return_value=1), \
             patch(f"{_RCP}.load_profile_for_user", return_value=MagicMock()), \
             patch(f"{_RCP}._generate_video_src", return_value=None):
            assert regenerate_video_for_post(9) is None

    def test_success_signs_ai_video_and_updates_db(self, tmp_path):
        from cqc_lem.app.run_content_plan import regenerate_video_for_post
        video_file = tmp_path / "new.mp4"
        video_file.write_bytes(b"v")
        with patch(f"{_RCP}.get_post_content", return_value="text"), \
             patch("cqc_lem.utilities.db.get_post_user_id", return_value=1), \
             patch(f"{_RCP}.load_profile_for_user", return_value=MagicMock()), \
             patch(f"{_RCP}._generate_video_src", return_value="http://runway/new.mp4"), \
             patch(f"{_RCP}.create_folder_if_not_exists"), \
             patch(f"{_RCP}.save_video_url_to_dir", return_value=str(video_file)), \
             patch("cqc_lem.utilities.c2pa_helper.add_ai_content_credentials") as c2pa, \
             patch("cqc_lem.utilities.db.get_post_status", return_value="planning"), \
             patch(f"{_RCP}.update_db_post_status"), \
             patch(f"{_RCP}.update_db_post_video_url") as upd:
            url = regenerate_video_for_post(9)
        assert url is not None and "file_name=videos/runwayml/new.mp4" in url
        c2pa.assert_called_once_with(str(video_file))
        upd.assert_called_once_with(9, url)

    def test_heals_planning_post_to_approved_on_success(self, tmp_path):
        from cqc_lem.app.run_content_plan import regenerate_video_for_post
        from cqc_lem.utilities.db import PostStatus
        video_file = tmp_path / "new.mp4"
        video_file.write_bytes(b"v")
        with patch(f"{_RCP}.get_post_content", return_value="text"), \
             patch("cqc_lem.utilities.db.get_post_user_id", return_value=1), \
             patch(f"{_RCP}.load_profile_for_user", return_value=MagicMock()), \
             patch(f"{_RCP}._generate_video_src", return_value="http://runway/new.mp4"), \
             patch(f"{_RCP}.create_folder_if_not_exists"), \
             patch(f"{_RCP}.save_video_url_to_dir", return_value=str(video_file)), \
             patch("cqc_lem.utilities.c2pa_helper.add_ai_content_credentials"), \
             patch("cqc_lem.utilities.db.get_post_status", return_value="planning"), \
             patch(f"{_RCP}.update_db_post_status") as mock_status, \
             patch(f"{_RCP}.update_db_post_video_url"):
            regenerate_video_for_post(9)
        assert PostStatus.APPROVED in [c.args[1] for c in mock_status.call_args_list]

    def test_does_not_reheal_posted_video(self, tmp_path):
        from cqc_lem.app.run_content_plan import regenerate_video_for_post
        from cqc_lem.utilities.db import PostStatus
        video_file = tmp_path / "new.mp4"
        video_file.write_bytes(b"v")
        with patch(f"{_RCP}.get_post_content", return_value="text"), \
             patch("cqc_lem.utilities.db.get_post_user_id", return_value=1), \
             patch(f"{_RCP}.load_profile_for_user", return_value=MagicMock()), \
             patch(f"{_RCP}._generate_video_src", return_value="http://runway/new.mp4"), \
             patch(f"{_RCP}.create_folder_if_not_exists"), \
             patch(f"{_RCP}.save_video_url_to_dir", return_value=str(video_file)), \
             patch("cqc_lem.utilities.c2pa_helper.add_ai_content_credentials"), \
             patch("cqc_lem.utilities.db.get_post_status", return_value=PostStatus.POSTED.value), \
             patch(f"{_RCP}.update_db_post_status") as mock_status, \
             patch(f"{_RCP}.update_db_post_video_url"):
            regenerate_video_for_post(9)
        assert PostStatus.APPROVED not in [c.args[1] for c in mock_status.call_args_list]

    def test_profile_load_failure_is_non_fatal(self, tmp_path):
        from cqc_lem.app.run_content_plan import regenerate_video_for_post
        video_file = tmp_path / "new.mp4"
        video_file.write_bytes(b"v")
        with patch(f"{_RCP}.get_post_content", return_value="text"), \
             patch("cqc_lem.utilities.db.get_post_user_id", side_effect=RuntimeError("db")), \
             patch(f"{_RCP}._generate_video_src", return_value="/local/pexels/new.mp4"), \
             patch(f"{_RCP}.create_folder_if_not_exists"), \
             patch(f"{_RCP}.save_video_url_to_dir", return_value=str(video_file)), \
             patch("cqc_lem.utilities.c2pa_helper.add_ai_content_credentials") as c2pa, \
             patch("cqc_lem.utilities.db.get_post_status", return_value="planning"), \
             patch(f"{_RCP}.update_db_post_status"), \
             patch(f"{_RCP}.update_db_post_video_url"):
            assert regenerate_video_for_post(9) is not None
        c2pa.assert_not_called()  # Pexels stock never gets AI credentials


class TestRegenerateCarouselTask:
    def test_no_user_returns_none(self):
        from cqc_lem.app.run_content_plan import regenerate_post_carousel_task
        with patch("cqc_lem.utilities.db.get_post_user_id", return_value=None):
            assert regenerate_post_carousel_task(9) is None

    def test_heals_errored_post_back_to_approved(self):
        from cqc_lem.app.run_content_plan import regenerate_post_carousel_task
        from cqc_lem.utilities.db import PostStatus
        with patch("cqc_lem.utilities.db.get_post_user_id", return_value=1), \
             patch("cqc_lem.utilities.db.get_post_buyer_stage", return_value=None), \
             patch(f"{_RCP}.create_carousel_content", return_value="caption") as gen, \
             patch("cqc_lem.utilities.db.update_db_post_content") as upd_content, \
             patch("cqc_lem.utilities.db.get_post_carousel_slides",
                   return_value='["https://x/s1.png"]'), \
             patch("cqc_lem.utilities.db.get_post_status", return_value="error"), \
             patch("cqc_lem.utilities.db.update_db_post_status") as upd_status:
            assert regenerate_post_carousel_task(9) == "caption"
        gen.assert_called_once_with(1, "awareness", 9)  # None stage defaults to awareness
        upd_content.assert_called_once_with(9, "caption")
        upd_status.assert_called_once_with(9, PostStatus.APPROVED)

    def test_posted_post_is_not_reapproved(self):
        from cqc_lem.app.run_content_plan import regenerate_post_carousel_task
        with patch("cqc_lem.utilities.db.get_post_user_id", return_value=1), \
             patch("cqc_lem.utilities.db.get_post_buyer_stage", return_value="decision"), \
             patch(f"{_RCP}.create_carousel_content", return_value="caption"), \
             patch("cqc_lem.utilities.db.update_db_post_content"), \
             patch("cqc_lem.utilities.db.get_post_carousel_slides",
                   return_value='["https://x/s1.png"]'), \
             patch("cqc_lem.utilities.db.get_post_status", return_value="posted"), \
             patch("cqc_lem.utilities.db.update_db_post_status") as upd_status:
            regenerate_post_carousel_task(9)
        upd_status.assert_not_called()


class TestFetchContent:
    def _session(self, response=None, exc=None):
        session = MagicMock()
        if exc is not None:
            session.get.side_effect = exc
        else:
            session.get.return_value = response
        return session

    def test_returns_response_content(self):
        from cqc_lem.app.run_content_plan import fetch_content
        resp = MagicMock()
        resp.content = b"<html>ok</html>"
        with patch(f"{_RCP}.get_session_for_response",
                   return_value=(self._session(resp), {})):
            assert fetch_content("https://x.com") == b"<html>ok</html>"

    @pytest.mark.parametrize("exc", [
        requests.exceptions.HTTPError("404"),
        requests.exceptions.ConnectionError("refused"),
        requests.exceptions.Timeout("slow"),
        requests.exceptions.RequestException("other"),
    ])
    def test_returns_none_on_request_errors(self, exc):
        from cqc_lem.app.run_content_plan import fetch_content
        with patch(f"{_RCP}.get_session_for_response",
                   return_value=(self._session(exc=exc), {})):
            assert fetch_content("https://x.com") is None


class TestGetSessionForResponse:
    def test_returns_session_with_retries_and_browser_headers(self):
        from cqc_lem.app.run_content_plan import get_session_for_response
        session, headers = get_session_for_response()
        assert "Mozilla" in headers["User-Agent"]
        adapter = session.get_adapter("https://example.com")
        assert adapter.max_retries.total == 5
        assert 429 in adapter.max_retries.status_forcelist


_SITEMAP_URLSET = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://x.com/old-post</loc><lastmod>2020-01-01</lastmod></url>
  <url><loc>https://x.com/new-post</loc><lastmod>2026-01-01</lastmod></url>
  <url><loc>https://x.com/undated</loc></url>
</urlset>"""

_SITEMAP_INDEX = b"""<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://x.com/post-sitemap.xml</loc></sitemap>
  <sitemap><loc>https://x.com/sitemap-misc.xml</loc></sitemap>
  <sitemap><loc>https://x.com/category-sitemap.xml</loc></sitemap>
</sitemapindex>"""


class TestFetchSitemapUrls:
    def test_urls_sorted_by_lastmod_desc(self):
        from cqc_lem.app.run_content_plan import fetch_sitemap_urls
        with patch(f"{_RCP}.fetch_content", return_value=_SITEMAP_URLSET):
            urls = fetch_sitemap_urls("https://x.com/sitemap.xml")
        assert urls == ["https://x.com/new-post", "https://x.com/old-post",
                        "https://x.com/undated"]

    def test_recurses_into_sub_sitemaps_skipping_noise(self):
        from cqc_lem.app.run_content_plan import fetch_sitemap_urls
        with patch(f"{_RCP}.fetch_content",
                   side_effect=[_SITEMAP_INDEX, _SITEMAP_URLSET]) as fc:
            urls = fetch_sitemap_urls("https://x.com/sitemap_index.xml")
        # Only post-sitemap.xml fetched; misc + category skipped
        fetched = [c[0][0] for c in fc.call_args_list]
        assert fetched == ["https://x.com/sitemap_index.xml", "https://x.com/post-sitemap.xml"]
        assert "https://x.com/new-post" in urls

    def test_parse_error_returns_empty(self):
        from cqc_lem.app.run_content_plan import fetch_sitemap_urls
        with patch(f"{_RCP}.fetch_content", return_value=b"not xml at all"):
            assert fetch_sitemap_urls("https://x.com/sitemap.xml") == []


class TestFilterRelevantUrls:
    def test_keyword_mode_filters_and_caps(self):
        from cqc_lem.app.run_content_plan import filter_relevant_urls
        urls = [f"https://x.com/blog/post-{i}" for i in range(15)] + ["https://x.com/cart"]
        result = filter_relevant_urls(urls, by_blog_post_check=False, max_list_size=10)
        assert len(result) == 10
        assert all("blog" in u for u in result)

    def test_blog_post_mode_uses_combined_check(self):
        from cqc_lem.app.run_content_plan import filter_relevant_urls
        with patch(f"{_RCP}.is_blog_post_combined", side_effect=lambda u: "keep" in u):
            result = filter_relevant_urls(["https://x.com/keep/1", "https://x.com/drop"])
        assert result == ["https://x.com/keep/1"]


class TestExtractPageContent:
    def test_extracts_title_and_long_paragraphs(self):
        from cqc_lem.app.run_content_plan import extract_page_content
        long_p = "x" * 60
        html = f"<html><head><title>My Page</title></head><body><p>short</p><p>{long_p}</p></body></html>"
        with patch(f"{_RCP}.fetch_content", return_value=html.encode()):
            title, content = extract_page_content("https://x.com/p")
        assert title == "My Page"
        assert content == [long_p]

    def test_request_error_returns_none_pair(self):
        from cqc_lem.app.run_content_plan import extract_page_content
        with patch(f"{_RCP}.fetch_content",
                   side_effect=requests.exceptions.RequestException("bad")):
            assert extract_page_content("https://x.com/p") == (None, None)


class TestScrapeRecentPosts:
    def test_extracts_articles_with_absolute_and_relative_links(self):
        from cqc_lem.app.run_content_plan import scrape_recent_posts
        listing = (b"<html><body>"
                   b'<article><a href="https://x.com/p/1">One</a></article>'
                   b'<article><a href="/p/2">Two</a></article>'
                   b"</body></html>")
        with patch(f"{_RCP}.fetch_content",
                   side_effect=[listing, b"post one html", b"post two html"]):
            posts = scrape_recent_posts("https://x.com/blog")
        assert posts == [
            {"link": "https://x.com/p/1", "content": b"post one html"},
            {"link": "https://x.com/p/2", "content": b"post two html"},
        ]

    def test_no_articles_returns_empty(self):
        from cqc_lem.app.run_content_plan import scrape_recent_posts
        with patch(f"{_RCP}.fetch_content", return_value=b"<html><body></body></html>"):
            assert scrape_recent_posts("https://x.com/blog") == []


class TestProcessSelectedPost:
    def test_handles_list_content_and_missing_values(self):
        from cqc_lem.app.run_content_plan import process_selected_post
        with patch(f"{_RCP}.myprint") as log:
            # List content is joined; a falsy url gets the placeholder.
            process_selected_post(None, ["part one", "part two"])
            logged = " | ".join(str(c.args[0]) for c in log.call_args_list)
            assert "Could not retrieve URL" in logged
            assert "part one part two" in logged

            log.reset_mock()
            # Falsy content gets the placeholder and never raises.
            process_selected_post("https://x.com/p", None)
            logged = " | ".join(str(c.args[0]) for c in log.call_args_list)
            assert "https://x.com/p" in logged
            assert "Could not retrieve content" in logged


class TestGenerateWebsiteContentPost:
    def test_generates_post_from_first_url_with_content(self):
        from cqc_lem.app.run_content_plan import generate_website_content_post
        with patch(f"{_RCP}.fetch_sitemap_urls", return_value=["u1"]), \
             patch(f"{_RCP}.filter_relevant_urls", return_value=["u1"]), \
             patch(f"{_RCP}.extract_page_content", return_value=("T", ["long content"])), \
             patch(f"{_RCP}.get_website_content_post_from_ai",
                   return_value="the post") as ai:
            result = generate_website_content_post("https://x.com/sitemap.xml",
                                                   MagicMock(), "awareness")
        assert result == "the post"
        assert ai.call_args[0][0] == ["long content"]
        assert ai.call_args[0][1] == "u1"

    def test_returns_none_after_exhausting_attempts(self):
        from cqc_lem.app.run_content_plan import generate_website_content_post
        with patch(f"{_RCP}.fetch_sitemap_urls", return_value=["u1", "u2", "u3"]), \
             patch(f"{_RCP}.filter_relevant_urls", return_value=["u1", "u2", "u3"]), \
             patch(f"{_RCP}.extract_page_content", return_value=("T", None)):
            assert generate_website_content_post("https://x.com/sitemap.xml",
                                                 MagicMock(), "awareness") is None

    def test_returns_none_when_no_relevant_urls(self):
        from cqc_lem.app.run_content_plan import generate_website_content_post
        with patch(f"{_RCP}.fetch_sitemap_urls", return_value=["u1"]), \
             patch(f"{_RCP}.filter_relevant_urls", return_value=[]):
            assert generate_website_content_post("https://x.com/sitemap.xml",
                                                 MagicMock(), "awareness") is None


class TestIsBlogPostByMetadata:
    def test_true_when_author_meta_present(self):
        from cqc_lem.app.run_content_plan import is_blog_post_by_metadata
        html = b'<html><head><meta name="author" content="Jane"></head></html>'
        with patch(f"{_RCP}.fetch_content", return_value=html):
            assert is_blog_post_by_metadata("https://x.com/p") is True

    def test_false_without_author_meta(self):
        from cqc_lem.app.run_content_plan import is_blog_post_by_metadata
        with patch(f"{_RCP}.fetch_content", return_value=b"<html></html>"):
            assert is_blog_post_by_metadata("https://x.com/p") is False

    def test_false_on_fetch_error(self):
        from cqc_lem.app.run_content_plan import is_blog_post_by_metadata
        with patch(f"{_RCP}.fetch_content", side_effect=RuntimeError("net")):
            assert is_blog_post_by_metadata("https://x.com/p") is False

    def test_combined_falls_back_to_metadata(self):
        from cqc_lem.app.run_content_plan import is_blog_post_combined
        with patch(f"{_RCP}.is_blog_post", return_value=False), \
             patch(f"{_RCP}.is_blog_post_by_metadata", return_value=True):
            assert is_blog_post_combined("https://x.com") is True

    def test_combined_false_when_both_fail(self):
        from cqc_lem.app.run_content_plan import is_blog_post_combined
        with patch(f"{_RCP}.is_blog_post", return_value=False), \
             patch(f"{_RCP}.is_blog_post_by_metadata", return_value=False):
            assert is_blog_post_combined("https://x.com") is False


class TestGetMainBlogUrlContentParsing:
    def test_parses_wordpress_rendered_content(self):
        from cqc_lem.app.run_content_plan import get_main_blog_url_content
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = [{"content": {"rendered": "<p>Body</p>"},
                                   "link": "https://x.com/p/1"}]
        session = MagicMock()
        session.get.return_value = resp
        with patch(f"{_RCP}.get_session_for_response", return_value=(session, {})):
            url, content = get_main_blog_url_content("https://x.com")
        assert url == "https://x.com/p/1"
        assert content == "<p>Body</p>"

    def test_json_string_content_is_parsed(self):
        from cqc_lem.app.run_content_plan import get_main_blog_url_content
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = [{"content": '{"rendered": "parsed body"}',
                                   "link": "https://x.com/p/2"}]
        session = MagicMock()
        session.get.return_value = resp
        with patch(f"{_RCP}.get_session_for_response", return_value=(session, {})):
            url, content = get_main_blog_url_content("https://x.com")
        assert content == "parsed body"

    def test_unparseable_string_content_kept_as_is(self):
        from cqc_lem.app.run_content_plan import get_main_blog_url_content
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = [{"content": "just plain text",
                                   "link": "https://x.com/p/3"}]
        session = MagicMock()
        session.get.return_value = resp
        with patch(f"{_RCP}.get_session_for_response", return_value=(session, {})):
            url, content = get_main_blog_url_content("https://x.com")
        assert content == "just plain text"
