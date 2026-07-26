"""Deep coverage tests for run_content_plan generation paths: carousel content,
create_text_post routing/refinement/shape persistence, premium video tiers, and
regenerate task wrappers."""

import os

import pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit

_RCP = "cqc_lem.app.run_content_plan"
_BLUEPRINT = {"format": "listicle", "hook_style": "question", "cta_style": "soft"}


def _profile():
    from cqc_lem.utilities.linkedin.profile import LinkedInProfile
    return LinkedInProfile(full_name="Jane Doe", job_title="CTO", company_name="Acme")


_VALID_EDU_CAROUSEL = {
    "cover": {"title": "5 AI tips"},
    "contents": [{"title": "Tip 1", "content": "Automate the boring parts"}],
    "call_to_action": {"title": "Follow for more"},
}


class TestCreateCarouselContent:
    def _run(self, stage="awareness", post_id=9, carousel_dict=None, image_side_effect=None,
             ppt_side_effect=None, prefs_exc=None, template=None):
        from cqc_lem.app.run_content_plan import create_carousel_content
        carousel_dict = carousel_dict if carousel_dict is not None else dict(_VALID_EDU_CAROUSEL)
        prefs_patch = patch(f"{_RCP}.get_engagement_preferences",
                            side_effect=prefs_exc, return_value={"tone": "warm"})
        with prefs_patch, \
             patch(f"{_RCP}.get_or_create_profile_synthesis", return_value="brief"), \
             patch("cqc_lem.utilities.ai.ai_helper.generate_carousel_content",
                   return_value=("caption", carousel_dict)), \
             patch("cqc_lem.utilities.carousel_creator.create_carousel_slide_images",
                   side_effect=image_side_effect,
                   return_value=["/a/9/slide_1.png", "/a/9/slide_2.png"]) as images, \
             patch("cqc_lem.utilities.carousel_creator.create_ppt",
                   side_effect=ppt_side_effect) as ppt, \
             patch(f"{_RCP}.update_db_post_carousel_slides") as save_slides, \
             patch(f"{_RCP}.update_db_post_status") as status:
            result = create_carousel_content(1, stage, post_id, template=template)
        return result, images, ppt, save_slides, status

    def test_awareness_renders_slides_saves_urls_and_ppt(self):
        result, images, ppt, save_slides, status = self._run()
        assert result == "caption"
        assert images.call_args[1]["template"] == "bold_listicle"
        urls = save_slides.call_args[0][1]
        assert len(urls) == 2 and "images/carousel/9/slide_1.png" in urls[0]
        ppt.assert_called_once()
        status.assert_not_called()

    def test_explicit_template_overrides_stage_pick(self):
        _, images, _, _, _ = self._run(template="story_arc")
        assert images.call_args[1]["template"] == "story_arc"

    def test_invalid_carousel_dict_flags_post_error(self):
        from cqc_lem.utilities.db import PostStatus
        result, images, _, _, status = self._run(carousel_dict={"bogus": True})
        assert result == "caption"  # caption still returned for reuse
        images.assert_not_called()
        status.assert_called_once_with(9, PostStatus.ERROR)

    def test_image_generation_failure_flags_post_error(self):
        from cqc_lem.utilities.db import PostStatus
        result, _, _, save_slides, status = self._run(
            image_side_effect=RuntimeError("Pillow failed"))
        assert result == "caption"
        save_slides.assert_not_called()
        status.assert_called_once_with(9, PostStatus.ERROR)

    def test_ppt_failure_is_non_fatal(self):
        result, _, _, save_slides, status = self._run(
            ppt_side_effect=RuntimeError("pptx broken"))
        assert result == "caption"
        save_slides.assert_called_once()
        status.assert_not_called()

    def test_prefs_load_failure_tolerated(self):
        result, _, _, _, _ = self._run(prefs_exc=RuntimeError("db down"))
        assert result == "caption"

    def test_no_post_id_skips_rendering(self):
        result, images, _, save_slides, _ = self._run(post_id=None)
        assert result == "caption"
        images.assert_not_called()
        save_slides.assert_not_called()


