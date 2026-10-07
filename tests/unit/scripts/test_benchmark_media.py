"""lem-vision / lem-image benchmark (issue #2251).

Covers scripts/benchmark_media.py and its `benchmark_models.py --tiers lem-vision,lem-image` entry
point. No provider is ever called.
"""

import base64
import hashlib
import io
import json
import pathlib
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_ROOT = pathlib.Path(__file__).resolve().parents[3]
_SCRIPTS = _ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import benchmark_media as media  # noqa: E402
import benchmark_models as bm  # noqa: E402
import model_health_check as mhc  # noqa: E402

CONFIG = """\
model_list:
  - model_name: lem-image
    litellm_params:
      model: openai/gpt-image-2
  - model_name: lem-image
    litellm_params:
      model: openai/gpt-image-1
  - model_name: lem-vision
    litellm_params:
      model: openai/gpt-4o-mini
"""

PRICES = {
    "openai/gpt-4o-mini": {"input_cost_per_token": 1.5e-07, "output_cost_per_token": 6e-07},
    "openai/gpt-5.4-mini": {"input_cost_per_token": 7.5e-07, "output_cost_per_token": 4.5e-06},
    "openai/gpt-image-2": {"input_cost_per_token": 5e-06, "output_cost_per_image_token": 3e-05},
    "gpt-image-1": {"input_cost_per_token": 5e-06, "output_cost_per_image_token": 4e-05},
    "openai/gpt-image-2.5-flare": {"input_cost_per_token": 5e-06,
                                   "output_cost_per_image_token": 3e-05},
}

SNAPSHOT = {"openai": {"source": "openai /v1/models",
                       "models": ["gpt-4o-mini", "gpt-5.4-mini", "gpt-image-2", "gpt-image-1",
                                  "gpt-image-2.5-flare"]},
            "perplexity": {"models": []},
            "deprecations": [{"provider": "openai", "model": "gpt-image-1",
                              "date": "2026-10-23"}]}


def _deployments():
    return mhc.parse_deployments(mhc.load_config_text(CONFIG))


def _truth_answer(fixture):
    return json.dumps(fixture["truth"])


class TestFixtures:
    def test_every_fixture_renders_deterministically_and_distinctly(self):
        digests = []
        for fixture in media.VISION_FIXTURES:
            first, second = media.render_fixture(fixture), media.render_fixture(fixture)
            assert first == second
            assert first[:8] == b"\x89PNG\r\n\x1a\n"
            digests.append(hashlib.sha256(first).hexdigest())
        assert len(set(digests)) == len(media.VISION_FIXTURES)

    def test_ground_truth_covers_all_three_questions(self):
        truths = [f["truth"] for f in media.VISION_FIXTURES]
        assert {t["stray_text"] for t in truths} == {True, False}
        assert {c for t in truths for c in t["cliche_objects"]} == {"gears", "pipes",
                                                                     "server_rack"}
        assert {"happy", "sad", "surprised", "none"} <= {t["emotion"] for t in truths}
        assert all(set(t) == set(media.VISION_FIELDS) for t in truths)

    def test_fixture_images_are_the_size_promised(self):
        from PIL import Image
        image = Image.open(io.BytesIO(media.render_fixture(media.VISION_FIXTURES[0])))
        assert image.size == (media.FIXTURE_SIZE, media.FIXTURE_SIZE)

    def test_an_unknown_element_is_an_error(self):
        with pytest.raises(ValueError):
            media.render_fixture({"id": "x", "draw": ("unicorn",)})

    def test_font_falls_back_without_freetype(self):
        from PIL import ImageFont
        real = ImageFont.load_default
        with patch.object(ImageFont, "load_default",
                          side_effect=lambda size=None: real() if size is None else
                          (_ for _ in ()).throw(OSError("no freetype"))):
            assert media._font(40) is not None


