"""Unit tests for the member post-stats API probe (scripts/linkedin_post_stats_api_probe.py)."""

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "linkedin_post_stats_api_probe.py"
_spec = importlib.util.spec_from_file_location("linkedin_post_stats_api_probe", SCRIPT)
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)


def _fetch(status: int, payload) -> callable:
    body = payload if isinstance(payload, str) else json.dumps(payload)
    return lambda *_a, **_k: (status, body)


def _ok_payload(metric: str, count: int, string_shape: bool = True) -> dict:
    metric_type = metric if string_shape else {
        "com.linkedin.adsexternalapi.memberanalytics.v1.CreatorPostAnalyticsMetricTypeV1": metric}
    return {"elements": [{"count": count, "metricType": metric_type,
                          "targetEntity": {"share": "urn:li:share:7"}}],
            "paging": {"count": 10, "start": 0, "links": []}}


@pytest.mark.unit
class TestUrnHandling:
    def test_pulls_a_share_urn_out_of_a_permalink(self):
        url = "https://www.linkedin.com/feed/update/urn:li:share:7325786486870552578/"
        assert probe.urn_from_post_url(url) == "urn:li:share:7325786486870552578"

    def test_pulls_a_ugcpost_urn_out_of_a_permalink(self):
        url = "https://www.linkedin.com/feed/update/urn:li:ugcPost:644315644/"
        assert probe.urn_from_post_url(url) == "urn:li:ugcPost:644315644"

    def test_no_urn_in_the_url_is_none(self):
        assert probe.urn_from_post_url("https://www.linkedin.com/feed/") is None
        assert probe.urn_from_post_url(None) is None

    def test_share_entity_param_is_url_encoded(self):
        assert probe.entity_param("urn:li:share:123") == "(share:urn%3Ali%3Ashare%3A123)"

    def test_ugcpost_entity_param_uses_the_ugc_key(self):
        assert probe.entity_param("urn:li:ugcPost:123") == "(ugc:urn%3Ali%3AugcPost%3A123)"

    def test_activity_urn_is_rejected(self):
        """The analytics PAGE keys off the activity URN; this finder does not, and the ids are
        not interchangeable. Guessing one from the other would probe the wrong post.
        """
        assert probe.entity_param("urn:li:activity:123") is None

    def test_garbage_urn_is_rejected(self):
        assert probe.entity_param("urn:li:person:123") is None
        assert probe.entity_param(None) is None
        assert probe.entity_param("") is None


@pytest.mark.unit
class TestVersionSupport:
    def test_pin_at_or_past_the_floor_supports_the_metric(self):
        assert probe.version_supports("202606", "202604") is True
        assert probe.version_supports("202604", "202604") is True

    def test_pin_below_the_floor_does_not(self):
        assert probe.version_supports("202506", "202604") is False

    def test_unusable_pin_is_undecidable(self):
        for pin in (None, "", "latest", "2026", "2026060"):
            assert probe.version_supports(pin, "202604") is None

    def test_post_save_has_a_later_floor_than_the_entity_finder(self):
        assert probe.METRIC_MIN_VERSION["POST_SAVE"] == "202604"
        assert probe.METRIC_MIN_VERSION["IMPRESSION"] == probe.ENTITY_FINDER_MIN_VERSION


@pytest.mark.unit
class TestAggregationSupport:
    def test_total_serves_every_metric_lem_stores(self):
        for metric in probe.DEFAULT_METRICS:
            assert probe.aggregation_supports(metric, "TOTAL") is True

    def test_daily_does_not_serve_impressions_for_a_post_entity(self):
        assert probe.aggregation_supports("IMPRESSION", "DAILY") is False

    def test_daily_serves_the_engagement_metrics(self):
        for metric in ("REACTION", "COMMENT", "RESHARE", "POST_SAVE"):
            assert probe.aggregation_supports(metric, "DAILY") is True


