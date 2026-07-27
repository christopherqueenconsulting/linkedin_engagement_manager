"""Every place LEM publishes an outbound link goes through build_utm_url (issue #658).

The helper's own contract is covered in test_marketing_attribution.py; this file is the other half —
proof that each SURFACE actually calls it, because an untagged link is invisible in exactly the same
way a missing one is: the signup it drives reads as `direct` and no dashboard can tell the
difference."""

import pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit

_ATTR = "cqc_lem.utilities.marketing.attribution"


def _owned(public="https://app.lem.test", signup="https://lem.test/trial", extra="lem.test"):
    return [
        patch(f"{_ATTR}.PUBLIC_BASE_URL", public),
        patch(f"{_ATTR}.BRAND_SIGNUP_URL", signup),
        patch(f"{_ATTR}.MARKETING_OWNED_DOMAINS", extra),
    ]


class _Patched:
    def __init__(self, patchers):
        self._patchers = patchers

    def __enter__(self):
        for p in self._patchers:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patchers):
            p.stop()
        return False


class TestArtifactCtaLink:
    def test_an_owned_newsletter_url_is_tagged_with_the_post_campaign(self):
        from cqc_lem.utilities.ai.content_alignment import artifact_cta_line
        newsletter = {"enabled": True, "title": "The Build Log",
                      "newsletter_url": "https://lem.test/newsletter"}
        with _Patched(_owned()):
            line = artifact_cta_line(None, newsletter, post_id=12)
        assert "utm_source=linkedin" in line
        assert "utm_campaign=post-12" in line
        assert "utm_content=post-body" in line

    def test_a_linkedin_hosted_newsletter_is_left_alone(self):
        # The mainline newsletter lives on linkedin.com — stamping our campaign params on somebody
        # else's domain reports nothing and pollutes their analytics.
        from cqc_lem.utilities.ai.content_alignment import artifact_cta_line
        url = "https://www.linkedin.com/newsletters/the-build-log-123"
        newsletter = {"enabled": True, "title": "The Build Log", "newsletter_url": url}
        with _Patched(_owned()):
            line = artifact_cta_line(None, newsletter, post_id=12)
        assert url in line and "utm_" not in line

    def test_an_enabled_newsletter_with_no_url_still_ships_the_cta(self):
        from cqc_lem.utilities.ai.content_alignment import artifact_cta_line
        with _Patched(_owned()):
            line = artifact_cta_line(None, {"enabled": True, "title": "The Build Log"}, post_id=1)
        assert line and "http" not in line


class TestFirstCommentPlacement:
    def test_a_carried_link_is_restamped_as_the_first_comment_placement(self):
        # The CTA was written assuming its link stays in the body; #392's split decides otherwise at
        # publish time, so the placement is corrected where the link actually lands.
        from cqc_lem.utilities.ai.content_alignment import first_comment_link_text
        body_link = ("https://lem.test/newsletter?utm_source=linkedin&utm_medium=social"
                     "&utm_campaign=post-12&utm_content=post_body")
        with _Patched(_owned()):
            text = first_comment_link_text([body_link], post_id=12)
        assert "utm_content=first-comment" in text
        assert "utm_content=post_body" not in text
        assert "utm_campaign=post-12" in text

    def test_an_untagged_owned_link_gets_the_full_tag_set(self):
        from cqc_lem.utilities.ai.content_alignment import first_comment_link_text
        with _Patched(_owned()):
            text = first_comment_link_text(["https://lem.test/guide"], post_id=3)
        for expected in ("utm_source=linkedin", "utm_medium=social", "utm_campaign=post-3",
                         "utm_content=first-comment"):
            assert expected in text

    def test_a_third_party_link_is_delivered_unchanged(self):
        from cqc_lem.utilities.ai.content_alignment import first_comment_link_text
        with _Patched(_owned()):
            text = first_comment_link_text(["https://www.reuters.com/tech/a"], post_id=3)
        assert "https://www.reuters.com/tech/a" in text and "utm_" not in text

    def test_no_links_is_still_an_empty_string(self):
        from cqc_lem.utilities.ai.content_alignment import first_comment_link_text
        assert first_comment_link_text([], post_id=1) == ""
        assert first_comment_link_text(["   "], post_id=1) == ""


class TestBrandSignupGoal:
    def test_the_seeded_business_goal_carries_the_tagged_url(self):
        from cqc_lem.utilities.brand_account import brand_preference_overrides
        with _Patched(_owned()):
            overrides = brand_preference_overrides({}, "P0")
        assert "utm_source=linkedin" in overrides["business_goals"]
        assert "utm_campaign=brand-profile" in overrides["business_goals"]