class TestVisionScoring:
    def test_a_perfect_answer(self):
        truth = {"stray_text": True, "cliche_objects": ["gears"], "emotion": "none"}
        score = media.score_vision_answer(truth, {"stray_text": True,
                                                  "cliche_objects": ["Gear", "none"],
                                                  "emotion": "None"})
        assert score == {"fields": {"stray_text": True, "cliche_objects": True, "emotion": True},
                         "agreement": 1.0, "parsed": True}

    def test_partial_and_unparsed(self):
        truth = {"stray_text": False, "cliche_objects": [], "emotion": "sad"}
        partial = media.score_vision_answer(truth, {"stray_text": "false",
                                                    "cliche_objects": ["server racks"],
                                                    "emotion": "sad"})
        assert partial["fields"] == {"stray_text": False, "cliche_objects": False, "emotion": True}
        assert partial["agreement"] == pytest.approx(1 / 3)
        assert media.score_vision_answer(truth, None)["parsed"] is False

    def test_normalize_cliches(self):
        assert media.normalize_cliches(["Server Rack", "cogs", "light-bulb", "none", ""]) == [
            "gears", "light_bulb", "server_rack"]
        assert media.normalize_cliches("gears") == []

    @pytest.mark.parametrize("text,expected", [
        ('{"a": 1}', {"a": 1}), ('```json\n{"a": 1}\n```', {"a": 1}), ("[1]", None),
        ("nope", None), (None, None), ("", None)])
    def test_parse_json_object(self, text, expected):
        assert media.parse_json_object(text) == expected

    def test_scorecard_excludes_errors_from_the_rate(self):
        good = media.score_vision_answer({"stray_text": True, "cliche_objects": [],
                                          "emotion": "none"},
                                         {"stray_text": True, "cliche_objects": [],
                                          "emotion": "none"})
        bad = media.score_vision_answer({"stray_text": True, "cliche_objects": [],
                                         "emotion": "none"}, None)
        card = media.vision_scorecard("m", "champion", [
            {"case": "a", "score": good, "latency_ms": 10, "cost_usd": 0.001},
            {"case": "b", "score": bad, "latency_ms": 30, "cost_usd": 0.001},
            {"case": "c", "score": None, "error": "500"}])
        assert (card["measured"], card["errors"], card["unparsed"]) == (2, 1, 1)
        assert card["agreement_rate"] == 0.5
        assert card["exact_cases"] == 1 and card["latency_p50_ms"] == 20
        empty = media.vision_scorecard("m", "candidate", [{"case": "a", "score": None}])
        assert empty["agreement_rate"] is None and empty["field_rates"]["emotion"] is None


def _scores(**override):
    base = {name: 5 for name in media.RUBRIC_CRITERIA}
    base.update(override)
    return base


class TestImageScoring:
    def test_rubric_mirrors_2248(self):
        assert media.RUBRIC_CRITERIA == ("specificity", "no_cliche", "thumbnail_read",
                                         "text_accuracy", "craft", "scroll_stop", "brand_fit")
        assert set(media.RUBRIC_DESCRIPTIONS) == set(media.RUBRIC_CRITERIA)
        assert set(media.RUBRIC_FLOORS) <= set(media.RUBRIC_CRITERIA)

    def test_rubric_matches_image_gen(self):
        from cqc_lem.utilities.ai import image_gen
        assert media.RUBRIC_CRITERIA == image_gen.RUBRIC_CRITERIA
        floors = dict(image_gen._RUBRIC_FLOORS, scroll_stop=image_gen._SCROLL_STOP_FLOOR)
        assert media.RUBRIC_FLOORS == floors
        assert "post_image" in image_gen._SCROLL_STOP_SURFACES

    def test_parse_rubric_is_strict(self):
        assert media.parse_rubric(json.dumps(_scores(notes="ok"))) == _scores()
        assert media.parse_rubric(json.dumps(_scores(craft=6))) is None
        assert media.parse_rubric(json.dumps(_scores(craft=True))) is None
        assert media.parse_rubric(json.dumps(_scores(craft="4"))) is None
        partial = _scores()
        partial.pop("brand_fit")
        assert media.parse_rubric(json.dumps(partial)) is None
        assert media.parse_rubric("not json") is None

    def test_floors(self):
        assert media.rubric_acceptable(_scores())
        assert not media.rubric_acceptable(_scores(no_cliche=4))
        assert media.rubric_acceptable(_scores(brand_fit=3, thumbnail_read=1))
        assert not media.rubric_acceptable(_scores(scroll_stop=3))

    def test_scorecard(self):
        rows = [{"brief": "a", "scores": _scores(), "latency_ms": 5, "cost_usd": 0.05},
                {"brief": "b", "scores": _scores(no_cliche=2), "cost_usd": 0.05},
                {"brief": "c", "scores": None, "error": None},
                {"brief": "d", "scores": None, "error": "500"}]
        card = media.image_scorecard("gpt-image-2", "champion", rows)
        assert (card["rendered"], card["measured"], card["unscored"], card["errors"]) == (
            3, 2, 1, 1)
        assert card["acceptance_rate"] == 0.5
        assert card["criterion_means"]["no_cliche"] == 3.5
        assert card["cost_usd"] == 0.1
        assert media.image_scorecard("x", "candidate", [])["overall_mean"] is None

    def test_judge_messages_carry_the_brief_and_image(self):
        brief = media.IMAGE_BRIEFS[1]
        msg = media.image_judge_messages(brief, b"PNGDATA")
        text = msg[0]["content"][0]["text"]
        assert 'exactly "FRESH TODAY"' in text and "scroll_stop" in text
        assert msg[0]["content"][1]["image_url"]["url"].endswith(
            base64.b64encode(b"PNGDATA").decode())
        no_text = media.image_judge_messages(media.IMAGE_BRIEFS[0], b"x")[0]["content"][0]["text"]
        assert "any visible text is a defect" in no_text