class _TextPostHarness:
    """Patch every collaborator of create_text_post; expose the mocks."""

    def __init__(self, **overrides):
        self.overrides = overrides

    def __enter__(self):
        self.patchers = {
            "tl": patch(f"{_RCP}.get_thought_leadership_post_from_ai",
                        return_value="TL post"),
            "news": patch(f"{_RCP}.get_industry_news_post_from_ai",
                          return_value="News post"),
            "story": patch(f"{_RCP}.get_personal_story_post_from_ai",
                           return_value="Story post"),
            "prompt": patch(f"{_RCP}.generate_engagement_prompt_post",
                            return_value="Prompt post"),
            "blog": patch(f"{_RCP}.get_blog_summary_post_from_ai",
                          return_value="Blog post"),
            "website": patch(f"{_RCP}.generate_website_content_post",
                             return_value=self.overrides.get("website_content", "Web post")),
            "prefs": patch(f"{_RCP}.get_engagement_preferences",
                           return_value={"tone": "warm"}),
            "synth": patch(f"{_RCP}.get_or_create_profile_synthesis",
                           return_value="brief"),
            "shape_hist": patch(f"{_RCP}.get_recent_post_shape_history",
                                return_value=[{"archetype": "myth_bust",
                                               "hook_style": "stat"}]),
            "blueprint": patch(f"{_RCP}.select_blueprint", return_value=dict(_BLUEPRINT)),
            "lm": patch(f"{_RCP}.get_lead_magnet_settings",
                        side_effect=self.overrides.get("lm_exc"),
                        return_value={"enabled": False, "keyword": None}),
            "lm_should": patch(f"{_RCP}.should_include_lead_magnet_cta",
                               return_value=self.overrides.get("lm_include", False)),
            "lm_directive": patch(f"{_RCP}.lead_magnet_cta_directive",
                                  return_value=self.overrides.get("lm_directive", "")),
            "blog_url": patch(f"{_RCP}.get_user_blog_url",
                              return_value=self.overrides.get("blog_url")),
            "blog_content": patch(f"{_RCP}.get_main_blog_url_content",
                                  return_value=self.overrides.get(
                                      "blog_content", (None, None))),
            "sitemap": patch(f"{_RCP}.get_user_sitemap_url",
                             return_value=self.overrides.get("sitemap_url")),
            "refine": patch(f"{_RCP}.get_ai_linked_post_refinement",
                            side_effect=lambda c, **kw: f"refined:{c}"),
            "hook": patch(f"{_RCP}.optimize_post_hook", side_effect=lambda c, **kw: c),
            "sanitize": patch(f"{_RCP}.sanitize_for_linkedin", side_effect=lambda c: c),
            "bait": patch(f"{_RCP}.strip_engagement_bait", side_effect=lambda c, **kw: c),
            "shape_save": patch(f"{_RCP}.update_db_post_shape",
                                side_effect=self.overrides.get("shape_exc")),
            # These tests exercise routing/refinement/shape persistence, not the A2 proof gate —
            # keep the stub post bodies from triggering a proof regeneration.
            "proof_env": patch.dict(os.environ, {"POST_PROOF_REGEN_ENABLED": "off"}),
            "authenticity": patch(f"{_RCP}._score_and_persist_authenticity"),
        }
        self.mocks = {k: p.start() for k, p in self.patchers.items()}
        return self.mocks

    def __exit__(self, *exc):
        for p in self.patchers.values():
            p.stop()
        return False


