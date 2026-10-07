"""The rules that decide what a curated post may be (docs/curated-sources.md).

Pure functions, no I/O: the political / paywall / licence / platform block gate, provenance, the
treatment rules (an activity URN is never a reshare parent; ND and unknown licences never
re-chart), the credit line, the 1-in-3 ceiling, and the little-text escaper.
"""

import json

import pytest

from cqc_lem.utilities import curated_sources as cs

pytestmark = pytest.mark.unit


def _item(**over):
    base = {"platform": cs.PLATFORM_RSS, "url": "https://openai.com/news/x",
            "title": "A practical guide to agents", "excerpt": "Teams cut handling time by 30%.",
            "author": "Jane Doe", "publisher": "OpenAI", "licence": "editorial",
            "paywalled": False}
    base.update(over)
    return base


class TestScreenCandidate:
    def test_a_clean_candidate_passes(self):
        assert cs.screen_candidate(_item()) == cs.Verdict(True)

    @pytest.mark.parametrize("text", [
        "What the midterm elections mean for AI",
        "Senator proposes new AI rules",
        "The White House issued guidance on AI",
        "Trump says AI will boom",
        "Regulators warn small businesses about chatbots",
        "The FTC weighs in on AI pricing",
        "Republicans and Democrats split on AI",
        "A culture war over AI art",
        "An executive order on automation",
    ])
    def test_political_content_is_blocked_absolutely(self, text):
        verdict = cs.screen_candidate(_item(title=text))
        assert not verdict.ok
        assert verdict.reason.startswith("political:")

    def test_the_political_filter_also_reads_author_and_url(self):
        assert cs.screen_candidate(_item(author="Senator Smith")).reason.startswith("political")
        assert not cs.screen_candidate(_item(url="https://x.com/whitehouse/status/1",
                                             title="", excerpt="a white house post")).ok

    @pytest.mark.parametrize("text", [
        "Democratize AI for every small business",   # 'democrat' inside a word
        "We advance invoicing with automation",      # 'vance' inside a word
        "Response time fell to 30 sec on average",   # lowercase 'sec' is a unit
        "Our congressional-style debate format",     # a hyphenated compound still counts
    ])
    def test_word_boundaries(self, text):
        verdict = cs.screen_candidate(_item(title=text))
        if "congressional" in text:
            assert not verdict.ok
        else:
            assert verdict.ok, verdict.reason

    @pytest.mark.parametrize("url,reason", [
        ("https://truthsocial.com/@x/posts/1", "excluded_platform:truthsocial.com"),
        ("https://www.reddit.com/r/smallbusiness/1", "excluded_platform:reddit.com"),
        ("https://old.reddit.com/r/x", "excluded_platform:reddit.com"),
        ("https://www.threads.net/@x/post/1", "excluded_platform:threads.net"),
        ("https://www.gartner.com/en/newsroom/x", "analyst_terms:gartner.com"),
    ])
    def test_excluded_platforms_and_analyst_houses(self, url, reason):
        assert cs.screen_candidate(_item(url=url)).reason == reason

    def test_a_gartner_citation_in_the_text_is_blocked(self):
        verdict = cs.screen_candidate(_item(excerpt="According to Gartner, 40% of firms..."))
        assert verdict.reason == "analyst_terms:gartner"

    def test_paywall_flag_and_markers(self):
        assert cs.screen_candidate(_item(paywalled=True)).reason == "paywall"
        verdict = cs.screen_candidate(_item(excerpt="This post is for paid subscribers only"))
        assert verdict.reason.startswith("paywall:")

    @pytest.mark.parametrize("licence", ["CC BY-NC 4.0", "cc-by-nc-sa", "cc_nc"])
    def test_non_commercial_licences_are_blocked(self, licence):
        assert cs.screen_candidate(_item(licence=licence)).reason == cs.BLOCK_LICENCE_NC

    def test_an_empty_item_does_not_crash(self):
        assert cs.screen_candidate({}).ok
        assert cs.screen_candidate(None).ok


class TestLicences:
    @pytest.mark.parametrize("raw,expected", [
        ("CC BY 4.0", cs.LICENCE_CC_BY), ("cc-by-sa", cs.LICENCE_CC_BY_SA),
        ("CC BY-ND", cs.LICENCE_CC_BY_ND), ("cc0", cs.LICENCE_PUBLIC_DOMAIN),
        ("public domain", cs.LICENCE_PUBLIC_DOMAIN), ("", cs.LICENCE_UNKNOWN),
        (None, cs.LICENCE_UNKNOWN), ("all rights reserved", cs.LICENCE_UNKNOWN),
        ("editorial", cs.LICENCE_EDITORIAL), ("cc", cs.LICENCE_CC_BY),
    ])
    def test_normalize(self, raw, expected):
        assert cs.normalize_licence(raw) == expected