def _vcard(model, role, rate, measured=10, cases=10):
    return {"tier": "lem-vision", "model": model, "role": role, "agreement_rate": rate,
            "measured": measured, "cases": cases}


def _icard(model, role, rate, mean, measured=3, cases=3):
    return {"tier": "lem-image", "model": model, "role": role, "acceptance_rate": rate,
            "overall_mean": mean, "measured": measured, "cases": cases, "rendered": measured}


class TestGate:
    def test_vision_recommend_reject_floor(self):
        champ = _vcard("a", "champion", 0.85)
        assert media.gate_media(_vcard("b", "candidate", 0.9), champ)["verdict"] == "recommend"
        rej = media.gate_media(_vcard("b", "candidate", 0.82), _vcard("a", "champion", 0.9))
        assert rej["verdict"] == "reject" and "< champion" in rej["reasons"][0]
        low = media.gate_media(_vcard("b", "candidate", 0.7), _vcard("a", "champion", 0.6))
        assert low["verdict"] == "reject" and "floor" in low["reasons"][0]

    def test_inconclusive_and_no_baseline(self):
        part = media.gate_media(_vcard("b", "candidate", 0.9, measured=8), None)
        assert part["verdict"] == "inconclusive"
        nob = media.gate_media(_vcard("b", "candidate", 0.5), None)
        assert nob["verdict"] == "no-baseline" and len(nob["reasons"]) == 2
        partial_champ = media.gate_media(_vcard("b", "candidate", 0.9),
                                         _vcard("a", "champion", 0.9, measured=5))
        assert partial_champ["verdict"] == "no-baseline"

    def test_image_needs_acceptance_and_mean(self):
        champ = _icard("a", "champion", 2 / 3, 4.2)
        assert media.gate_media(_icard("b", "candidate", 1.0, 4.5), champ)["verdict"] == "recommend"
        worse_mean = media.gate_media(_icard("b", "candidate", 1.0, 4.0), champ)
        assert worse_mean["verdict"] == "reject" and "rubric mean" in worse_mean["reasons"][0]

    def test_gate_cards_pairs_by_tier(self):
        gates = media.gate_cards([_vcard("a", "champion", 0.9), _vcard("b", "candidate", 0.95),
                                  _icard("c", "candidate", 1.0, 5.0)])
        assert [(g["model"], g["verdict"]) for g in gates] == [("b", "recommend"),
                                                              ("c", "no-baseline")]

    def test_harness_outage(self):
        run = {"scorecards": [dict(_vcard("a", "champion", None, measured=0), errors=10)],
               "results": {"lem-vision": {"a": [{"error": "401 bad key"}, {"error": "401 bad key"},
                                                {"error": "dns"}]}}}
        assert "401 bad key" in media.harness_outage(run)
        run["scorecards"][0]["measured"] = 1
        assert media.harness_outage(run) is None
        assert media.harness_outage({"scorecards": []}) is None


