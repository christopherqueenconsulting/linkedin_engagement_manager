"""Issue #624 — the CTA -> owned-asset map: an artifact CTA is only worth writing if something
actually arrives, so every CTA must resolve to a real asset, a real channel, and an honest
'deliverable' flag."""
from cqc_lem.utilities.ai.content_alignment import (
    ARTIFACT_CHANNEL_DM, ARTIFACT_CHANNEL_LINK, ARTIFACT_KIND_LEAD_MAGNET,
    ARTIFACT_KIND_NEWSLETTER, ARTIFACT_KIND_NONE, artifact_cta_line, resolve_artifact_delivery,
    split_link_for_first_comment)

_LM = {"enabled": True, "keyword": "AUDIT", "message": "the 12-point pipeline checklist"}
_NEWS = {"enabled": True, "title": "The Build Log", "newsletter_url": "https://li/newsletter/build"}


class TestResolveArtifactDelivery:
    def test_lead_magnet_maps_to_the_dm_channel(self):
        d = resolve_artifact_delivery(lead_magnet=_LM, newsletter=_NEWS)
        assert d["kind"] == ARTIFACT_KIND_LEAD_MAGNET
        assert d["channel"] == ARTIFACT_CHANNEL_DM
        assert d["keyword"] == "AUDIT"
        assert d["message"] == "the 12-point pipeline checklist"
        assert d["deliverable"] is True

    def test_lead_magnet_wins_over_the_newsletter(self):
        """It is the mechanic the automation already listens for, so it delivers fastest."""
        assert resolve_artifact_delivery(_LM, _NEWS)["kind"] == ARTIFACT_KIND_LEAD_MAGNET

    def test_half_configured_lead_magnet_falls_through(self):
        # Keyword but no message = an invitation to comment for a DM that never gets written.
        half = {"enabled": True, "keyword": "AUDIT", "message": ""}
        assert resolve_artifact_delivery(half, _NEWS)["kind"] == ARTIFACT_KIND_NEWSLETTER

    def test_newsletter_maps_to_the_link_channel(self):
        d = resolve_artifact_delivery(newsletter=_NEWS)
        assert d["kind"] == ARTIFACT_KIND_NEWSLETTER
        assert d["channel"] == ARTIFACT_CHANNEL_LINK
        assert d["url"] == "https://li/newsletter/build"
        assert d["label"] == "The Build Log"
        assert d["deliverable"] is True

    def test_newsletter_without_a_url_is_not_deliverable(self):
        """It can still be NAMED, but nothing arrives from it — the map must not pretend it does."""
        d = resolve_artifact_delivery(newsletter={"enabled": True, "title": "The Build Log"})
        assert d["kind"] == ARTIFACT_KIND_NEWSLETTER and d["deliverable"] is False

    def test_disabled_newsletter_is_no_asset(self):
        d = resolve_artifact_delivery(newsletter={"enabled": False, "newsletter_url": "https://x"})
        assert d["kind"] == ARTIFACT_KIND_NONE and d["channel"] is None

    def test_no_assets_at_all(self):
        d = resolve_artifact_delivery()
        assert d == {"kind": ARTIFACT_KIND_NONE, "channel": None, "label": "", "keyword": "",
                     "message": "", "url": "", "deliverable": False}


class TestArtifactCtaLine:
    def test_lead_magnet_line_carries_the_exact_keyword(self):
        line = artifact_cta_line(lead_magnet=_LM, newsletter=_NEWS, post_id=1)
        assert "AUDIT" in line and "comment" in line.lower()
        assert "http" not in line          # the DM delivers it; no link in the body

    def test_newsletter_line_carries_the_subscribe_url(self):
        """The URL IS the deliverable — without it the CTA asks the reader to go find the thing."""
        line = artifact_cta_line(newsletter=_NEWS, post_id=1)
        assert "The Build Log" in line
        assert "https://li/newsletter/build" in line

    def test_newsletter_line_without_a_url_still_reads_cleanly(self):
        line = artifact_cta_line(newsletter={"enabled": True, "title": "The Build Log"})
        assert "The Build Log" in line and "http" not in line

    def test_untitled_newsletter_is_not_named_twice(self):
        line = artifact_cta_line(newsletter={"enabled": True, "newsletter_url": "https://li/n"})
        assert line.count("my newsletter") == 1

    def test_no_asset_means_no_line(self):
        assert artifact_cta_line() == ""


class TestNewsletterLinkRidesTheFirstComment:
    """Issue #624 scope: verify the edit-in-later pattern (#392) covers ARTIFACT links too — an
    in-body link costs 19-60% reach, and the newsletter CTA is the one artifact that ships a URL."""

    def test_subscribe_url_is_split_out_of_the_body(self):
        body = "Here is the thing I learned.\n\n" + artifact_cta_line(newsletter=_NEWS, post_id=2)
        stripped, carried = split_link_for_first_comment(body)
        assert carried == ["https://li/newsletter/build"]
        assert "https://li/newsletter/build" not in stripped
        assert "The Build Log" in stripped          # the ask survives; only the URL moves

    def test_split_is_a_no_op_when_the_user_turned_it_off(self):
        body = artifact_cta_line(newsletter=_NEWS, post_id=2)
        assert split_link_for_first_comment(body, enabled=False) == (body, [])
