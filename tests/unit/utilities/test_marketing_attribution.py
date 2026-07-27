"""Unit tests for utilities/marketing/attribution.py — UTM discipline at the source (issue #658)."""

import pytest
from unittest.mock import patch

pytestmark = pytest.mark.unit

_ATTR = "cqc_lem.utilities.marketing.attribution"


def _owned(public="https://app.lem.test", signup="https://lem.test/trial", extra="blog.lem.test"):
    """Patch the env constants the owned-host set is derived from."""
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


def _params(url):
    from urllib.parse import parse_qsl, urlparse
    return dict(parse_qsl(urlparse(url).query))


class TestBuildUtmUrl:
    def test_adds_the_three_required_params(self):
        from cqc_lem.utilities.marketing.attribution import build_utm_url
        out = build_utm_url("https://lem.test/trial", "linkedin", "social", "post-12")
        assert _params(out) == {"utm_source": "linkedin", "utm_medium": "social",
                                "utm_campaign": "post-12"}

    def test_preserves_existing_query_params(self):
        from cqc_lem.utilities.marketing.attribution import build_utm_url
        out = build_utm_url("https://lem.test/trial?plan=pro", "linkedin", "social", "post-12")
        assert _params(out)["plan"] == "pro"
        assert _params(out)["utm_source"] == "linkedin"

    def test_never_overwrites_an_existing_utm(self):
        # A hand-tagged link keeps its own attribution — and the same call run twice is a no-op,
        # which is what makes it safe to tag at more than one choke point.
        from cqc_lem.utilities.marketing.attribution import build_utm_url
        first = build_utm_url("https://lem.test/t?utm_source=mine", "linkedin", "social", "post-1")
        assert _params(first)["utm_source"] == "mine"
        assert build_utm_url(first, "linkedin", "social", "post-1") == first

    def test_blank_and_non_http_urls_come_back_untouched(self):
        from cqc_lem.utilities.marketing.attribution import build_utm_url
        for raw in ("", None, "   ", "mailto:hi@lem.test", "/relative/path", "javascript:alert(1)"):
            assert build_utm_url(raw, "linkedin", "social", "c") == (str(raw or "").strip()
                                                                     if raw else str(raw or ""))

    def test_values_are_slugged_so_a_breakdown_stays_readable(self):
        from cqc_lem.utilities.marketing.attribution import build_utm_url
        out = build_utm_url("https://lem.test/t", "LinkedIn", "First Comment", "Post 12 / Receipt")
        assert _params(out) == {"utm_source": "linkedin", "utm_medium": "first-comment",
                                "utm_campaign": "post-12-receipt"}

    def test_ref_rides_beside_the_utms(self):
        from cqc_lem.utilities.marketing.attribution import build_utm_url
        out = build_utm_url("https://lem.test/t", "referral", "referral", "member-referral", ref="7")
        assert _params(out)["ref"] == "7"

    def test_a_url_with_a_fragment_keeps_it(self):
        from cqc_lem.utilities.marketing.attribution import build_utm_url
        out = build_utm_url("https://lem.test/t#pricing", "linkedin", "social", "post-1")
        assert out.endswith("#pricing")
        assert _params(out)["utm_source"] == "linkedin"