class TestRosterAndSpend:
    def test_default_roster_drops_sunsetting_and_adds_the_successor(self):
        roster = media.resolve_roster(_deployments(), ["lem-vision", "lem-image"],
                                      provider_snapshot=SNAPSHOT)
        assert roster["lem-vision"] == {"champion": "gpt-4o-mini", "candidates": ["gpt-5.4-mini"]}
        assert roster["lem-image"] == {"champion": "gpt-image-2",
                                       "candidates": ["gpt-image-2.5-flare"]}

    def test_overrides_are_taken_as_given_and_capped(self):
        roster = media.resolve_roster(_deployments(), ["lem-image"],
                                      overrides={"lem-image": ["gpt-image-2", "x", "x", "y", "z"]})
        assert roster["lem-image"]["candidates"] == ["x", "y"]

    def test_an_unconfigured_tier_has_no_champion(self):
        assert media.resolve_roster([], ["lem-vision"])["lem-vision"] == {"champion": None,
                                                                          "candidates": []}

    def test_plan_prices_every_call(self):
        roster = {"lem-vision": {"champion": "gpt-4o-mini", "candidates": []},
                  "lem-image": {"champion": "gpt-image-2", "candidates": ["gpt-image-1"]}}
        plan = media.plan_spend(roster, PRICES, judge_model="gpt-4o-mini")
        assert plan["unpriced"] == []
        vision = media.vision_call_cost("gpt-4o-mini", PRICES)
        assert vision == pytest.approx((600 + 8500) * 1.5e-07 + 1200 * 6e-07)
        assert plan["items"][0]["est_usd"] == pytest.approx(vision * 10, rel=1e-4)
        render = media.image_render_cost("gpt-image-1", PRICES, "medium")
        assert render == pytest.approx(250 * 5e-06 + 1800 * 4e-05)
        assert plan["total_usd"] == pytest.approx(sum(i["est_usd"] for i in plan["items"]))
        assert media.spend_refusal(plan, 5.0) is None
        assert "exceeds the $0.10 cap" in media.spend_refusal(plan, 0.10)

    def test_an_unpriced_model_refuses_the_whole_plan(self):
        roster = {"lem-image": {"champion": "gpt-image-9", "candidates": []},
                  "lem-vision": {"champion": "mystery", "candidates": []}}
        plan = media.plan_spend(roster, PRICES, judge_model=None)
        assert plan["total_usd"] is None
        assert plan["unpriced"] == ["gpt-image-9", "mystery", "no judge (judge)"]
        refusal = media.spend_refusal(plan, 100.0)
        assert refusal.startswith("refusing to run: no pinned price for")
        text = media.render_spend_plan(plan, 100.0)
        assert "UNPRICED" in text and "n/a (unpriced model)" in text

    def test_bad_quality_or_bool_prices_are_unpriced(self):
        assert media.image_render_cost("gpt-image-2", PRICES, "ultra") is None
        assert media.vision_call_cost("x", {"x": {"input_cost_per_token": True,
                                                  "output_cost_per_token": 1}}) is None

    def test_max_spend_env(self, monkeypatch):
        monkeypatch.delenv("BENCHMARK_MAX_SPEND_USD", raising=False)
        assert media.max_spend_usd() == 2.0
        monkeypatch.setenv("BENCHMARK_MAX_SPEND_USD", "0.75")
        assert media.max_spend_usd() == 0.75
        monkeypatch.setenv("BENCHMARK_MAX_SPEND_USD", "-3")
        assert media.max_spend_usd() == 0.0
        monkeypatch.setenv("BENCHMARK_MAX_SPEND_USD", "lots")
        assert media.max_spend_usd() == 2.0

    def test_meter(self):
        meter = media.SpendMeter(1.0)
        meter.reserve(0.6)
        meter.charge(0.6)
        meter.charge(-5)
        with pytest.raises(media.SpendCapExceeded):
            meter.reserve(0.5)