@pytest.mark.unit
class TestMetricParsing:
    def test_reads_the_bare_string_metric_type(self):
        assert probe.metric_type_name("REACTION") == "REACTION"

    def test_reads_the_pre_202605_object_metric_type(self):
        assert probe.metric_type_name(
            {"com.linkedin.x.CreatorPostAnalyticsMetricTypeV1": "REACTION"}) == "REACTION"

    def test_unreadable_metric_type_is_none(self):
        assert probe.metric_type_name(None) is None
        assert probe.metric_type_name({"a": 1}) is None

    def test_total_from_a_string_shaped_response(self):
        assert probe.metric_total(_ok_payload("IMPRESSION", 72), "IMPRESSION") == 72

    def test_total_from_an_object_shaped_response(self):
        payload = _ok_payload("IMPRESSION", 72, string_shape=False)
        assert probe.metric_total(payload, "IMPRESSION") == 72

    def test_daily_elements_are_summed(self):
        payload = {"elements": [{"count": 10, "metricType": "REACTION"},
                                {"count": 20, "metricType": "REACTION"}]}
        assert probe.metric_total(payload, "REACTION") == 30

    def test_other_metrics_are_ignored(self):
        payload = {"elements": [{"count": 10, "metricType": "REACTION"},
                                {"count": 999, "metricType": "COMMENT"}]}
        assert probe.metric_total(payload, "REACTION") == 10

    def test_no_matching_element_is_none_not_zero(self):
        """An unprovisioned or empty surface must never read as 'this post got zero'."""
        assert probe.metric_total({"elements": []}, "REACTION") is None
        assert probe.metric_total({}, "REACTION") is None
        assert probe.metric_total("nope", "REACTION") is None

    def test_a_real_zero_is_kept(self):
        assert probe.metric_total(_ok_payload("POST_SAVE", 0), "POST_SAVE") == 0

    def test_non_integer_counts_are_skipped(self):
        payload = {"elements": [{"count": "10", "metricType": "REACTION"},
                                {"count": True, "metricType": "REACTION"}]}
        assert probe.metric_total(payload, "REACTION") is None


@pytest.mark.unit
class TestClassifyStatus:
    @pytest.mark.parametrize("status,expected", [
        (200, "ok"), (400, "bad_request"), (401, "unauthorized"), (403, "forbidden"),
        (426, "version_retired"), (429, "throttled"), (500, "server_error"),
        (503, "server_error"), (418, "http_418"),
    ])
    def test_each_status_names_its_own_fix(self, status, expected):
        assert probe.classify_status(status, "{}") == expected

    def test_400_naming_the_query_type_is_a_version_gap_not_a_coding_bug(self):
        body = '{"message":"Unsupported query type TOTAL for metric type POST_SAVE."}'
        assert probe.classify_status(400, body) == "unsupported_metric"

    def test_a_plain_400_stays_a_bad_request(self):
        assert probe.classify_status(400, '{"message":"malformed entity"}') == "bad_request"
        assert probe.classify_status(400, "") == "bad_request"


@pytest.mark.unit
class TestProbeMetric:
    def test_ok_records_count_and_shape(self):
        result = probe.probe_metric("(share:x)", "IMPRESSION", "tok", "202606",
                                    fetch=_fetch(200, _ok_payload("IMPRESSION", 72)))
        assert result["status"] == "ok"
        assert result["count"] == 72
        assert result["column"] == "impressions"
        assert result["metric_type_shape"] == "string"
        assert result["version_ok"] is True

    def test_object_shape_is_reported_as_object(self):
        result = probe.probe_metric("(share:x)", "REACTION", "tok", "202604",
                                    fetch=_fetch(200, _ok_payload("REACTION", 3, False)))
        assert result["metric_type_shape"] == "object"
        assert result["count"] == 3

    def test_403_carries_linkedins_own_message(self):
        result = probe.probe_metric("(share:x)", "POST_SAVE", "tok", "202606",
                                    fetch=_fetch(403, {"message": "Not enough permissions",
                                                       "status": 403}))
        assert result["status"] == "forbidden"
        assert result["count"] is None
        assert result["error"] == "Not enough permissions"

    def test_unparseable_body_falls_back_to_an_excerpt(self):
        result = probe.probe_metric("(share:x)", "COMMENT", "tok", "202606",
                                    fetch=_fetch(500, "<html>boom</html>"))
        assert result["status"] == "server_error"
        assert "boom" in result["error"]

    def test_a_pin_below_the_metric_floor_is_flagged(self):
        result = probe.probe_metric("(share:x)", "POST_SAVE", "tok", "202506",
                                    fetch=_fetch(400, {"message": "unknown queryType"}))
        assert result["version_ok"] is False
        assert result["min_version"] == "202604"

    def test_a_daily_unsupported_metric_is_flagged_before_the_response_is_read(self):
        result = probe.probe_metric("(share:x)", "IMPRESSION", "tok", "202606",
                                    aggregation="DAILY",
                                    fetch=_fetch(400, {"message": "Unsupported query type DAILY "
                                                                  "for metric type IMPRESSION."}))
        assert result["aggregation"] == "DAILY"
        assert result["aggregation_ok"] is False
        assert result["version_ok"] is True

    def test_total_leaves_every_metric_servable(self):
        result = probe.probe_metric("(share:x)", "IMPRESSION", "tok", "202606",
                                    fetch=_fetch(200, _ok_payload("IMPRESSION", 72)))
        assert result["aggregation"] == "TOTAL"
        assert result["aggregation_ok"] is True

    def test_url_carries_the_finder_entity_and_aggregation(self):
        seen = {}

        def fetch(url, token, version):
            seen["url"] = url
            return 200, json.dumps(_ok_payload("REACTION", 1))

        probe.probe_metric("(share:urn%3Ali%3Ashare%3A7)", "REACTION", "tok", "202606",
                           aggregation="DAILY", fetch=fetch)
        assert "q=entity" in seen["url"]
        assert "entity=(share:urn%3Ali%3Ashare%3A7)" in seen["url"]
        assert "queryType=REACTION" in seen["url"]
        assert "aggregation=DAILY" in seen["url"]


