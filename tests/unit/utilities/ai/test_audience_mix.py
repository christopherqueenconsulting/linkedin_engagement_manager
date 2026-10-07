"""Dual-audience alternation (showcase round 5): the pure half in `utilities/ai/audience_mix.py`."""

import json

import pytest

from cqc_lem.utilities.ai import audience_mix as am

pytestmark = pytest.mark.unit

MIX = {"primary_audience": "ops and engineering leaders already running AI",
       "secondary_audience": "small-business owners", "secondary_share": 0.6,
       "secondary_focus_topics": ["cash flow", "customer response time"]}


def _replay(mix, n, history=None):
    """Pick `n` posts in a row, each seeing the audiences the earlier ones recorded."""
    history = list(history or [])
    for _ in range(n):
        history.insert(0, am.select_audience(mix, history))
    return history


class TestParse:
    def test_normalizes_bounds_and_drops_unknown_keys(self):
        mix = am.parse_audience_mix({"primary_audience": "  ops  leaders ",
                                     "secondary_audience": "x" * 900, "secondary_share": 7,
                                     "secondary_focus_topics": ["Cash flow", "cash flow", " ", 3],
                                     "junk": 1})
        assert mix == {"primary_audience": "ops leaders",
                       "secondary_audience": "x" * am.AUDIENCE_DESCRIPTION_MAX,
                       "secondary_share": 1.0, "secondary_focus_topics": ["Cash flow", "3"]}

    @pytest.mark.parametrize("raw", [None, "", "{not json", "[1, 2]", {}, {"secondary_share": 0},
                                     b"null"])
    def test_nothing_usable_is_none(self, raw):
        assert am.parse_audience_mix(raw) is None

    def test_accepts_the_column_json_and_bytes(self):
        assert am.parse_audience_mix(json.dumps(MIX)) == MIX
        assert am.parse_audience_mix(json.dumps(MIX).encode()) == MIX

    @pytest.mark.parametrize("share,expected", [("0.35", 0.35), ("nope", 0.0), (-1, 0.0),
                                                (float("nan"), 0.0)])
    def test_share_is_clamped(self, share, expected):
        mix = am.parse_audience_mix({**MIX, "secondary_share": share})
        assert mix["secondary_share"] == expected

    def test_topics_may_arrive_as_one_string_and_are_capped(self):
        mix = am.parse_audience_mix({"secondary_focus_topics": ",".join(f"t{i}" for i in range(20))})
        assert len(mix["secondary_focus_topics"]) == am.AUDIENCE_TOPICS_MAX
        assert am.parse_audience_mix({"secondary_focus_topics": 5}) is None


class TestSelectAudience:
    def test_inactive_mix_is_always_primary(self):
        for mix in (None, {**MIX, "secondary_share": 0}, {**MIX, "secondary_audience": ""}):
            assert not am.mix_active(mix)
            assert am.select_audience(mix, ["secondary"] * 3) == am.AUDIENCE_PRIMARY

    def test_a_point_six_share_alternates_six_in_ten_without_streaks(self):
        picks = list(reversed(_replay(MIX, 10)))
        assert picks.count(am.AUDIENCE_SECONDARY) == 6
        # Alternating, never a run longer than two.
        runs = "".join("S" if p == am.AUDIENCE_SECONDARY else "P" for p in picks)
        assert "SSS" not in runs and "PP" not in runs

    def test_the_share_holds_over_every_rolling_window(self):
        picks = list(reversed(_replay(MIX, 40)))
        for start in range(len(picks) - am.AUDIENCE_WINDOW + 1):
            window = picks[start:start + am.AUDIENCE_WINDOW]
            assert window.count(am.AUDIENCE_SECONDARY) == 6

    def test_unrecorded_legacy_posts_do_not_count(self):
        # Nine posts written before the setting existed: the first pick is NOT a six-post streak.
        picks = list(reversed(_replay(MIX, 4, history=[None] * 9)[:4]))
        assert picks == ["secondary", "primary", "secondary", "primary"]

    @pytest.mark.parametrize("share,expected", [(0.3, 3), (0.5, 5), (1.0, 10)])
    def test_other_shares(self, share, expected):
        picks = _replay({**MIX, "secondary_share": share}, 10)
        assert picks.count(am.AUDIENCE_SECONDARY) == expected


class TestProfileAndPrefs:
    PREFS = {"focus_topics": ["LLM cost", "AI governance"],
             "business_goals": "Earn diagnostic calls with ops leaders."}

    def test_secondary_profile_is_smb_with_its_own_topics(self):
        profile = am.audience_profile(MIX, am.AUDIENCE_SECONDARY, self.PREFS)
        assert profile == {"audience": "secondary", "description": "small-business owners",
                           "smb": True, "focus_topics": ["cash flow", "customer response time"]}

    def test_primary_profile_keeps_the_users_topics(self):
        profile = am.audience_profile(MIX, "anything-else", self.PREFS)
        assert profile["audience"] == am.AUDIENCE_PRIMARY and profile["smb"] is False
        assert profile["focus_topics"] == ["LLM cost", "AI governance"]

    def test_secondary_without_topics_falls_back_to_the_users(self):
        mix = {**MIX, "secondary_focus_topics": []}
        assert am.audience_profile(mix, "secondary", self.PREFS)["focus_topics"] == \
            ["LLM cost", "AI governance"]

    def test_prefs_are_re_aimed_at_the_secondary_reader(self):
        profile = am.audience_profile(MIX, "secondary", self.PREFS)
        prefs = am.audience_prefs(self.PREFS, profile)
        assert prefs["focus_topics"] == ["cash flow", "customer response time"]
        assert prefs["business_goals"].startswith("This post serves small-business owners.")
        assert "Earn diagnostic calls" in prefs["business_goals"]
        assert self.PREFS["focus_topics"] == ["LLM cost", "AI governance"]  # not mutated

    def test_primary_prefs_keep_the_goal_and_none_profile_is_a_copy(self):
        primary = am.audience_prefs(self.PREFS, am.audience_profile(MIX, "primary", self.PREFS))
        assert primary["business_goals"] == self.PREFS["business_goals"]
        assert am.audience_prefs(self.PREFS, None) == self.PREFS
        no_goal = am.audience_prefs({}, am.audience_profile(MIX, "secondary", {}))
        assert no_goal["business_goals"] == "This post serves small-business owners."

    def test_directives_name_one_reader(self):
        smb = am.audience_directive(am.audience_profile(MIX, "secondary", self.PREFS))
        assert "small-business owners" in smb and "OUTCOME" in smb and "technical leader" in smb
        pro = am.audience_directive(am.audience_profile(MIX, "primary", self.PREFS))
        assert "practitioner" in pro and "already running AI" in pro
        assert am.audience_directive(None) == ""
        assert am.audience_directive({"description": ""}) == ""