class TestOwnedLinks:
    def test_public_base_signup_and_configured_domains_are_ours(self):
        from cqc_lem.utilities.marketing.attribution import is_owned_link
        with _Patched(_owned()):
            assert is_owned_link("https://app.lem.test/dashboard")
            assert is_owned_link("https://lem.test/trial")
            assert is_owned_link("https://blog.lem.test/post")

    def test_subdomains_of_an_owned_host_count(self):
        from cqc_lem.utilities.marketing.attribution import is_owned_link
        with _Patched(_owned()):
            assert is_owned_link("https://docs.lem.test/guide")

    def test_a_lookalike_suffix_is_not_ours(self):
        from cqc_lem.utilities.marketing.attribution import is_owned_link
        with _Patched(_owned()):
            assert not is_owned_link("https://notlem.test/trial")
            assert not is_owned_link("https://lem.test.evil.com/trial")

    def test_third_party_and_unparseable_links_are_never_ours(self):
        from cqc_lem.utilities.marketing.attribution import is_owned_link
        with _Patched(_owned()):
            for raw in ("https://www.linkedin.com/newsletter/x", "https://substack.com/x",
                        "", None, "not a url", "/relative"):
                assert not is_owned_link(raw)

    def test_nothing_is_owned_when_nothing_is_configured(self):
        # The default deployment posture: no PUBLIC_BASE_URL, no signup URL — tagging is a no-op
        # everywhere rather than a guess about which host is ours.
        from cqc_lem.utilities.marketing.attribution import is_owned_link, tag_owned_link
        with _Patched(_owned(public="", signup="", extra="")):
            assert not is_owned_link("https://lem.test/trial")
            assert tag_owned_link("https://lem.test/trial", "linkedin", "social",
                                  "post-1") == "https://lem.test/trial"

    def test_tag_owned_link_passes_third_party_links_through_untouched(self):
        from cqc_lem.utilities.marketing.attribution import tag_owned_link
        with _Patched(_owned()):
            article = "https://www.reuters.com/tech/some-article"
            assert tag_owned_link(article, "linkedin", "social", "post-1") == article

    def test_a_bare_entry_is_host_normalized_like_a_parsed_url(self):
        # `www.`, a port and a trailing path are all things an operator types into
        # MARKETING_OWNED_DOMAINS. `_host` strips them off a real URL, so an entry that keeps them
        # would never compare equal — and the domain would silently go untagged forever, which is
        # indistinguishable from the pre-#658 behaviour.
        from cqc_lem.utilities.marketing.attribution import is_owned_link, owned_hosts
        with _Patched(_owned(public="", signup="",
                             extra="www.lem.test, blog.example.org:8443, Docs.Example.NET/guide")):
            assert owned_hosts() == {"lem.test", "blog.example.org", "docs.example.net"}
            assert is_owned_link("https://www.lem.test/trial")
            assert is_owned_link("https://lem.test/trial")
            assert is_owned_link("https://blog.example.org/p")
            assert is_owned_link("https://docs.example.net/guide")
            assert not is_owned_link("https://example.net/guide")


class TestMarkPlacement:
    def test_replaces_the_placement_the_body_assumed(self):
        from cqc_lem.utilities.marketing.attribution import mark_placement
        with _Patched(_owned()):
            body = "https://lem.test/trial?utm_source=linkedin&utm_content=post_body"
            out = mark_placement(body, "first_comment")
            assert _params(out)["utm_content"] == "first-comment"
            assert _params(out)["utm_source"] == "linkedin"

    def test_leaves_third_party_links_alone(self):
        from cqc_lem.utilities.marketing.attribution import mark_placement
        with _Patched(_owned()):
            link = "https://substack.com/x?utm_content=post_body"
            assert mark_placement(link, "first_comment") == link


class TestVocabularyIsWhatLandsOnTheUrl:
    def test_placement_constants_survive_slugging_unchanged(self):
        # Every value is slugged on the way onto a URL, so a constant spelled with an underscore
        # would never equal the value a PostHog filter has to match. The constant IS the data.
        from cqc_lem.utilities.marketing.attribution import (PLACEMENT_FIRST_COMMENT,
                                                             PLACEMENT_POST_BODY,
                                                             PLACEMENT_VIDEO_DESCRIPTION,
                                                             _slug, build_utm_url)
        for placement in (PLACEMENT_POST_BODY, PLACEMENT_FIRST_COMMENT,
                          PLACEMENT_VIDEO_DESCRIPTION):
            assert _slug(placement) == placement
            out = build_utm_url("https://lem.test/t", "linkedin", "social", "post-1",
                                content=placement)
            assert _params(out)["utm_content"] == placement