@pytest.mark.unit
class TestCompareCounts:
    def test_agreement_is_a_match(self):
        results = [{"column": "reactions", "count": 5}]
        comparison = probe.compare_counts(results, {"reactions": 5})
        assert comparison["reactions"] == {"api": 5, "scrape": 5, "match": True, "delta": 0}

    def test_disagreement_carries_the_delta(self):
        results = [{"column": "impressions", "count": 80}]
        comparison = probe.compare_counts(results, {"impressions": 72})
        assert comparison["impressions"]["match"] is False
        assert comparison["impressions"]["delta"] == 8

    def test_a_missing_side_is_ungraded_not_a_mismatch(self):
        results = [{"column": "saves", "count": None}, {"column": "impressions", "count": 3}]
        comparison = probe.compare_counts(results, {"saves": 2, "impressions": None})
        assert comparison["saves"]["match"] is None
        assert comparison["impressions"]["match"] is None

    def test_no_stored_row_grades_nothing(self):
        comparison = probe.compare_counts([{"column": "reactions", "count": 5}], None)
        assert comparison["reactions"]["match"] is None


@pytest.mark.unit
class TestVerdict:
    def test_403_on_any_metric_blocks_and_names_the_permission(self):
        results = [{"status": "ok"}, {"status": "forbidden"}]
        outcome = probe.verdict(results, {})
        assert outcome["outcome"] == "blocked"
        assert probe.REQUIRED_PERMISSION in outcome["reason"]

    def test_401_is_an_error_not_a_block(self):
        assert probe.verdict([{"status": "unauthorized"}], {})["outcome"] == "error"

    def test_426_points_at_the_version_pin(self):
        outcome = probe.verdict([{"status": "version_retired"}], {})
        assert outcome["outcome"] == "error"
        assert "LI_API_VERSION" in outcome["reason"]

    def test_all_ok_and_matching(self):
        comparison = {"reactions": {"match": True}, "saves": {"match": True}}
        assert probe.verdict([{"status": "ok"}], comparison)["outcome"] == "available_match"

    def test_all_ok_but_disagreeing(self):
        comparison = {"reactions": {"match": True}, "impressions": {"match": False}}
        outcome = probe.verdict([{"status": "ok"}], comparison)
        assert outcome["outcome"] == "available_mismatch"
        assert "impressions" in outcome["reason"]

    def test_all_ok_with_nothing_to_compare(self):
        comparison = {"reactions": {"match": None}}
        assert probe.verdict([{"status": "ok"}], comparison)["outcome"] == "available"

    def test_a_metric_below_the_version_floor_names_the_floor(self):
        results = [{"status": "ok", "metric": "REACTION"},
                   {"status": "unsupported_metric", "metric": "POST_SAVE", "version_ok": False}]
        outcome = probe.verdict(results, {})
        assert outcome["outcome"] == "partial"
        assert "POST_SAVE" in outcome["reason"] and "202604" in outcome["reason"]
        assert "LI_API_VERSION" in outcome["reason"]

    def test_a_daily_only_restriction_blames_the_flag_not_the_version(self):
        """LinkedIn answers a DAILY-unsupported metric with the same 400 shape as a version gap.
        Sending the operator to bump LI_API_VERSION over their own --aggregation flag would waste
        the exact debugging session this probe exists to prevent.
        """
        results = [{"status": "unsupported_metric", "metric": "IMPRESSION",
                    "version_ok": True, "aggregation_ok": False}]
        outcome = probe.verdict(results, {})
        assert outcome["outcome"] == "partial"
        assert "DAILY" in outcome["reason"] and "--aggregation TOTAL" in outcome["reason"]
        assert "LI_API_VERSION" not in outcome["reason"]

    def test_the_aggregation_cause_is_named_ahead_of_the_version_one(self):
        results = [{"status": "unsupported_metric", "metric": "IMPRESSION",
                    "version_ok": False, "aggregation_ok": False}]
        assert "DAILY" in probe.verdict(results, {})["reason"]

    def test_an_unsupported_metric_on_a_new_enough_pin_is_just_partial(self):
        """version_ok True means the pin clears the floor — blaming LI_API_VERSION there would
        send whoever reads this report to fix the wrong thing.
        """
        results = [{"status": "unsupported_metric", "metric": "POST_SAVE", "version_ok": True}]
        outcome = probe.verdict(results, {})
        assert outcome["outcome"] == "partial"
        assert "LI_API_VERSION" not in outcome["reason"]

    def test_mixed_failures_are_partial(self):
        outcome = probe.verdict([{"status": "ok"}, {"status": "throttled"}], {})
        assert outcome["outcome"] == "partial"
        assert "throttled" in outcome["reason"]

    def test_nothing_probed(self):
        assert probe.verdict([], {})["outcome"] == "not_probed"

    def test_403_wins_over_a_successful_metric(self):
        """One readable metric must not make an unreadable surface look usable."""
        comparison = {"reactions": {"match": True}}
        results = [{"status": "ok"}, {"status": "forbidden"}]
        assert probe.verdict(results, comparison)["outcome"] == "blocked"