class TestProvenance:
    def test_complete(self):
        assert cs.provenance_reason(_item()) is None

    @pytest.mark.parametrize("over,suffix", [
        ({"url": "ftp://x"}, "url"), ({"author": "", "publisher": ""}, "author"),
        ({"excerpt": "", "title": ""}, "snapshot"),
    ])
    def test_missing(self, over, suffix):
        assert cs.provenance_reason(_item(**over)) == f"no_provenance:{suffix}"


class TestUrnsAndTreatments:
    def test_split_post_urns_keeps_both(self):
        share, activity = cs.split_post_urns("urn:li:activity:111",
                                             "https://www.linkedin.com/feed/update/urn:li:share:222/")
        assert (share, activity) == ("urn:li:share:222", "urn:li:activity:111")
        assert cs.split_post_urns(None, "") == (None, None)

    @pytest.mark.parametrize("urn,ok", [
        ("urn:li:share:1", True), ("urn:li:ugcPost:9", True), ("urn:li:activity:1", False),
        ("", False), (None, False), ("urn:li:share:abc", False),
    ])
    def test_reshareable(self, urn, ok):
        assert cs.is_reshareable_urn(urn) is ok

    def test_reshare_needs_a_share_urn_on_a_linkedin_item(self):
        li = _item(platform=cs.PLATFORM_LINKEDIN, canonical_id="urn:li:share:5",
                   licence=cs.LICENCE_LINKEDIN_NATIVE)
        assert cs.treatment_allowed(li, cs.TREATMENT_RESHARE).ok
        assert not cs.treatment_allowed({**li, "canonical_id": "urn:li:activity:5"},
                                        cs.TREATMENT_RESHARE).ok
        assert not cs.treatment_allowed({**li, "link_only": True}, cs.TREATMENT_RESHARE).ok
        assert not cs.treatment_allowed(_item(), cs.TREATMENT_RESHARE).ok

    @pytest.mark.parametrize("licence,ok", [
        ("public_domain", True), ("cc_by", True), ("facts_only", True), ("editorial", True),
        ("cc_by_nd", False), ("unknown", False), ("linkedin_native", False),
    ])
    def test_rechart_licences(self, licence, ok):
        assert cs.treatment_allowed(_item(licence=licence), cs.TREATMENT_RECHART).ok is ok

    def test_screenshot_is_never_a_treatment(self):
        assert "screenshot" not in cs.TREATMENTS
        assert cs.treatment_allowed(_item(), "screenshot").reason == "unknown_treatment:screenshot"

    def test_link_needs_a_url(self):
        assert not cs.treatment_allowed(_item(url=""), cs.TREATMENT_LINK).ok

    def test_pick_treatment_order(self):
        li = _item(platform=cs.PLATFORM_LINKEDIN, canonical_id="urn:li:ugcPost:5",
                   licence=cs.LICENCE_LINKEDIN_NATIVE)
        assert cs.pick_treatment(li, has_chartable_figures=True) == cs.TREATMENT_RESHARE
        link_only = {**li, "canonical_id": None, "link_only": True}
        assert cs.pick_treatment(link_only, True) == cs.TREATMENT_LINK
        assert cs.pick_treatment(_item(), True) == cs.TREATMENT_RECHART
        assert cs.pick_treatment(_item(), False) == cs.TREATMENT_LINK
        assert cs.pick_treatment(_item(licence="cc_by_nd"), True) == cs.TREATMENT_LINK
        assert cs.pick_treatment(_item(url=""), False) is None
        assert cs.pick_treatment({**link_only, "url": ""}, False) is None