class TestCreateTextPost:
    def test_thought_leadership_refined_and_shape_persisted(self):
        from cqc_lem.app.run_content_plan import create_text_post
        with _TextPostHarness() as m:
            result = create_text_post(1, "awareness", post_type="thought_leadership",
                                      user_profile=_profile(), post_id=42)
        assert result == "refined:TL post"
        # The assigned shape, plus the post's verified-fact allow-list (#619) — empty here because
        # this harness gives the user no story-bank entries.
        assert m["tl"].call_args[1]["blueprint"] == {**_BLUEPRINT, "fact_anchors": []}
        m["shape_save"].assert_called_once_with(42, "listicle", "question", topic=None)

    def test_industry_news_and_personal_story_and_prompt(self):
        from cqc_lem.app.run_content_plan import create_text_post
        with _TextPostHarness() as m:
            assert create_text_post(1, "decision", post_type="industry_news",
                                    user_profile=_profile(),
                                    refine_final_post=False) == "News post"
            assert create_text_post(1, "decision", post_type="personal_story",
                                    user_profile=_profile(),
                                    refine_final_post=False) == "Story post"
            assert create_text_post(1, "decision", post_type="engagement_prompt",
                                    user_profile=_profile(),
                                    refine_final_post=False) == "Prompt post"

    def test_blog_summary_with_blog(self):
        from cqc_lem.app.run_content_plan import create_text_post
        with _TextPostHarness(blog_url="https://blog.x.com",
                              blog_content=("https://blog.x.com/p1", "post body")) as m:
            result = create_text_post(1, "awareness", post_type="blog_summary",
                                      user_profile=_profile(), refine_final_post=False)
        assert result == "Blog post"
        assert m["blog"].call_args[0][:2] == ("https://blog.x.com/p1", "post body")

    def test_blog_summary_without_blog_falls_back_to_other_type(self):
        from cqc_lem.app.run_content_plan import create_text_post
        with _TextPostHarness() as m, \
             patch(f"{_RCP}.random.choice", return_value="thought_leadership"):
            result = create_text_post(1, "awareness", post_type="blog_summary",
                                      user_profile=_profile(), refine_final_post=False)
        assert result == "TL post"
        m["blog"].assert_not_called()

    def test_website_content_with_sitemap(self):
        from cqc_lem.app.run_content_plan import create_text_post
        with _TextPostHarness(sitemap_url="https://x.com/sitemap.xml") as m:
            result = create_text_post(1, "awareness", post_type="website_content",
                                      user_profile=_profile(), refine_final_post=False)
        assert result == "Web post"
        assert m["website"].call_args[0][0] == "https://x.com/sitemap.xml"

    def test_website_content_empty_result_falls_back(self):
        from cqc_lem.app.run_content_plan import create_text_post
        with _TextPostHarness(sitemap_url="https://x.com/sitemap.xml",
                              website_content=None), \
             patch(f"{_RCP}.random.choice", return_value="personal_story"):
            result = create_text_post(1, "awareness", post_type="website_content",
                                      user_profile=_profile(), refine_final_post=False)
        assert result == "Story post"

    def test_website_content_without_sitemap_falls_back(self):
        from cqc_lem.app.run_content_plan import create_text_post
        with _TextPostHarness() as m, \
             patch(f"{_RCP}.random.choice", return_value="industry_news"):
            result = create_text_post(1, "awareness", post_type="website_content",
                                      user_profile=_profile(), refine_final_post=False)
        assert result == "News post"
        m["website"].assert_not_called()

    def test_random_type_chosen_when_none(self):
        from cqc_lem.app.run_content_plan import create_text_post
        with _TextPostHarness(), \
             patch(f"{_RCP}.random.choice", return_value="thought_leadership") as choice:
            result = create_text_post(1, "awareness", user_profile=_profile(),
                                      refine_final_post=False)
        assert result == "TL post"
        choice.assert_called()

    def test_scrapes_profile_via_selenium_when_missing(self):
        from cqc_lem.app.run_content_plan import create_text_post
        driver, wait = MagicMock(), MagicMock()
        with _TextPostHarness(), \
             patch(f"{_RCP}.get_user_password_pair_by_id",
                   return_value=("a@x.com", "pw")), \
             patch(f"{_RCP}.get_driver_wait_pair", return_value=(driver, wait)), \
             patch(f"{_RCP}.get_my_profile", return_value=_profile()) as gmp, \
             patch(f"{_RCP}.quit_gracefully") as quit_g:
            result = create_text_post(1, "awareness", post_type="thought_leadership",
                                      refine_final_post=False)
        assert result == "TL post"
        gmp.assert_called_once()
        quit_g.assert_called_once_with(driver)

    def test_profile_scrape_failure_uses_neutral_fallback(self):
        # Configurability audit: scrape failure first tries the cached DB profile
        # (load_profile_for_user); only when that's ALSO unavailable does it fall back to a
        # neutral placeholder — never the old fake "John Doe / ABC Inc." persona, which leaked
        # into prompts as if it were the user's identity.
        from cqc_lem.app.run_content_plan import create_text_post
        driver, wait = MagicMock(), MagicMock()
        with _TextPostHarness() as m, \
             patch(f"{_RCP}.get_user_password_pair_by_id",
                   return_value=("a@x.com", "pw")), \
             patch(f"{_RCP}.get_driver_wait_pair", return_value=(driver, wait)), \
             patch(f"{_RCP}.get_my_profile", side_effect=RuntimeError("selenium down")), \
             patch(f"{_RCP}.load_profile_for_user", return_value=None), \
             patch(f"{_RCP}.quit_gracefully") as quit_g:
            result = create_text_post(1, "awareness", post_type="thought_leadership",
                                      refine_final_post=False)
        assert result == "TL post"
        assert m["tl"].call_args[0][0].full_name == "LinkedIn Member"  # neutral, not a fake persona
        quit_g.assert_called_once_with(driver)

    def test_lead_magnet_cta_included_on_rotation(self):
        from cqc_lem.app.run_content_plan import create_text_post
        with _TextPostHarness(lm_include=True,
                              lm_directive="Comment GUIDE and I'll DM it") as m:
            create_text_post(1, "awareness", post_type="thought_leadership",
                             user_profile=_profile(), refine_final_post=False, post_id=5)
        assert m["tl"].call_args[1]["lead_magnet_cta"] == "Comment GUIDE and I'll DM it"

    def test_lead_magnet_settings_failure_uses_empty_cta(self):
        from cqc_lem.app.run_content_plan import create_text_post
        with _TextPostHarness(lm_exc=RuntimeError("db down")) as m:
            create_text_post(1, "awareness", post_type="thought_leadership",
                             user_profile=_profile(), refine_final_post=False)
        assert m["tl"].call_args[1]["lead_magnet_cta"] == ""

    def test_shape_persist_failure_still_returns_content(self):
        from cqc_lem.app.run_content_plan import create_text_post
        with _TextPostHarness(shape_exc=RuntimeError("db down")):
            result = create_text_post(1, "awareness", post_type="thought_leadership",
                                      user_profile=_profile(), post_id=42,
                                      refine_final_post=False)
        assert result == "TL post"