@pytest.mark.unit
class TestBuildReport:
    def test_report_names_the_permission_and_version_floors(self):
        results = [probe.probe_metric("(share:x)", "POST_SAVE", "tok", "202606",
                                      fetch=_fetch(200, _ok_payload("POST_SAVE", 4)))]
        report = probe.build_report("urn:li:share:7", "(share:x)", "202606", results,
                                    {"saves": 4})
        assert report["required_permission"] == "r_member_postAnalytics"
        assert report["entity_finder_min_version"] == "202506"
        assert report["metric_type_string_since"] == "202605"
        assert report["comparison"]["saves"]["match"] is True
        assert report["verdict"]["outcome"] == "available_match"

    def test_report_records_the_aggregation_it_ran_under(self):
        """The report is pasted into an issue; without the aggregation, a metric that came back
        empty cannot be told apart from one LinkedIn refuses to serve that way.
        """
        report = probe.build_report("urn:li:share:7", "(share:x)", "202606", [], None, "DAILY")
        assert report["aggregation"] == "DAILY"

    def test_report_is_json_serialisable(self):
        results = [probe.probe_metric("(share:x)", "COMMENT", "tok", "202606",
                                      fetch=_fetch(403, {"message": "denied"}))]
        report = probe.build_report("urn:li:share:7", "(share:x)", "202606", results, None)
        assert json.loads(json.dumps(report))["verdict"]["outcome"] == "blocked"


@pytest.mark.unit
class TestMain:
    def test_an_activity_urn_fails_with_an_explanation_not_a_probe(self, capsys):
        code = probe.main(["--entity-urn", "urn:li:activity:123", "--token", "t",
                           "--version", "202606"])
        assert code == 1
        report = json.loads(capsys.readouterr().out)
        assert report["verdict"]["outcome"] == "error"
        assert "share/ugcPost" in report["verdict"]["reason"]

    def test_no_urn_at_all_fails_before_any_network_call(self, capsys):
        code = probe.main(["--token", "t", "--version", "202606"])
        assert code == 1
        assert "no share/ugcPost URN" in json.loads(capsys.readouterr().out)["verdict"]["reason"]

    def test_happy_path_prints_a_report(self, capsys, monkeypatch):
        monkeypatch.setattr(probe, "http_get_json",
                            lambda url, token, version, timeout=20:
                            (200, json.dumps(_ok_payload("REACTION", 5))))
        code = probe.main(["--entity-urn", "urn:li:share:7", "--token", "t",
                           "--version", "202606", "--metrics", "REACTION"])
        assert code == 0
        report = json.loads(capsys.readouterr().out)
        assert report["metrics"][0]["count"] == 5
        assert report["entity_param"] == "(share:urn%3Ali%3Ashare%3A7)"

    def test_the_token_is_never_echoed_into_the_report(self, capsys, monkeypatch):
        monkeypatch.setattr(probe, "http_get_json",
                            lambda url, token, version, timeout=20:
                            (403, json.dumps({"message": "Not enough permissions"})))
        probe.main(["--entity-urn", "urn:li:share:7", "--token", "s3cr3t-token",
                    "--version", "202606", "--metrics", "REACTION"])
        assert "s3cr3t-token" not in capsys.readouterr().out