class TestTutorialDescription:
    def test_a_trial_cta_is_appended_with_the_flow_campaign(self):
        from cqc_lem.utilities.marketing.video_tutorials import TUTORIAL_FLOWS, description_with_cta
        flow = next(iter(TUTORIAL_FLOWS.values()))
        with _Patched(_owned()):
            out = description_with_cta("Here is how it works.", flow)
        assert "utm_source=youtube" in out
        assert "utm_medium=video" in out
        assert f"utm_campaign=tutorial-{flow.key.replace('_', '-')}" in out

    def test_an_owned_link_already_in_the_description_is_tagged_in_place(self):
        from cqc_lem.utilities.marketing.video_tutorials import TUTORIAL_FLOWS, description_with_cta
        flow = next(iter(TUTORIAL_FLOWS.values()))
        with _Patched(_owned()):
            out = description_with_cta("Docs: https://app.lem.test/docs", flow)
        assert "https://app.lem.test/docs?utm_source=youtube" in out

    def test_the_cta_is_not_stacked_when_the_signup_link_is_already_there(self):
        from cqc_lem.utilities.marketing.video_tutorials import TUTORIAL_FLOWS, description_with_cta
        flow = next(iter(TUTORIAL_FLOWS.values()))
        with _Patched(_owned()):
            out = description_with_cta("Start free: https://lem.test/trial", flow)
        assert out.count("https://lem.test/trial") == 1

    def test_no_signup_url_configured_leaves_the_description_alone(self):
        from cqc_lem.utilities.marketing.video_tutorials import TUTORIAL_FLOWS, description_with_cta
        flow = next(iter(TUTORIAL_FLOWS.values()))
        with _Patched(_owned(signup="", public="", extra="")):
            assert description_with_cta("Plain description.", flow) == "Plain description."

    def test_an_over_long_description_never_chops_the_cta_url(self):
        # The cap belongs on the PROSE. Truncating the finished string would cut the trial link
        # mid-URL and publish a broken one, which is strictly worse than publishing no CTA.
        from cqc_lem.utilities.marketing.video_tutorials import (DESCRIPTION_MAX_CHARS,
                                                                 TUTORIAL_FLOWS,
                                                                 description_with_cta)
        flow = next(iter(TUTORIAL_FLOWS.values()))
        with _Patched(_owned()):
            out = description_with_cta("word " * 2000, flow)
        assert len(out) <= DESCRIPTION_MAX_CHARS
        assert out.endswith("utm_content=video-description")

    def test_a_cap_too_small_for_the_cta_drops_it_whole(self):
        from cqc_lem.utilities.marketing.video_tutorials import TUTORIAL_FLOWS, description_with_cta
        flow = next(iter(TUTORIAL_FLOWS.values()))
        with _Patched(_owned()):
            out = description_with_cta("Plain description.", flow, max_chars=20)
        assert out == "Plain description."[:20]
        assert "utm_" not in out


class TestNewsletterEditionBody:
    def test_an_owned_link_in_an_edited_edition_is_tagged_per_edition(self):
        from cqc_lem.app.run_automation import _tagged_edition_body
        with _Patched(_owned()):
            out = _tagged_edition_body("More at https://lem.test/guide", edition_id=4)
        assert "utm_source=newsletter" in out
        assert "utm_campaign=newsletter-4" in out

    def test_a_link_free_edition_is_returned_byte_identical(self):
        # The generator is told not to put links in an edition at all, so this is the mainline case.
        from cqc_lem.app.run_automation import _tagged_edition_body
        body = "PLAIN TEXT EDITION\n\nNo links here, by design."
        with _Patched(_owned()):
            assert _tagged_edition_body(body, edition_id=4) == body


class TestFunnelEventCarriesTheRef:
    def test_ref_reaches_posthog_and_the_person(self):
        from cqc_lem.utilities.observability import FUNNEL_SIGNUP_COMPLETED, track_funnel_event
        with patch("cqc_lem.utilities.observability.posthog", MagicMock()) as ph:
            track_funnel_event(FUNNEL_SIGNUP_COMPLETED, user_id=9,
                               attribution={"utm_source": "referral", "ref": "7"})
        props = ph.capture.call_args.kwargs["properties"]
        assert props["ref"] == "7" and props["channel"] == "referral"
        assert props["$set_once"]["initial_ref"] == "7"