class TestGenerateVideoSrcPremium:
    def _run(self, avatar, create_video_return="https://runway/v.mp4"):
        import cqc_lem.app.run_content_plan as rcp
        with patch("cqc_lem.utilities.db.get_post_video_quality", return_value="premium"), \
             patch("cqc_lem.utilities.db.get_video_credit_balance", return_value=10), \
             patch("cqc_lem.utilities.db.deduct_video_credits", return_value=True), \
             patch("cqc_lem.utilities.db.refund_video_credits") as refund, \
             patch("cqc_lem.utilities.db.get_active_avatar", return_value=avatar), \
             patch("cqc_lem.utilities.ai.video_models.is_premium", return_value=True), \
             patch(f"{_RCP}.get_flux_image_prompt_from_ai", return_value="image prompt"), \
             patch(f"{_RCP}.get_runway_ml_video_prompt_from_ai", return_value="motion"), \
             patch("cqc_lem.utilities.ai.ai_helper.generate_post_image",
                   return_value="/tmp/avatar.png") as gen_img, \
             patch(f"{_RCP}.create_runway_video",
                   return_value=create_video_return) as create_video, \
             patch("cqc_lem.utilities.pexels_helper.download_pexels_video",
                   return_value="/local/pexels.mp4") as pexels, \
             patch(f"{_RCP}.create_folder_if_not_exists"):
            result = rcp._generate_video_src(1, "text content", _profile(), post_id=9)
        return result, gen_img, create_video, refund, pexels

    def test_premium_with_avatar_uses_avatar_image(self):
        avatar = {"status": "succeeded", "model_ref": "owner/m:v1"}
        result, gen_img, create_video, refund, _ = self._run(avatar)
        assert result == "https://runway/v.mp4"
        gen_img.assert_called_once_with("image prompt", 1, ratio="9:16")
        assert create_video.call_args[0][0] == "/tmp/avatar.png"
        assert create_video.call_args[1]["audio"] is True
        refund.assert_not_called()

    def test_premium_without_avatar_uses_text_to_video(self):
        result, gen_img, create_video, _, _ = self._run(avatar=None)
        assert result == "https://runway/v.mp4"
        gen_img.assert_not_called()
        assert create_video.call_args[0][0] is None  # text->video

    def test_no_video_output_refunds_and_falls_back_to_pexels(self):
        avatar = {"status": "succeeded", "model_ref": "owner/m:v1"}
        result, _, _, refund, pexels = self._run(avatar, create_video_return=None)
        assert result == "/local/pexels.mp4"
        refund.assert_called_once()
        assert refund.call_args[0][0] == 1
        pexels.assert_called_once()