class FakeProvider:
    """Answers vision fixtures from their truth (champion) or wrongly (others); renders bytes."""

    def __init__(self, perfect=("gpt-4o-mini",), fail_models=(), judge_text=None):
        self.perfect = set(perfect)
        self.fail_models = set(fail_models)
        self.judge_text = judge_text or json.dumps(_scores())
        self.calls = []

    def vision(self, model, messages):
        self.calls.append(("vision", model))
        if model in self.fail_models:
            return {"text": None, "error": "boom", "usage": {}, "latency_ms": 1.0}
        prompt = messages[0]["content"][0]["text"]
        if "Score each criterion" in prompt:
            return {"text": self.judge_text, "error": None,
                    "usage": {"prompt_tokens": 1000, "completion_tokens": 50}, "latency_ms": 2.0}
        url = messages[0]["content"][1]["image_url"]["url"]
        image = base64.b64decode(url.split(",", 1)[1])
        fixture = next(f for f in media.VISION_FIXTURES if media.render_fixture(f) == image)
        answer = (fixture["truth"] if model in self.perfect else
                  {"stray_text": not fixture["truth"]["stray_text"], "cliche_objects": [],
                   "emotion": "none"})
        return {"text": json.dumps(answer), "error": None, "usage": {}, "latency_ms": 3.0}

    def render(self, model, prompt, *, quality):
        self.calls.append(("render", model))
        if model in self.fail_models:
            return {"image": None, "error": "render failed", "usage": {}, "latency_ms": 1.0}
        return {"image": b"PNG" + model.encode(), "error": None,
                "usage": {"input_tokens": 100, "output_tokens": 1000}, "latency_ms": 9.0}


def _roster():
    return {"lem-vision": {"champion": "gpt-4o-mini", "candidates": ["gpt-5.4-mini"]},
            "lem-image": {"champion": "gpt-image-2", "candidates": ["gpt-image-2.5-flare"]}}


def _run(provider, cap=5.0, roster=None):
    return media.run_media_benchmark(roster or _roster(), provider=provider, prices=PRICES,
                                     cap=cap, judge_model="gpt-4o-mini", run_id="mm-test",
                                     today="2026-10-07")


