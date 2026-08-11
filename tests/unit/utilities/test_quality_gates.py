"""Unit tests for the quality-gate reasons the review UI renders (issue #421): every finding carries
the score it failed on, the threshold it failed against, and a specific remediation; the persisted
JSON round-trips; and the user's own thresholds override the deploy defaults.
"""

import json

import pytest

pytestmark = pytest.mark.unit

from cqc_lem.utilities.quality_gates import (  # noqa: E402
    AUTHENTICITY_SCORE_MIN_BOUNDS,
    GATE_AUTHENTICITY,
    GATE_FOCUS,
    GATE_MALFORMED_ASSET,
    GATE_MISSING_ASSET,
    GATE_SIMILARITY,
    SIMILARITY_MAX_PCT_BOUNDS,
    authenticity_finding,
    clamp_threshold,
    demoting_findings,
    focus_finding,
    malformed_asset_finding,
    missing_asset_finding,
    parse_gate_findings,
    similarity_finding,
)


class TestFindings:
    def test_authenticity_carries_score_threshold_and_reasons(self):
        f = authenticity_finding(41, 60, ["no concrete specifics", "buzzword filler"])
        assert f["gate"] == GATE_AUTHENTICITY and f["demoted"] is True
        assert f["score"] == 41 and f["threshold"] == 60
        assert "41" in f["explanation"] and "60" in f["explanation"]
        assert f["remediation"]
        assert f["details"] == ["no concrete specifics", "buzzword filler"]

    def test_similarity_reads_as_percentages_and_quotes_the_match(self):
        f = similarity_finding(0.71, 0.55, "Why routing LLM calls by complexity cuts cost")
        assert f["gate"] == GATE_SIMILARITY and f["demoted"] is True
        assert "71%" in f["explanation"] and "55%" in f["explanation"]
        assert "routing LLM calls" in f["details"][0]

    def test_similarity_truncates_a_long_match(self):
        f = similarity_finding(0.9, 0.55, "word " * 100)
        assert len(f["details"][0]) < 200 and f["details"][0].endswith("”")

    def test_similarity_without_a_match_has_no_details(self):
        assert similarity_finding(0.9, 0.55, None)["details"] == []

    def test_focus_alignment_is_advisory_and_names_the_topics(self):
        f = focus_finding(0.02, 0.15, ["AI adoption", "B2B sales"])
        assert f["gate"] == GATE_FOCUS
        # #384 deliberately never blocks the pipeline on an off-niche post — it only warns.
        assert f["demoted"] is False
        assert "AI adoption" in f["remediation"]

    @pytest.mark.parametrize("post_type,expected", [("video", "video"), ("carousel", "slides"),
                                                    ("document", "slides")])
    def test_missing_asset_names_the_missing_media(self, post_type, expected):
        f = missing_asset_finding(post_type)
        assert f["gate"] == GATE_MISSING_ASSET and f["demoted"] is True
        assert expected in f["explanation"]
        assert f["score"] is None and f["threshold"] is None

    def test_malformed_asset_quotes_the_probe_reason(self):
        """#1280: the review UI has to say WHICH way the file was unusable, not just that it was."""
        f = malformed_asset_finding("video", "zero-byte file")
        assert f["gate"] == GATE_MALFORMED_ASSET
        assert "zero-byte file" in f["explanation"] and f["details"] == ["zero-byte file"]
        # Demoting by default: whoever wires this onto a post (#1402) is holding it, flag or not.
        assert f["demoted"] is True

    def test_malformed_asset_reads_without_a_reason(self):
        f = malformed_asset_finding("video")
        assert f["details"] == [] and "empty or unparseable" in f["explanation"]

    def test_every_finding_has_a_label_and_a_fix(self):
        for f in (authenticity_finding(10, 60), similarity_finding(0.9, 0.55),
                  focus_finding(0.0, 0.15, []), missing_asset_finding("video"),
                  malformed_asset_finding("video", "zero-byte file")):
            assert f["label"] and f["explanation"] and f["remediation"]


