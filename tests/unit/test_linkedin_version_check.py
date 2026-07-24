"""Unit tests for the LinkedIn API version checker (scripts/linkedin_version_check.py)."""

import importlib.util
from datetime import datetime, timezone
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "linkedin_version_check.py"
_spec = importlib.util.spec_from_file_location("linkedin_version_check", SCRIPT)
lvc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lvc)


@pytest.mark.unit
class TestCandidateVersions:
    def test_newest_first_and_spans_the_window(self):
        versions = lvc.candidate_versions(datetime(2026, 7, 24, tzinfo=timezone.utc),
                                          lookback=3, lookahead=1)
        assert versions == ["202608", "202607", "202606", "202605", "202604"]

    def test_rolls_over_the_year_boundary(self):
        versions = lvc.candidate_versions(datetime(2026, 1, 15, tzinfo=timezone.utc),
                                          lookback=2, lookahead=1)
        assert versions == ["202602", "202601", "202512", "202511"]


@pytest.mark.unit
class TestIsActiveResponse:
    def test_426_means_not_active(self):
        assert lvc.is_active_response(426, '{"code":"NONEXISTENT_VERSION"}') is False

    def test_403_still_proves_the_version_exists(self):
        # LEM's token has no read scope, so an active version answers ACCESS_DENIED.
        assert lvc.is_active_response(403, '{"code":"ACCESS_DENIED"}') is True

    def test_200_is_active(self):
        assert lvc.is_active_response(200, "{}") is True

    def test_401_is_undecidable_and_raises(self):
        with pytest.raises(lvc.ProbeError):
            lvc.is_active_response(401, '{"code":"EMPTY_ACCESS_TOKEN"}')


@pytest.mark.unit
class TestFindActiveVersions:
    def test_collects_the_contiguous_live_window_below_unissued_versions(self):
        live = {"202607", "202606", "202605"}
        calls = []

        def probe(_token, version):
            calls.append(version)
            return version in live

        active = lvc.find_active_versions("tok", ["202609", "202608", "202607", "202606",
                                                  "202605", "202604", "202603"], probe=probe)
        assert active == ["202605", "202606", "202607"]
        # stops at the first gap below the window instead of probing the whole lookback
        assert calls[-1] == "202604"

    def test_returns_empty_when_nothing_is_active(self):
        assert lvc.find_active_versions("tok", ["202607", "202606"],
                                        probe=lambda *_: False) == []


@pytest.mark.unit
class TestDecide:
    ACTIVE = ["202601", "202602", "202603", "202604", "202605", "202606", "202607"]

    def test_retired_pin_is_an_urgent_bump(self):
        plan = lvc.decide("202501", self.ACTIVE)
        assert plan["action"] == "urgent-bump"
        assert plan["target"] == "202607"
        assert "retired" in plan["reason"]

    def test_healthy_pin_is_left_alone(self):
        plan = lvc.decide("202606", self.ACTIVE)
        assert plan["action"] == "none"
        assert plan["target"] == "202606"
        assert plan["headroom"] == 5

    def test_pin_at_the_retirement_edge_is_bumped(self):
        plan = lvc.decide("202602", self.ACTIVE, min_headroom=2)
        assert plan["action"] == "bump"
        assert plan["target"] == "202607"
        assert plan["headroom"] == 1

    def test_oldest_live_version_is_bumped(self):
        plan = lvc.decide("202601", self.ACTIVE)
        assert plan["action"] == "bump"
        assert plan["headroom"] == 0

    def test_newest_pin_never_bumps(self):
        assert lvc.decide("202607", self.ACTIVE)["action"] == "none"

    def test_missing_pin_is_an_urgent_bump(self):
        assert lvc.decide(None, self.ACTIVE)["action"] == "urgent-bump"

    def test_no_active_versions_is_an_error_not_a_bump(self):
        plan = lvc.decide("202606", [])
        assert plan["action"] == "error"

    def test_min_headroom_zero_only_bumps_the_oldest(self):
        assert lvc.decide("202601", self.ACTIVE, min_headroom=0)["action"] == "none"


@pytest.mark.unit
class TestRewrites:
    def test_rewrites_the_env_line_only(self):
        text = "FOO=1\nLI_API_VERSION=202501\nLI_USERINFO_RESOURCE=/userinfo\n"
        updated, changed = lvc.rewrite_env_text(text, "202607")
        assert changed is True
        assert "LI_API_VERSION=202607" in updated
        assert "LI_USERINFO_RESOURCE=/userinfo" in updated
        assert "FOO=1" in updated

    def test_env_rewrite_is_a_noop_when_the_key_is_absent(self):
        assert lvc.rewrite_env_text("FOO=1\n", "202607") == ("FOO=1\n", False)

    def test_rewrites_the_env_constants_default(self):
        text = ("LI_API_VERSION = get_constant_from_env('LI_API_VERSION', "
                "default_value='202501')\n")
        updated, changed = lvc.rewrite_default_text(text, "202607")
        assert changed is True
        assert "default_value='202607'" in updated

    def test_default_rewrite_leaves_other_constants_alone(self):
        text = ("OTHER = get_constant_from_env('OTHER', default_value='202501')\n"
                "LI_API_VERSION = get_constant_from_env('LI_API_VERSION', default_value='202501')\n")
        updated, _ = lvc.rewrite_default_text(text, "202607")
        assert "OTHER', default_value='202501'" in updated
        assert "LI_API_VERSION', default_value='202607'" in updated


@pytest.mark.unit
class TestMainRewriteRepoDefaults:
    def test_writes_both_files(self, tmp_path):
        (tmp_path / ".env.example").write_text("LI_API_VERSION=202501\n")
        constants = tmp_path / "src" / "cqc_lem" / "utilities"
        constants.mkdir(parents=True)
        (constants / "env_constants.py").write_text(
            "LI_API_VERSION = get_constant_from_env('LI_API_VERSION', default_value='202501')\n")

        assert lvc.main(["--rewrite-repo-defaults", str(tmp_path), "--version", "202607"]) == 0

        assert "LI_API_VERSION=202607" in (tmp_path / ".env.example").read_text()
        assert "default_value='202607'" in (constants / "env_constants.py").read_text()
