"""Collectors for curated sources: RSS/Atom parsing, BLS, LinkedIn cards, manual paste.

Every collector screens at COLLECT time: a political, paywalled or NC candidate is stored
`blocked` with its reason and never reaches the drafter's `new` list. All network and DB I/O is
mocked.
"""

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
import requests

from cqc_lem.utilities import curated_collectors as cc, curated_sources as cs

pytestmark = pytest.mark.unit

_M = "cqc_lem.utilities.curated_collectors"

RSS = """<?xml version="1.0"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/"><channel><title>Feed</title>
<item><title>Agents for invoicing</title><link>https://example.com/a</link>
<description>&lt;p&gt;Teams cut handling time by &lt;b&gt;30%&lt;/b&gt;.&lt;/p&gt;</description>
<dc:creator>Jane Doe</dc:creator><pubDate>Tue, 06 Oct 2026 10:00:00 GMT</pubDate></item>
<item><title>Senate hearing on AI</title><link>https://example.com/b</link>
<description>Lawmakers debated.</description></item>
<item><title>No link</title><link>mailto:x@y.z</link></item>
</channel></rss>"""

ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Blog</title>
<entry><title>Small models win</title><link href="https://blog.example.com/1"/>
<summary>A 3x speed-up.</summary><author><name>Sam Roe</name></author>
<updated>2026-10-05T08:00:00Z</updated></entry>
<entry><title>Alt link</title><link rel="alternate" href="https://blog.example.com/2"/>
<content>Body</content></entry>
</feed>"""

BLS = {"status": "REQUEST_SUCCEEDED", "Results": {"series": [{"seriesID": "CES0500000003", "data": [
    {"year": "2026", "period": "M08", "periodName": "August", "value": "36.12", "latest": "true"},
    {"year": "2026", "period": "M07", "periodName": "July", "value": "36.01"}]}]}}

_FEED = {"name": "Example", "url": "https://example.com/feed", "publisher": "Example Co",
         "licence": "editorial"}
_SERIES = {"series_id": "CES0500000003", "label": "Average hourly pay for private-sector employees",
           "unit": "$", "title": "Average hourly earnings"}


class TestParseFeed:
    def test_rss_items(self):
        items = cc.parse_feed(RSS, _FEED)
        assert [i["url"] for i in items] == ["https://example.com/a", "https://example.com/b"]
        first = items[0]
        assert first["excerpt"] == "Teams cut handling time by 30% ."
        assert first["author"] == "Jane Doe"
        assert first["publisher"] == "Example Co"
        assert first["licence"] == cs.LICENCE_EDITORIAL
        assert first["platform"] == cs.PLATFORM_RSS
        assert isinstance(first["published_at"], datetime)
        assert items[1]["author"] == "Example Co"     # no creator: the publisher is the author

    def test_atom_entries(self):
        items = cc.parse_feed(ATOM, {**_FEED, "licence": "CC BY 4.0"})
        assert [i["url"] for i in items] == ["https://blog.example.com/1",
                                             "https://blog.example.com/2"]
        assert items[0]["author"] == "Sam Roe"
        assert items[0]["licence"] == cs.LICENCE_CC_BY
        assert items[0]["published_at"].year == 2026

    def test_unparseable_feed_is_empty(self):
        assert cc.parse_feed("<not xml", _FEED) == []

    def test_dates(self):
        assert cc._date(None) is None
        assert cc._date("garbage") is None
        assert cc._date("2026-10-05T08:00:00").tzinfo is not None

    def test_plain_text(self):
        assert cc.plain_text(None) == ""
        assert cc.plain_text("<p>a   <i>b</i></p>", limit=3) == "a b"


class TestRecordCandidate:
    def test_clean_is_new_and_political_is_blocked_at_collect_time(self):
        with patch(f"{_M}.insert_curated_source", return_value=7) as insert:
            assert cc.record_candidate(1, {"url": "https://a.b", "title": "Tools"}) == (7, "new")
            assert cc.record_candidate(1, {"url": "https://a.b/2",
                                           "title": "Senator on AI"}) == (7, "blocked")
        assert insert.call_args_list[0].kwargs == {"status": "new", "block_reason": None}
        assert insert.call_args_list[1].kwargs["status"] == "blocked"
        assert insert.call_args_list[1].kwargs["block_reason"].startswith("political:")

    @pytest.mark.parametrize("item,reason", [
        ({"url": "https://a.b", "paywalled": True}, "paywall"),
        ({"url": "https://a.b", "licence": "cc-by-nc"}, "licence_nc"),
    ])
    def test_paywall_and_nc_never_land_new(self, item, reason):
        with patch(f"{_M}.insert_curated_source", return_value=3) as insert:
            _, status = cc.record_candidate(1, item)
        assert status == "blocked"
        assert insert.call_args.kwargs["block_reason"] == reason


class TestCollectRss:
    def test_fetches_each_feed_once_and_records_for_every_user(self):
        response = MagicMock(text=RSS, content=RSS.encode())
        response.raise_for_status.return_value = None
        with patch(f"{_M}.requests.get", return_value=response) as get, \
             patch(f"{_M}.insert_curated_source", side_effect=[1, 2, 3, None]):
            counts = cc.collect_rss([1, 2], {"feeds": [_FEED]})
        assert get.call_count == 1
        assert counts == {"feeds": 1, "items": 2, "new": 2, "blocked": 1, "failed_feeds": 0}

    def test_a_dead_feed_is_counted_not_raised(self):
        with patch(f"{_M}.requests.get", side_effect=requests.ConnectionError("down")):
            counts = cc.collect_rss([1], {"feeds": [_FEED]})
        assert counts["failed_feeds"] == 1 and counts["feeds"] == 0

    def test_an_oversized_feed_is_skipped(self):
        response = MagicMock(text="x", content=b"x" * (cc.FEED_MAX_BYTES + 1))
        response.raise_for_status.return_value = None
        with patch(f"{_M}.requests.get", return_value=response):
            assert cc._fetch("https://a.b") is None


class TestGovData:
    def test_bls_candidate_is_one_fixed_shape_sentence(self):
        item = cc.bls_candidate(_SERIES, BLS)
        assert item["excerpt"] == ("Average hourly pay for private-sector employees was $36.12 "
                                   "in August 2026.")
        assert item["licence"] == cs.LICENCE_PUBLIC_DOMAIN
        assert item["platform"] == cs.PLATFORM_GOV_DATA
        assert item["url"].endswith("CES0500000003?period=2026-M08")
        assert item["author"] == cc.BLS_PUBLISHER

    def test_percent_and_bare_units(self):
        pct = cc.bls_candidate({"series_id": "LNS14000000", "label": "The US unemployment rate",
                                "unit": "%"}, BLS)
        assert "was 36.12 percent in August 2026." in pct["excerpt"]
        bare = cc.bls_candidate({"series_id": "X"}, BLS)
        assert bare["excerpt"] == "X was 36.12 in August 2026."

    @pytest.mark.parametrize("payload", [{}, {"Results": {"series": []}},
                                         {"Results": {"series": [{"data": [
                                             {"period": "M01", "value": "-"}]}]}},
                                         {"Results": {"series": [{"data": [{"period": "A01"}]}]}}])
    def test_unusable_payloads(self, payload):
        assert cc.bls_candidate(_SERIES, payload) is None

    def test_collect_gov_data(self):
        response = MagicMock()
        response.json.return_value = BLS
        response.raise_for_status.return_value = None
        with patch(f"{_M}.requests.get", side_effect=[response, requests.Timeout("slow"),
                                                      MagicMock(json=MagicMock(return_value={}))]), \
             patch(f"{_M}.insert_curated_source", return_value=5):
            counts = cc.collect_gov_data([1], {"gov_series": [_SERIES, _SERIES, _SERIES]})
        assert counts == {"series": 1, "new": 1, "blocked": 0}


class TestLinkedin:
    def test_a_share_urn_on_the_container_is_reshareable(self):
        item = cc.linkedin_candidate("Ann Lee", "Great results.", "urn:li:share:42")
        assert item["canonical_id"] == "urn:li:share:42"
        assert item["link_only"] is False
        assert item["url"] == "https://www.linkedin.com/feed/update/urn:li:share:42/"

    def test_an_activity_urn_is_recorded_but_link_only(self):
        item = cc.linkedin_candidate("Ann Lee", "Great results.", "urn:li:activity:9")
        assert item["canonical_id"] is None
        assert item["activity_urn"] == "urn:li:activity:9"
        assert item["link_only"] is True

    def test_no_urn_or_no_content_is_no_candidate(self):
        assert cc.linkedin_candidate("A", "text", None) is None
        assert cc.linkedin_candidate("A", "  ", "urn:li:share:1") is None

    def test_the_hook_is_off_without_the_flag(self, monkeypatch):
        monkeypatch.setenv("CURATED_SOURCES_ENABLED", "false")
        with patch(f"{_M}.record_candidate") as record:
            cc.record_linkedin_candidate(1, MagicMock(), "A", "text")
        record.assert_not_called()

    def test_the_hook_records_with_the_flag(self, monkeypatch):
        monkeypatch.setenv("CURATED_SOURCES_ENABLED", "true")
        with patch("cqc_lem.utilities.linkedin.cards._feed_post_container_urn",
                   return_value="urn:li:ugcPost:5"), \
             patch(f"{_M}.record_candidate") as record:
            cc.record_linkedin_candidate(1, MagicMock(), "Ann", "Body", profile_url="https://p")
        item = record.call_args.args[1]
        assert item["canonical_id"] == "urn:li:ugcPost:5"
        assert item["profile_url"] == "https://p"

    def test_the_hook_skips_a_card_without_a_container_urn(self, monkeypatch):
        monkeypatch.setenv("CURATED_SOURCES_ENABLED", "true")
        with patch("cqc_lem.utilities.linkedin.cards._feed_post_container_urn", return_value=None), \
             patch("cqc_lem.utilities.linkedin.cards._post_permalink_from_card",
                   return_value="https://www.linkedin.com/feed/update/urn:li:share:1/"), \
             patch(f"{_M}.record_candidate") as record:
            cc.record_linkedin_candidate(1, MagicMock(), "Ann", "Body")
        record.assert_not_called()

    def test_the_hook_never_raises(self, monkeypatch):
        monkeypatch.setenv("CURATED_SOURCES_ENABLED", "true")
        with patch("cqc_lem.utilities.linkedin.cards._feed_post_container_urn",
                   side_effect=RuntimeError("stale element")):
            cc.record_linkedin_candidate(1, MagicMock(), "Ann", "Body")

    def test_flag_read_failure_is_off(self):
        with patch("cqc_lem.utilities.flags.flag_enabled", side_effect=RuntimeError("x")):
            assert cc.curated_enabled(1) is False


class TestManual:
    def test_an_x_url_keeps_the_owner_supplied_snapshot(self):
        item = cc.manual_candidate("https://x.com/someone/status/1", author="Some One",
                                   excerpt="Short take.", licence="cc-by")
        assert item["platform"] == cs.PLATFORM_MANUAL
        assert item["link_only"] is True
        assert item["licence"] == cs.LICENCE_CC_BY
        assert item["author"] == "Some One"

    @pytest.mark.parametrize("url", [
        "https://evil.com/?x=linkedin.com/feed/update/urn:li:share:77/",
        "https://linkedin.com.evil.com/feed/update/urn:li:share:77/",
        "https://evil-linkedin.com/feed/update/urn:li:share:77/",
        "https://evil.com/linkedin.com/feed/update/urn:li:share:77/",
    ])
    def test_a_hostile_url_naming_linkedin_is_never_a_linkedin_post(self, url):
        item = cc.manual_candidate(url, author="Ann")
        assert item["platform"] == cs.PLATFORM_MANUAL
        assert item["canonical_id"] is None and item["activity_urn"] is None
        assert item["link_only"] is True
        assert not cs.treatment_allowed(item, cs.TREATMENT_RESHARE).ok

    def test_a_urn_smuggled_into_a_linkedin_query_string_is_ignored(self):
        item = cc.manual_candidate("https://www.linkedin.com/in/ann?x=urn:li:share:77")
        assert item["canonical_id"] is None
        assert item["platform"] == cs.PLATFORM_MANUAL

    @pytest.mark.parametrize("url,ok", [
        ("https://www.linkedin.com/feed/", True), ("https://linkedin.com/x", True),
        ("https://evil.com/?x=linkedin.com", False), ("ftp://www.linkedin.com/x", False),
        ("https://[::1/x", False), (None, False),
    ])
    def test_is_linkedin_host(self, url, ok):
        assert cc.is_linkedin_host(url) is ok

    def test_a_pasted_linkedin_share_url_can_reshare(self):
        item = cc.manual_candidate(
            "https://www.linkedin.com/feed/update/urn:li:share:77/", author="Ann")
        assert item["platform"] == cs.PLATFORM_LINKEDIN
        assert item["canonical_id"] == "urn:li:share:77"
        assert item["licence"] == cs.LICENCE_LINKEDIN_NATIVE
        assert cs.treatment_allowed(item, cs.TREATMENT_RESHARE).ok