class TestParseGateFindings:
    def test_round_trips_persisted_json(self):
        findings = [authenticity_finding(41, 60), missing_asset_finding("video")]
        assert parse_gate_findings(json.dumps(findings)) == findings

    def test_accepts_bytes_and_decoded_values(self):
        findings = [authenticity_finding(41, 60)]
        assert parse_gate_findings(json.dumps(findings).encode()) == findings
        assert parse_gate_findings(findings) == findings
        assert parse_gate_findings(findings[0]) == findings

    @pytest.mark.parametrize("raw", [None, "", "not json", "[1, 2]", '{"no": "gate"}', "42"])
    def test_unusable_values_read_as_no_findings(self, raw):
        # A malformed reason must never break the review queue.
        assert parse_gate_findings(raw) == []

    def test_demoting_findings_filters_out_advisories(self):
        findings = [authenticity_finding(41, 60), focus_finding(0.0, 0.15, [])]
        assert demoting_findings(findings) == [findings[0]]
        assert demoting_findings(None) == []


class TestClampThreshold:
    @pytest.mark.parametrize("value,expected", [(None, None), ("", None), ("nope", None),
                                                (-10, 0), (0, 0), (60, 60), (140, 100), ("75", 75)])
    def test_authenticity_bounds(self, value, expected):
        assert clamp_threshold(value, *AUTHENTICITY_SCORE_MIN_BOUNDS) == expected

    @pytest.mark.parametrize("value,expected", [(None, None), (0, 10), (55, 55), (250, 100)])
    def test_similarity_bounds(self, value, expected):
        assert clamp_threshold(value, *SIMILARITY_MAX_PCT_BOUNDS) == expected


class TestUserThresholdsOverrideDefaults:
    def test_authenticity_min_prefers_the_users_setting(self, monkeypatch):
        from cqc_lem.utilities.ai.content_alignment import authenticity_score_min
        monkeypatch.setenv("AUTHENTICITY_SCORE_MIN", "60")
        assert authenticity_score_min({"authenticity_score_min": 85}) == 85
        # Unset / unusable overrides fall back to the deploy default.
        assert authenticity_score_min({"authenticity_score_min": None}) == 60
        assert authenticity_score_min({"authenticity_score_min": "bad"}) == 60
        assert authenticity_score_min({}) == 60
        assert authenticity_score_min() == 60

    def test_authenticity_min_override_is_clamped(self, monkeypatch):
        from cqc_lem.utilities.ai.content_alignment import authenticity_score_min
        monkeypatch.delenv("AUTHENTICITY_SCORE_MIN", raising=False)
        assert authenticity_score_min({"authenticity_score_min": 500}) == 100
        assert authenticity_score_min({"authenticity_score_min": -5}) == 0

    def test_similarity_max_prefers_the_users_percent(self, monkeypatch):
        from cqc_lem.utilities.ai.content_framework import post_similarity_max
        monkeypatch.setenv("POST_SIMILARITY_MAX", "0.55")
        assert post_similarity_max({"post_similarity_max_pct": 40}) == pytest.approx(0.40)
        assert post_similarity_max({"post_similarity_max_pct": None}) == pytest.approx(0.55)
        assert post_similarity_max() == pytest.approx(0.55)

    def test_similarity_max_override_is_clamped_into_a_usable_band(self, monkeypatch):
        from cqc_lem.utilities.ai.content_framework import post_similarity_max
        monkeypatch.delenv("POST_SIMILARITY_MAX", raising=False)
        # 0% would flag every post as a duplicate of everything.
        assert post_similarity_max({"post_similarity_max_pct": 0}) == pytest.approx(0.10)
        assert post_similarity_max({"post_similarity_max_pct": 900}) == pytest.approx(1.0)