class TestRun:
    def test_a_full_run_scores_gates_and_meters(self):
        provider = FakeProvider()
        run = _run(provider)
        cards = {(c["tier"], c["model"]): c for c in run["scorecards"]}
        assert cards[("lem-vision", "gpt-4o-mini")]["agreement_rate"] == 1.0
        assert cards[("lem-vision", "gpt-5.4-mini")]["field_rates"]["stray_text"] == 0.0
        assert cards[("lem-image", "gpt-image-2")]["acceptance_rate"] == 1.0
        verdicts = {g["model"]: g["verdict"] for g in run["gates"]}
        assert verdicts == {"gpt-5.4-mini": "reject", "gpt-image-2.5-flare": "recommend"}
        assert run["harness_outage"] is None
        # image cost comes off the reported usage: 100 in + 1000 out image tokens, plus the judge
        render = 100 * 5e-06 + 1000 * 3e-05
        judge = 1000 * 1.5e-07 + 50 * 6e-07
        assert cards[("lem-image", "gpt-image-2")]["cost_usd"] == pytest.approx(
            3 * (render + judge), rel=1e-4)
        assert run["spend"]["actual_usd"] > 0 and run["spend"]["stopped"] is None
        assert ("render", "gpt-image-2") in provider.calls

    def test_the_meter_stops_a_run_at_the_cap(self):
        run = _run(FakeProvider(), cap=0.0)
        assert run["spend"]["stopped"].startswith("spend cap $0.00 reached")
        assert all(c["measured"] == 0 for c in run["scorecards"])
        assert run["harness_outage"]

    def test_errors_and_unscored_briefs(self):
        provider = FakeProvider(fail_models=("gpt-image-2.5-flare",), judge_text="garbage")
        run = _run(provider)
        cards = {(c["tier"], c["model"]): c for c in run["scorecards"]}
        assert cards[("lem-image", "gpt-image-2.5-flare")]["errors"] == 3
        assert cards[("lem-image", "gpt-image-2")]["unscored"] == 3
        verdicts = {g["model"]: g["verdict"] for g in run["gates"]}
        assert verdicts["gpt-image-2.5-flare"] == "inconclusive"
        report = media.render_media_report(run)
        assert "unscored (judge answer unparseable)" in report
        assert "error: render failed" in report

    def test_report_leaderboard_and_write(self, tmp_path):
        run = _run(FakeProvider())
        report = media.render_media_report(run)
        assert report.startswith("# Media benchmark `mm-test` (2026-10-07)")
        assert "| `gpt-4o-mini` | champion | 100% |" in report
        assert "never applied" in report
        rows = media.media_leaderboard_rows(run)
        assert {r["verdict"] for r in rows} == {"baseline", "reject", "recommend"}
        readme = tmp_path / "README.md"
        readme.write_text("# t\n")
        path = media.write_media_report(run, str(tmp_path))
        assert pathlib.Path(path).name == "2026-10-07-mm-test.md"
        once = readme.read_text()
        media.write_media_report(run, str(tmp_path))
        assert readme.read_text() == once  # re-render replaces its own rows
        assert once.count("`mm-test`") == 4
        assert media.MEDIA_LEADERBOARD_BEGIN in once and "## Media leaderboard" in once

    def test_leaderboard_keeps_other_runs_and_drops_header_rows(self):
        text = media.update_media_leaderboard("", [
            {"date": "2026-01-01", "run_id": "old", "tier": "lem-vision", "model": "m",
             "role": "champion", "score": "90%", "detail": "d", "cost": "$0", "verdict": "baseline"}])
        merged = media.update_media_leaderboard(text, [
            {"date": "2026-02-01", "run_id": "new", "tier": "lem-vision", "model": "m",
             "role": "champion", "score": "80%", "detail": "d", "cost": "$0", "verdict": "baseline"}])
        body = merged.split(media.MEDIA_LEADERBOARD_BEGIN)[1]
        assert body.index("`new`") < body.index("`old`")
        assert body.count("| Date |") == 1
        assert media.parse_media_row("| a | b |") is None

    def test_refuses_untagged_or_unmeasured_runs(self):
        with pytest.raises(ValueError):
            media.render_media_report({"kind": "text", "source": "benchmark"})
        run = _run(FakeProvider(), cap=0.0)
        with pytest.raises(ValueError, match="harness outage"):
            media.render_media_report(run)


class TestMediaProvider:
    def test_vision_and_render_through_attributed_openai(self):
        client = MagicMock()
        client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=' {"a": 1} '))],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=2))
        client.images.generate.return_value = SimpleNamespace(
            data=[SimpleNamespace(b64_json=base64.b64encode(b"img").decode())],
            usage=SimpleNamespace(input_tokens=5, output_tokens=7))
        with patch("cqc_lem.utilities.ai.client.AttributedOpenAI", return_value=client) as ctor:
            provider = media.MediaProvider("sk-test", "https://api.example/v1")
            got = provider.vision("gpt-4o-mini", [{"role": "user", "content": "x"}])
            rendered = provider.render("gpt-image-2", "p", quality="medium")
        ctor.assert_called_once()
        assert ctor.call_args.kwargs["base_url"] == "https://api.example/v1"
        assert got["text"] == '{"a": 1}' and got["usage"]["prompt_tokens"] == 10
        kwargs = client.chat.completions.create.call_args.kwargs
        assert kwargs["max_completion_tokens"] == media.VISION_MAX_TOKENS
        assert "temperature" not in kwargs
        assert rendered["image"] == b"img" and rendered["usage"]["output_tokens"] == 7

    def test_provider_failures_are_case_results(self):
        client = MagicMock()
        client.chat.completions.create.side_effect = RuntimeError("429")
        client.images.generate.return_value = SimpleNamespace(data=[SimpleNamespace(b64_json=None)])
        with patch("cqc_lem.utilities.ai.client.AttributedOpenAI", return_value=client):
            provider = media.MediaProvider("sk-test")
            assert provider.vision("m", [])["error"] == "429"
            assert "no b64" in provider.render("m", "p", quality="low")["error"]