class TestCredit:
    @pytest.mark.parametrize("treatment", cs.TREATMENTS)
    def test_every_treatment_carries_a_source_line(self, treatment):
        body = cs.with_credit("My take on this.", _item(), treatment)
        assert "Source: Jane Doe" in body
        assert cs.has_credit(body, _item())

    def test_credit_line_shape(self):
        line = cs.credit_line(_item(), cs.TREATMENT_LINK)
        assert line == 'Source: Jane Doe, "A practical guide to agents", OpenAI.'
        assert "http" not in line

    def test_cc_rechart_notes_the_licence_and_change(self):
        line = cs.credit_line(_item(licence="cc_by"), cs.TREATMENT_RECHART)
        assert "(CC-BY, changes: re-charted)" in line

    def test_publisher_only_and_url_only(self):
        assert cs.credit_line({"publisher": "BLS"}, cs.TREATMENT_RECHART) == "Source: BLS."
        assert cs.credit_line({"url": "https://a.b/c"}, cs.TREATMENT_LINK) == "Source: https://a.b/c."

    def test_with_credit_is_idempotent_and_goes_before_hashtags(self):
        body = cs.with_credit("Body text.\n\n#AI #SMB", _item(), cs.TREATMENT_LINK)
        assert body.endswith("#AI #SMB")
        assert body.index("Source:") < body.index("#AI")
        assert cs.with_credit(body, _item(), cs.TREATMENT_LINK) == body
        assert cs.with_credit("", _item(), cs.TREATMENT_LINK).startswith("Source:")

    def test_has_credit_needs_the_name(self):
        assert not cs.has_credit("Source: someone else", _item())
        assert not cs.has_credit("no credit", _item())

    def test_chart_source_line(self):
        assert cs.chart_source_line(_item()) == "Source: OpenAI, A practical guide to agents"
        assert cs.chart_source_line({"url": "https://www.bls.gov/x"}) == "Source: bls.gov"


class TestCeiling:
    @pytest.mark.parametrize("before,after,ok", [
        ([], [], True),
        ([False, False], [False, False], True),
        ([True, False, False], [], True),     # three back is outside the window
        ([False, True], [], False),
        ([True, False], [], False),
        ([], [True], False),
        ([], [False, True], False),
        ([], [False, False, True], True),    # three ahead is outside the window
    ])
    def test_one_in_three(self, before, after, ok):
        assert cs.ceiling_allows(before, after) is ok

    def test_any_three_consecutive_hold_at_most_one(self):
        """Fill a long plan slot by slot; every window of three must hold <= 1 curated post."""
        plan: list = []
        for _ in range(30):
            plan.append(cs.ceiling_allows(plan, []))
        assert any(plan)
        for i in range(len(plan) - 2):
            assert sum(plan[i:i + 3]) <= 1

    def test_window_one_disables_the_lookaround(self):
        assert cs.ceiling_allows([True], [True], window=1) is True

    @pytest.mark.parametrize("mix,ok", [("value", True), ("authority", True), ("Authority", True),
                                        ("promo", False), (None, False), ("", False)])
    def test_slots(self, mix, ok):
        assert cs.slot_allows(mix) is ok


class TestLittleText:
    def test_every_reserved_character_is_escaped(self):
        raw = r"a\b|c{d}e@f[g]h(i)j<k>l#m*n_o~p"
        assert cs.escape_little_text(raw, keep_hashtags=False) == \
            r"a\\b\|c\{d\}e\@f\[g\]h\(i\)j\<k\>l\#m\*n\_o\~p"

    def test_hashtags_become_templates(self):
        assert cs.escape_little_text("Read this #AI_tips (now)") == \
            r"Read this {hashtag|\#|AI\_tips} \(now\)"

    def test_a_bare_hash_or_number_is_escaped_not_templated(self):
        assert cs.escape_little_text("Issue #2260 and # alone") == r"Issue \#2260 and \# alone"

    def test_none_and_plain(self):
        assert cs.escape_little_text(None) == ""
        assert cs.escape_little_text("Plain text, no specials.") == "Plain text, no specials."


class TestAllowlist:
    def test_the_shipped_proposal_loads_and_is_marked_a_proposal(self):
        data = cs.load_feed_allowlist(cs._DEFAULT_FEEDS_PATH)
        assert len(data["feeds"]) >= 15
        assert data["gov_series"]
        with open(cs._DEFAULT_FEEDS_PATH, encoding="utf-8") as fh:
            assert json.load(fh)["_status"].startswith("PROPOSAL")
        for feed in data["feeds"]:
            assert feed["url"].startswith("https://")
            assert cs.screen_candidate({"url": feed["url"], "licence": feed.get("licence")}).ok

    def test_env_override_and_missing_and_broken(self, tmp_path, monkeypatch):
        good = tmp_path / "feeds.json"
        good.write_text(json.dumps({"feeds": [{"url": "https://a.b/feed"}, {"name": "no url"}],
                                    "gov_series": [{"series_id": "X"}, {}]}))
        monkeypatch.setenv("CURATED_FEEDS_PATH", str(good))
        assert cs.feeds_config_path() == str(good)
        data = cs.load_feed_allowlist()
        assert len(data["feeds"]) == 1 and len(data["gov_series"]) == 1
        assert cs.load_feed_allowlist(str(tmp_path / "missing.json")) == {"feeds": [],
                                                                           "gov_series": []}
        bad = tmp_path / "bad.json"
        bad.write_text("{not json")
        assert cs.load_feed_allowlist(str(bad)) == {"feeds": [], "gov_series": []}