class TestRegenerateTaskWrappers:
    def test_regenerate_post_video_task_delegates(self):
        from cqc_lem.app.run_content_plan import regenerate_post_video_task
        with patch(f"{_RCP}.regenerate_video_for_post", return_value="url") as regen:
            assert regenerate_post_video_task(9) == "url"
        regen.assert_called_once_with(9)

    def test_regenerate_post_task_delegates_with_guidance(self):
        from cqc_lem.app.run_content_plan import regenerate_post_task
        with patch(f"{_RCP}.regenerate_post", return_value="content") as regen:
            assert regenerate_post_task(9, guidance="shorter") == "content"
        regen.assert_called_once_with(9, "shorter")

    def test_regenerate_post_returns_none_when_generation_empty(self):
        from cqc_lem.app.run_content_plan import regenerate_post
        with patch("cqc_lem.utilities.db.get_post_user_id", return_value=1), \
             patch("cqc_lem.utilities.db.get_post_type", return_value="text"), \
             patch("cqc_lem.utilities.db.get_post_buyer_stage", return_value="awareness"), \
             patch(f"{_RCP}.load_profile_for_user", return_value=_profile()), \
             patch(f"{_RCP}.create_text_post", return_value=None):
            assert regenerate_post(9) is None

    def test_regenerate_post_unknown_type_skipped(self):
        # 'text posts only' treats an unknown/None post type as non-text — never regenerate blind.
        from cqc_lem.app.run_content_plan import regenerate_post
        with patch("cqc_lem.utilities.db.get_post_user_id", return_value=1), \
             patch("cqc_lem.utilities.db.get_post_type", return_value=None), \
             patch(f"{_RCP}.create_text_post") as ctp, \
             patch(f"{_RCP}.update_db_post_status") as us:
            assert regenerate_post(9) is None
        ctp.assert_not_called()
        us.assert_not_called()

    def test_regenerate_post_guidance_failure_keeps_base_regen(self):
        from cqc_lem.app.run_content_plan import regenerate_post
        from cqc_lem.utilities.db import PostStatus
        with patch("cqc_lem.utilities.db.get_post_user_id", return_value=1), \
             patch("cqc_lem.utilities.db.get_post_type", return_value="text"), \
             patch("cqc_lem.utilities.db.get_post_buyer_stage", return_value="awareness"), \
             patch(f"{_RCP}.load_profile_for_user", return_value=_profile()), \
             patch(f"{_RCP}.create_text_post", return_value="base content"), \
             patch(f"{_RCP}.get_engagement_preferences",
                   side_effect=RuntimeError("db down")), \
             patch(f"{_RCP}.update_db_post_content") as upd, \
             patch(f"{_RCP}.update_db_post_status") as status:
            assert regenerate_post(9, guidance="make it shorter") == "base content"
        upd.assert_called_once_with(9, "base content")
        status.assert_called_once_with(9, PostStatus.PENDING)

    def test_regenerate_video_c2pa_failure_is_non_fatal(self, tmp_path):
        from cqc_lem.app.run_content_plan import regenerate_video_for_post
        video_file = tmp_path / "v.mp4"
        video_file.write_bytes(b"v")
        with patch(f"{_RCP}.get_post_content", return_value="text"), \
             patch("cqc_lem.utilities.db.get_post_user_id", return_value=1), \
             patch(f"{_RCP}.load_profile_for_user", return_value=None), \
             patch(f"{_RCP}._generate_video_src", return_value="http://runway/v.mp4"), \
             patch(f"{_RCP}.create_folder_if_not_exists"), \
             patch(f"{_RCP}.save_video_url_to_dir", return_value=str(video_file)), \
             patch("cqc_lem.utilities.c2pa_helper.add_ai_content_credentials",
                   side_effect=RuntimeError("no cert")), \
             patch("cqc_lem.utilities.db.get_post_status", return_value="planning"), \
             patch(f"{_RCP}.update_db_post_status"), \
             patch(f"{_RCP}.update_db_post_video_url"):
            assert regenerate_video_for_post(9) is not None


class TestGetMainBlogUrlNonWordpressFallbacks:
    def test_non_200_status_falls_back_to_scraping(self):
        from cqc_lem.app.run_content_plan import get_main_blog_url_content
        resp = MagicMock()
        resp.status_code = 204
        resp.raise_for_status.return_value = None
        session = MagicMock()
        session.get.return_value = resp
        with patch(f"{_RCP}.get_session_for_response", return_value=(session, {})), \
             patch(f"{_RCP}.scrape_recent_posts",
                   return_value=[{"link": "https://x/p", "content": "scraped"}]) as scrape:
            url, content = get_main_blog_url_content("https://x.com")
        scrape.assert_called_once_with("https://x.com")
        assert url == "https://x/p" and content == "scraped"

    def test_generic_request_error_falls_back_to_scraping(self):
        import requests
        from cqc_lem.app.run_content_plan import get_main_blog_url_content
        session = MagicMock()
        session.get.side_effect = requests.exceptions.RequestException("odd failure")
        with patch(f"{_RCP}.get_session_for_response", return_value=(session, {})), \
             patch(f"{_RCP}.scrape_recent_posts", return_value=[]) as scrape:
            url, content = get_main_blog_url_content("https://x.com")
        scrape.assert_called_once()
        assert (url, content) == (None, None)


class TestScrapeRecentPostsErrorPath:
    def test_request_exception_returns_empty(self):
        import requests
        from cqc_lem.app.run_content_plan import scrape_recent_posts
        with patch(f"{_RCP}.fetch_content",
                   side_effect=requests.exceptions.RequestException("net down")):
            assert scrape_recent_posts("https://x.com/blog") == []