class TestTagLinksInText:
    def test_rewrites_only_the_owned_links_in_a_body(self):
        from cqc_lem.utilities.marketing.attribution import tag_links_in_text
        with _Patched(_owned()):
            text = ("Reuters covered it: https://www.reuters.com/a\n"
                    "Full walkthrough: https://blog.lem.test/guide")
            out = tag_links_in_text(text, "youtube", "video", "tutorial-account")
        assert "https://www.reuters.com/a" in out
        assert "utm_source=youtube" in out
        assert out.count("utm_campaign=tutorial-account") == 1

    def test_trailing_sentence_punctuation_is_not_part_of_the_url(self):
        from cqc_lem.utilities.marketing.attribution import tag_links_in_text
        with _Patched(_owned()):
            out = tag_links_in_text("See https://lem.test/trial.", "linkedin", "social", "post-1")
        assert out.endswith(".")
        assert "utm_source=linkedin" in out

    def test_a_shorter_url_that_prefixes_a_longer_one_is_not_corrupted(self):
        from cqc_lem.utilities.marketing.attribution import tag_links_in_text
        with _Patched(_owned()):
            out = tag_links_in_text("https://lem.test/a and https://lem.test/a/b",
                                    "linkedin", "social", "post-1")
        assert "https://lem.test/a?utm_source=linkedin" in out
        assert "https://lem.test/a/b?utm_source=linkedin" in out

    def test_empty_text_is_returned_as_is(self):
        from cqc_lem.utilities.marketing.attribution import tag_links_in_text
        assert tag_links_in_text("", "linkedin", "social", "c") == ""
        assert tag_links_in_text(None, "linkedin", "social", "c") == ""


class TestCampaignNames:
    def test_post_campaign_carries_the_id_and_archetype(self):
        from cqc_lem.utilities.marketing.attribution import campaign_for_post
        assert campaign_for_post(12, "build_receipt") == "post-12-build-receipt"
        assert campaign_for_post(12) == "post-12"
        assert campaign_for_post() == "post"

    def test_edition_and_tutorial_campaigns(self):
        from cqc_lem.utilities.marketing.attribution import (campaign_for_edition,
                                                             campaign_for_tutorial)
        assert campaign_for_edition(4) == "newsletter-4"
        assert campaign_for_edition() == "newsletter"
        assert campaign_for_tutorial("account_prefs") == "tutorial-account-prefs"
        assert campaign_for_tutorial(None) == "tutorial"

    def test_campaigns_are_bounded(self):
        from cqc_lem.utilities.marketing.attribution import campaign_for_post
        assert len(campaign_for_post(1, "x" * 200)) <= 60


class TestSignupAndReferralUrls:
    def test_signup_url_is_tagged(self):
        from cqc_lem.utilities.marketing.attribution import signup_url
        with _Patched(_owned()):
            out = signup_url("linkedin", "profile", "brand-profile")
        assert out.startswith("https://lem.test/trial?")
        assert _params(out)["utm_campaign"] == "brand-profile"

    def test_no_signup_url_configured_yields_nothing_to_publish(self):
        from cqc_lem.utilities.marketing.attribution import signup_url
        with _Patched(_owned(signup="")):
            assert signup_url("linkedin", "profile", "brand-profile") == ""

    def test_referral_url_shape(self):
        from cqc_lem.utilities.marketing.attribution import referral_url
        with _Patched(_owned()):
            out = referral_url(7)
        assert _params(out) == {"utm_source": "referral", "utm_medium": "referral",
                                "utm_campaign": "member-referral", "ref": "7"}

    def test_referral_url_needs_both_a_user_and_a_landing_page(self):
        from cqc_lem.utilities.marketing.attribution import referral_url
        with _Patched(_owned()):
            assert referral_url(None) == ""
        with _Patched(_owned(signup="")):
            assert referral_url(7) == ""

    def test_referral_code_rejects_a_non_numeric_user(self):
        from cqc_lem.utilities.marketing.attribution import referral_code
        assert referral_code(7) == "7"
        assert referral_code(None) == ""
        assert referral_code("not-an-id") == ""


class TestParseReferral:
    def test_reads_the_utms_and_ref_off_a_landing_url(self):
        from cqc_lem.utilities.marketing.attribution import parse_referral
        out = parse_referral("https://lem.test/?utm_source=referral&ref=7&other=x")
        assert out == {"utm_source": "referral", "ref": "7"}

    def test_a_bare_url_carries_no_attribution(self):
        from cqc_lem.utilities.marketing.attribution import parse_referral
        assert parse_referral("https://lem.test/") == {}
        assert parse_referral("") == {}
        assert parse_referral(None) == {}