class TestBenchmarkModelsEntryPoint:
    def _files(self, tmp_path):
        config = tmp_path / "config.yaml"
        config.write_text(CONFIG)
        prices = tmp_path / "prices.json"
        prices.write_text(json.dumps({"models": PRICES}))
        snapshot = tmp_path / "snap.json"
        snapshot.write_text(json.dumps(SNAPSHOT))
        return ["--config", str(config), "--prices", str(prices), "--provider-snapshot",
                str(snapshot), "--out-dir", str(tmp_path / "out")]

    def test_dry_run_prints_the_plan_and_calls_nothing(self, tmp_path, capsys):
        with patch.object(media, "MediaProvider") as provider:
            rc = bm.main(["--dry-run", "--tiers", "lem-vision,lem-image"] + self._files(tmp_path))
        assert rc == 0
        provider.assert_not_called()
        out = capsys.readouterr().out
        assert "Planned media-benchmark spend" in out and "`gpt-image-2.5-flare`" in out
        assert not (tmp_path / "out").exists()

    def test_over_the_cap_is_refused_before_any_call(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("BENCHMARK_ENABLED", "true")
        with patch.object(media, "MediaProvider") as provider:
            rc = bm.main(["--run", "--tiers", "lem-image", "--max-spend-usd", "0.01"]
                         + self._files(tmp_path))
        assert rc == 1
        provider.assert_not_called()
        assert "exceeds the $0.01 cap" in capsys.readouterr().err

    def test_not_enabled_or_no_key_is_a_no_op_or_error(self, tmp_path, monkeypatch, capsys):
        monkeypatch.delenv("BENCHMARK_ENABLED", raising=False)
        assert bm.main(["--run", "--tiers", "lem-vision"] + self._files(tmp_path)) == 0
        monkeypatch.setenv("BENCHMARK_ENABLED", "true")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        assert bm.main(["--run", "--tiers", "lem-vision"] + self._files(tmp_path)) == 1
        assert "OPENAI_API_KEY must be set" in capsys.readouterr().err

    def test_unreadable_inputs_error(self, tmp_path):
        assert bm.main(["--dry-run", "--tiers", "lem-vision", "--config",
                        str(tmp_path / "missing.yaml")]) == 1

    def test_a_real_run_writes_results_report_and_registry(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("BENCHMARK_ENABLED", "true")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        out = tmp_path / "out"
        out.mkdir()
        (out / "README.md").write_text("# r\n\n<!-- MODEL-REGISTRY:BEGIN -->\nold\n"
                                       "<!-- MODEL-REGISTRY:END -->\n")
        results = tmp_path / "results.json"
        with patch.object(media, "MediaProvider", return_value=FakeProvider()):
            rc = bm.main(["--run", "--tiers", "lem-vision,lem-image", "--run-id", "mm-x",
                          "--today", "2026-10-07", "--results-out", str(results)]
                         + self._files(tmp_path))
        assert rc == 2
        captured = capsys.readouterr()
        assert "RECOMMEND [lem-image] gpt-image-2 -> gpt-image-2.5-flare" in captured.out
        assert json.loads(results.read_text())["kind"] == "media"
        readme = (out / "README.md").read_text()
        assert "| Tier | Order | Provider |" in readme  # registry regenerated beside the report
        assert "`mm-x`" in readme
        # --render of the saved results replays the same publish without a provider
        rc = bm.main(["--render", str(results)] + self._files(tmp_path))
        assert rc == 2

    def test_render_refuses_an_outage(self, tmp_path, capsys):
        run = _run(FakeProvider(), cap=0.0)
        results = tmp_path / "r.json"
        results.write_text(json.dumps(run))
        assert bm.main(["--render", str(results)] + self._files(tmp_path)) == 1
        assert "harness outage" in capsys.readouterr().err

    def test_registry_regeneration_failure_never_fails_the_run(self, tmp_path, capsys):
        (tmp_path / "README.md").write_text("<!-- MODEL-REGISTRY:BEGIN -->\n"
                                            "<!-- MODEL-REGISTRY:END -->\n")
        bm._regenerate_registry(str(tmp_path), str(tmp_path / "missing.yaml"), "p", "s")
        assert "could not regenerate the model registry" in capsys.readouterr().err
        bm._regenerate_registry(str(tmp_path / "nowhere"), "c", "p", "s")  # no README: no-op
