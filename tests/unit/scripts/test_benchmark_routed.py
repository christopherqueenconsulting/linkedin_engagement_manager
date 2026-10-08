"""Metered text-tier candidates for the model benchmark (issue #2256).

Covers scripts/benchmark_routed.py and its wiring into `benchmark_models.py`: provider-qualified ids
routed to OpenRouter (or OpenAI direct), the spend plan and its two refusals, the in-run meter, the
price-based quota delta, and the rule that no key ever reaches output or an artifact. No provider
is ever called.
"""

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

import benchmark_models as bm  # noqa: E402
import benchmark_routed as routed  # noqa: E402

SECRET = "sk-or-v1-0123456789abcdefSECRET"
PRICES = json.loads((_ROOT / ".litellm" / "model_prices_snapshot.json").read_text())["models"]


def _suite(tier="lem-simple", cases=None):
    cases = cases or [
        {"id": "c1", "messages": [{"role": "user", "content": "x" * 300}],
         "params": {"temperature": 0.3, "max_tokens": 100},
         "assertions": [{"type": "max_chars", "value": 300}]},
        {"id": "c2", "messages": [{"role": "user", "content": "y" * 30}],
         "params": {"max_tokens": 5}, "judge": False,
         "assertions": [{"type": "max_chars", "value": 300}]},
    ]
    return {"tier": tier, "version": 1, "contract": "c", "judge_rubric": "r",
            "thresholds": bm.normalize_thresholds(None), "cases": cases}


def _completion(text="A fine answer.", prompt_tokens=50, completion_tokens=20):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text), finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                              total_tokens=prompt_tokens + completion_tokens))


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _opener(payload):
    def opener(request, timeout=None):
        assert request.headers["Authorization"] == f"Bearer {SECRET}"
        return _Resp(json.dumps(payload).encode())
    return opener


# ───────────────────────────── pure routing ─────────────────────────────

class TestRouting:
    def test_a_slash_means_metered_and_a_bare_tag_means_ollama(self):
        assert routed.is_routed("openai/gpt-5.4-mini")
        assert not routed.is_routed("gpt-oss:20b")
        assert routed.provider_of("anthropic/claude-sonnet-5") == "anthropic"
        assert routed.provider_of("gpt-oss:120b") == routed.ROUTE_OLLAMA

    def test_the_openai_route_reaches_only_openai_ids(self):
        assert routed.route_problem(["openai/gpt-4o"], "openai") is None
        assert "anthropic/x" in routed.route_problem(["openai/gpt-4o", "anthropic/x"], "openai")
        assert routed.route_problem(["anthropic/x"], "openrouter") is None
        assert "unknown" in routed.route_problem([], "bedrock")

    def test_wire_model_strips_the_qualifier_only_for_openai_direct(self):
        assert routed.wire_model("openai/gpt-4o", "openrouter") == "openai/gpt-4o"
        assert routed.wire_model("openai/gpt-4o", "openai") == "gpt-4o"

    def test_openai_params_are_translated_like_the_proxy_does(self):
        params = {"temperature": 0.7, "max_tokens": 200}
        assert routed.wire_params("openai/gpt-5.4-mini", params, "openai") == {
            "max_completion_tokens": 200}
        assert routed.wire_params("openai/gpt-4o", params, "openai") == {
            "temperature": 0.7, "max_completion_tokens": 200}
        assert routed.wire_params("openai/gpt-5.4-mini", {"temperature": 1}, "openai") == {
            "temperature": 1}
        assert routed.wire_params("openai/gpt-5.4-mini", params, "openrouter") == params
        assert params == {"temperature": 0.7, "max_tokens": 200}  # never mutated

    def test_redact_strips_every_configured_secret(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", SECRET)
        monkeypatch.setenv("OPENAI_API_KEY", "short")
        assert routed.redact(f"401 bad key {SECRET}!") == "401 bad key [redacted]!"
        assert routed.redact("a short word") == "a short word"
        assert routed.redact("x abcdefghij y", ["abcdefghij"]) == "x [redacted] y"


# ───────────────────────────── spend plan ─────────────────────────────

class TestPlan:
    def test_tokens_and_budgets(self):
        assert routed.estimate_input_tokens([{"content": "abcdef"}]) == 2 + 8
        assert routed.output_budget({"max_tokens": 50}) == 50
        assert routed.output_budget({"max_completion_tokens": 70}) == 70
        assert routed.output_budget({}) == routed.UNBOUNDED_OUTPUT_TOKENS
        assert routed.price_spec("openai/nope", PRICES) is None
        assert routed.price_spec("openai/gpt-4o", PRICES) == {"in": 2.5e-06, "out": 1e-05}

    def test_metered_items_are_priced_and_ollama_items_are_free(self):
        suites = {"lem-simple": _suite()}
        targets = [("lem-simple", "gpt-oss:20b", "champion"),
                   ("lem-simple", "openai/gpt-5.4-mini", "candidate")]
        plan = routed.plan_text_spend(suites, targets, PRICES, judge_enabled=True, judge_cap=10,
                                      judge_tokens=900)
        ollama, metered = plan["items"]
        assert ollama["est_usd"] == 0.0 and ollama["route"] == routed.ROUTE_OLLAMA
        expected = sum(routed.call_ceiling(c["messages"], c["params"],
                                           {"in": 7.5e-07, "out": 4.5e-06})
                       for c in suites["lem-simple"]["cases"])
        assert metered["est_usd"] == pytest.approx(expected)
        assert plan["judge"]["calls"] == 2  # one judgeable case per model
        assert plan["total_usd"] == pytest.approx(plan["routed_usd"] + plan["judge"]["est_usd"])

    def test_the_judge_cap_bounds_the_judge_line(self):
        suites = {"lem-simple": _suite()}
        targets = [("lem-simple", "openai/gpt-4o-mini", "champion"),
                   ("lem-simple", "openai/gpt-5.4-mini", "candidate")]
        plan = routed.plan_text_spend(suites, targets, PRICES, judge_enabled=True, judge_cap=1,
                                      judge_tokens=900)
        assert plan["judge"]["calls"] == 1
        off = routed.plan_text_spend(suites, targets, PRICES, judge_enabled=False, judge_cap=9,
                                     judge_tokens=900)
        assert off["judge"]["calls"] == 0 and off["total_usd"] == off["routed_usd"]

    def test_an_unpriced_model_or_judge_has_no_bound(self):
        plan = routed.plan_text_spend({"lem-simple": _suite()},
                                      [("lem-simple", "openai/gpt-99", "candidate")], {},
                                      judge_enabled=True, judge_cap=5, judge_tokens=900)
        assert plan["total_usd"] is None
        assert plan["unpriced"] == ["lem-medium (judge)", "openai/gpt-99"]
        assert "no pinned price" in routed.spend_refusal(plan, 2.0, "openrouter", None)
        assert "UNPRICED" in routed.render_plan(plan, 2.0, "openrouter", None)

    def _plan(self, routed_usd=0.5, total=0.6):
        return {"items": [], "judge": {"model": "lem-medium", "calls": 0, "est_usd": 0.0},
                "routed_usd": routed_usd, "total_usd": total, "unpriced": []}

    def test_refusals(self):
        assert "exceeds the $0.10 cap" in routed.spend_refusal(self._plan(), 0.10, "openai", None)
        assert routed.spend_refusal(self._plan(), 2.0, "openai", None) is None
        unread = routed.spend_refusal(self._plan(), 2.0, "openrouter", {"ok": False,
                                                                       "error": "HTTPError"})
        assert "could not be read" in unread and "HTTPError" in unread
        assert "could not be read" in routed.spend_refusal(self._plan(), 2.0, "openrouter", None)
        over = routed.spend_refusal(self._plan(), 2.0, "openrouter",
                                    {"ok": True, "limit_remaining": 0.25})
        assert "exceeds the key's remaining limit $0.25" in over
        assert routed.spend_refusal(self._plan(), 2.0, "openrouter",
                                    {"ok": True, "limit_remaining": None}) is None
        assert routed.spend_refusal(self._plan(), 2.0, "openrouter", None,
                                    check_limit=False) is None
        assert routed.spend_refusal(self._plan(routed_usd=0.0, total=0.1), 2.0, "openrouter",
                                    None) is None

    def test_effective_cap_takes_the_lower_bound(self):
        assert routed.effective_cap(2.0, "openrouter", {"limit_remaining": 0.5}) == 0.5
        assert routed.effective_cap(2.0, "openrouter", {"limit_remaining": None}) == 2.0
        assert routed.effective_cap(2.0, "openai", {"limit_remaining": 0.5}) == 2.0

    @pytest.mark.parametrize("key_limit,expected", [
        (None, "not read (dry run"),
        ({"ok": False, "error": "boom"}, "UNREADABLE (boom)"),
        ({"ok": True, "limit_remaining": None}, "none set on this key"),
        ({"ok": True, "limit_remaining": 18.84}, "$18.84 remaining"),
    ])
    def test_render_states_the_openrouter_limit(self, key_limit, expected):
        text = routed.render_plan(self._plan(), 2.0, "openrouter", key_limit)
        assert expected in text and "**Estimated total:** $0.6000 of a $2.00 cap" in text

    def test_metered_usage_carries_the_price(self):
        assert routed.metered_usage("openai/gpt-4o-mini", PRICES) == {
            "level": None, "label": "metered", "price_in": 1.5e-07, "price_out": 6e-07}


# ───────────────────────────── /auth/key ─────────────────────────────

class TestKeyLimit:
    def test_reads_the_remaining_limit(self):
        got = routed.fetch_key_limit(SECRET, opener=_opener(
            {"data": {"limit": 20, "limit_remaining": 18.84, "usage": 41.2}}))
        assert got == {"ok": True, "limit": 20, "limit_remaining": 18.84, "usage": 41.2}

    def test_no_limit_set_is_readable(self):
        got = routed.fetch_key_limit(SECRET, opener=_opener(
            {"data": {"limit": None, "limit_remaining": None}}))
        assert got["ok"] and got["limit_remaining"] is None

    @pytest.mark.parametrize("payload,reason", [
        ({"error": "nope"}, "no `data` object"),
        ({"data": {"limit": 20, "limit_remaining": "lots"}}, "not a number"),
        ({"data": {"limit": True, "limit_remaining": 1}}, "not a number"),
        ({"data": {"limit": 20}}, "`limit_remaining` is missing"),
    ])
    def test_an_unusable_answer_is_unreadable(self, payload, reason):
        got = routed.fetch_key_limit(SECRET, opener=_opener(payload))
        assert got["ok"] is False and reason in got["error"]

    def test_a_failure_never_echoes_the_key(self):
        def opener(request, timeout=None):
            raise OSError(f"connection refused while sending {SECRET}")
        got = routed.fetch_key_limit(SECRET, opener=opener)
        assert got["ok"] is False and SECRET not in got["error"] and "[redacted]" in got["error"]


# ───────────────────────────── the metered client ─────────────────────────────

def _client(route="openrouter", cap=2.0, sdk=None):
    meter = routed.SpendMeter(cap)
    client = routed.build_routed_client(bm.ProviderClient, route=route, api_key=SECRET,
                                        prices=PRICES, meter=meter)
    client._client = sdk or MagicMock()
    return client, meter


class TestRoutedClient:
    def test_targets_openrouter_and_meters_real_usage(self):
        client, meter = _client()
        client._client.chat.completions.create.return_value = _completion(
            prompt_tokens=1000, completion_tokens=100)
        out = client.complete("openai/gpt-4o", [{"role": "user", "content": "hi"}],
                              {"temperature": 0.5, "max_tokens": 50})
        assert client.base_url == routed.OPENROUTER_BASE_URL
        assert out["text"] == "A fine answer." and out["error"] is None
        kwargs = client._client.chat.completions.create.call_args.kwargs
        assert kwargs == {"model": "openai/gpt-4o", "messages": [{"role": "user", "content": "hi"}],
                          "temperature": 0.5, "max_tokens": 50}
        assert meter.spent == pytest.approx(1000 * 2.5e-06 + 100 * 1e-05)

    def test_openai_direct_strips_the_qualifier_and_translates(self, monkeypatch):
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
        client, _ = _client(route="openai")
        client._client.chat.completions.create.return_value = _completion()
        client.complete("openai/gpt-5.4-mini", [{"role": "user", "content": "hi"}],
                        {"temperature": 0.5, "max_tokens": 50})
        assert client.base_url == routed.OPENAI_BASE_URL
        kwargs = client._client.chat.completions.create.call_args.kwargs
        assert kwargs["model"] == "gpt-5.4-mini" and kwargs["max_completion_tokens"] == 50
        assert "temperature" not in kwargs

    def test_missing_usage_is_charged_at_the_ceiling(self):
        client, meter = _client()
        client._client.chat.completions.create.return_value = SimpleNamespace(
            choices=_completion().choices, usage=None)
        messages = [{"role": "user", "content": "hi"}]
        client.complete("openai/gpt-4o", messages, {"max_tokens": 50})
        assert meter.spent == pytest.approx(routed.call_ceiling(
            messages, {"max_tokens": 50}, {"in": 2.5e-06, "out": 1e-05}))

    def test_the_meter_refuses_a_call_past_the_cap_before_it_is_sent(self):
        client, meter = _client(cap=0.0001)
        out = client.complete("openai/gpt-4o", [{"role": "user", "content": "hi"}],
                              {"max_tokens": 4000})
        client._client.chat.completions.create.assert_not_called()
        assert out["text"] is None and "spend cap" in out["error"]
        assert meter.spent == 0.0

    def test_an_unpriced_model_is_never_called(self):
        client, _ = _client()
        out = client.complete("openai/gpt-99", [{"role": "user", "content": "hi"}], {})
        client._client.chat.completions.create.assert_not_called()
        assert "no pinned price" in out["error"]

    def test_a_provider_error_is_redacted(self):
        client, _ = _client()
        client._client.chat.completions.create.side_effect = RuntimeError(
            f"401 Incorrect API key provided: {SECRET}")
        out = client.complete("openai/gpt-4o", [{"role": "user", "content": "hi"}], {})
        assert SECRET not in out["error"] and "[redacted]" in out["error"]

    def test_the_one_client_is_attributed_openai(self):
        client = routed.build_routed_client(bm.ProviderClient, route="openrouter", api_key=SECRET,
                                            prices=PRICES, meter=routed.SpendMeter(1.0))
        with patch("cqc_lem.utilities.ai.client.AttributedOpenAI") as attributed:
            client._openai()
        kwargs = attributed.call_args.kwargs
        assert kwargs["base_url"] == routed.OPENROUTER_BASE_URL
        assert kwargs["default_headers"] == {"X-Title": "LEM model benchmark"}

    def test_dispatch_by_id_shape(self):
        ollama, metered = MagicMock(), MagicMock()
        provider = routed.DispatchingProvider(ollama, metered)
        provider.complete("gpt-oss:20b", [], {})
        provider.complete("openai/gpt-4o", [], {}, allow_budget_escalation=False)
        assert ollama.complete.call_args.args[0] == "gpt-oss:20b"
        assert metered.complete.call_args.kwargs == {"allow_budget_escalation": False}
        missing = routed.DispatchingProvider(None, None).complete("openai/gpt-4o", [], {})
        assert missing["text"] is None and "no provider configured" in missing["error"]


# ───────────────────────────── price-based quota delta ─────────────────────────────

class TestPriceDelta:
    def test_a_price_rise_is_an_increase_and_holds_off_lem_complex(self):
        cand = routed.metered_usage("openai/gpt-5.4-mini", PRICES)
        champ = routed.metered_usage("openai/gpt-4o-mini", PRICES)
        delta = bm.usage_delta(cand, champ, candidate_model="openai/gpt-5.4-mini",
                               champion_model="openai/gpt-4o-mini")
        assert delta["direction"] == bm.USAGE_UP
        assert "5.0x in / 7.5x out" in delta["summary"] and "price increase" in delta["summary"]
        assert bm.quota_policy("lem-simple", delta)["decision"] == bm.POLICY_HOLD

    def test_a_price_drop_is_adoptable(self):
        cand = routed.metered_usage("openai/gpt-6.1-sol", PRICES)
        champ = routed.metered_usage("openai/gpt-4o", PRICES)
        delta = bm.usage_delta(cand, champ)
        assert delta["direction"] == bm.USAGE_DOWN
        assert bm.quota_policy("lem-complex", delta)["decision"] == bm.POLICY_ADOPT

    def test_flat_and_mixed(self):
        same = {"price_in": 1e-06, "price_out": 2e-06}
        assert bm.usage_delta(same, dict(same))["direction"] == bm.USAGE_FLAT
        mixed = bm.price_delta((5e-07, 3e-06), (1e-06, 2e-06))
        assert mixed["direction"] == bm.USAGE_UP  # a cheaper input does not pay for dearer output
        zero = bm.price_delta((0.0, 1e-06), (0.0, 1e-06))
        assert "flat on price" in zero["summary"]

    def test_metered_versus_ollama_stays_unknown(self):
        delta = bm.usage_delta(routed.metered_usage("openai/gpt-4o-mini", PRICES),
                               {"level": 2, "label": "medium"})
        assert delta["direction"] == bm.USAGE_UNKNOWN

    def test_usage_name_prints_the_price(self):
        assert bm.usage_name(routed.metered_usage("openai/gpt-4o-mini", PRICES)) == \
            "metered $0.15/$0.60 per 1M"


# ───────────────────────────── CLI ─────────────────────────────

def _no_network(*args, **kwargs):
    raise AssertionError("the network was touched")


@pytest.fixture
def offline(monkeypatch):
    """Fail the test on any HTTP the harness could make, and clear every key."""
    monkeypatch.setattr("urllib.request.urlopen", _no_network)
    for name in routed.SECRET_ENVS + ("BENCHMARK_ENABLED", "BENCHMARK_MAX_SPEND_USD",
                                      "BENCHMARK_TEXT_PROVIDER", "OLLAMA_CLOUD_URL",
                                      "POSTHOG_API_KEY"):
        monkeypatch.delenv(name, raising=False)


class TestCli:
    def test_dry_run_prints_the_plan_per_model_and_touches_nothing(self, offline, tmp_path,
                                                                   capsys):
        """Issue #2256 acceptance box 1."""
        with patch.object(bm.ProviderClient, "_openai", side_effect=_no_network):
            rc = bm.main(["--dry-run", "--models", "openai/gpt-5.4-mini,openai/gpt-6.1-sol",
                          "--tiers", "lem-complex,lem-medium", "--out-dir", str(tmp_path)])
        out = capsys.readouterr().out
        assert rc == 0
        for tier in ("lem-complex", "lem-medium"):
            for model in ("openai/gpt-5.4-mini", "openai/gpt-6.1-sol"):
                assert f"| {tier} | `{model}` | candidate | metered |" in out
        assert "dry run: no provider was called and nothing was written" in out
        assert list(tmp_path.iterdir()) == []

    def test_a_dry_run_over_the_cap_exits_1(self, offline, capsys):
        rc = bm.main(["--dry-run", "--models", "openai/gpt-6.1-sol", "--tiers", "lem-complex",
                      "--max-spend-usd", "0.01"])
        assert rc == 1
        assert "exceeds the $0.01 cap" in capsys.readouterr().err

    def _run(self, monkeypatch, tmp_path, *extra, key_limit=None):
        monkeypatch.setenv("BENCHMARK_ENABLED", "true")
        monkeypatch.setenv("OPENROUTER_API_KEY", SECRET)
        sdk = MagicMock()
        sdk.chat.completions.create.return_value = _completion()
        limit = key_limit if key_limit is not None else {"ok": True, "limit_remaining": 18.84}
        with patch.object(routed, "fetch_key_limit", return_value=limit) as fetch, \
                patch("cqc_lem.utilities.ai.client.AttributedOpenAI", return_value=sdk):
            rc = bm.main(["--run", "--champion-source", "metered", "--tiers", "lem-simple",
                          "--no-judge", "--no-usage-levels", "--out-dir", str(tmp_path),
                          "--results-out", str(tmp_path / "results.json"), *extra])
        return rc, sdk, fetch

    def test_a_plan_over_the_cap_exits_1_before_any_completion(self, offline, monkeypatch,
                                                               tmp_path, capsys):
        """Issue #2256 acceptance box 2."""
        rc, sdk, _ = self._run(monkeypatch, tmp_path, "--models", "openai/gpt-5.4-mini",
                               "--max-spend-usd", "0.0001")
        assert rc == 1
        sdk.chat.completions.create.assert_not_called()
        assert "exceeds the $0.00 cap" in capsys.readouterr().err

    def test_a_plan_over_openrouters_limit_exits_1_before_any_completion(self, offline,
                                                                         monkeypatch, tmp_path,
                                                                         capsys):
        """Issue #2256 acceptance box 3, first half."""
        rc, sdk, fetch = self._run(monkeypatch, tmp_path, "--models", "openai/gpt-5.4-mini",
                                   key_limit={"ok": True, "limit_remaining": 0.0001})
        assert rc == 1
        fetch.assert_called_once_with(SECRET)
        sdk.chat.completions.create.assert_not_called()
        assert "exceeds the key's remaining limit" in capsys.readouterr().err

    def test_an_unreadable_key_limit_exits_1_before_any_completion(self, offline, monkeypatch,
                                                                   tmp_path, capsys):
        """Issue #2256 acceptance box 3, second half - through the real fetch, HTTP mocked."""
        monkeypatch.setenv("BENCHMARK_ENABLED", "true")
        monkeypatch.setenv("OPENROUTER_API_KEY", SECRET)
        sdk = MagicMock()

        def refused(request, timeout=None):
            raise OSError(f"403 Forbidden for {SECRET}")
        monkeypatch.setattr("urllib.request.urlopen", refused)
        with patch("cqc_lem.utilities.ai.client.AttributedOpenAI", return_value=sdk):
            rc = bm.main(["--run", "--champion-source", "metered", "--tiers", "lem-simple",
                          "--models", "openai/gpt-5.4-mini", "--no-judge", "--no-usage-levels",
                          "--out-dir", str(tmp_path)])
        captured = capsys.readouterr()
        assert rc == 1
        sdk.chat.completions.create.assert_not_called()
        assert "could not be read from /api/v1/auth/key" in captured.err
        assert SECRET not in captured.out + captured.err

    def test_a_missing_key_exits_1(self, offline, monkeypatch, capsys):
        monkeypatch.setenv("BENCHMARK_ENABLED", "true")
        rc = bm.main(["--run", "--models", "openai/gpt-4o", "--tiers", "lem-simple",
                      "--champions", "lem-simple=openai/gpt-4o-mini", "--no-judge"])
        assert rc == 1
        assert "OPENROUTER_API_KEY must be set" in capsys.readouterr().err

    def test_the_openai_route_refuses_a_foreign_id(self, offline, capsys):
        rc = bm.main(["--dry-run", "--text-provider", "openai", "--tiers", "lem-simple",
                      "--models", "anthropic/claude-sonnet-5"])
        assert rc == 1
        assert "reaches only openai/*" in capsys.readouterr().err

    def test_the_key_never_reaches_output_or_an_artifact(self, offline, monkeypatch, tmp_path,
                                                         capsys):
        """Issue #2256 acceptance box 4: a provider error quoting the key is redacted.

        Everywhere it could land: stdout, stderr, the results JSON and the report.
        """
        monkeypatch.setenv("BENCHMARK_ENABLED", "true")
        monkeypatch.setenv("OPENROUTER_API_KEY", SECRET)
        sdk = MagicMock()

        def create(model, messages, **params):
            if model == "openai/gpt-5.4-mini":
                raise RuntimeError(f"401 invalid key {SECRET}")
            return _completion()
        sdk.chat.completions.create.side_effect = create
        with patch.object(routed, "fetch_key_limit",
                          return_value={"ok": True, "limit_remaining": 18.84}), \
                patch("cqc_lem.utilities.ai.client.AttributedOpenAI", return_value=sdk):
            rc = bm.main(["--run", "--champion-source", "metered", "--tiers", "lem-simple",
                          "--models", "openai/gpt-5.4-mini", "--no-judge", "--no-usage-levels",
                          "--out-dir", str(tmp_path),
                          "--results-out", str(tmp_path / "results.json")])
        captured = capsys.readouterr()
        assert rc in (0, 2)
        assert sdk.chat.completions.create.called
        artifacts = [p.read_text() for p in tmp_path.iterdir()]
        assert any("[redacted]" in text for text in artifacts)
        for text in [captured.out, captured.err] + artifacts:
            assert SECRET not in text
        results = json.loads((tmp_path / "results.json").read_text())
        assert results["metered"]["route"] == "openrouter"
        assert results["metered"]["spent_usd"] > 0
        assert results["champions"] == {"lem-simple": "openai/gpt-4o-mini"}
        report = next(p for p in tmp_path.iterdir() if p.suffix == ".md" and p.name != "README.md")
        assert "**Metered models:** via `openrouter`" in report.read_text()

    def test_an_ollama_tag_beside_a_metered_id_still_needs_ollama_credentials(
            self, offline, monkeypatch, tmp_path, capsys):
        monkeypatch.setenv("BENCHMARK_ENABLED", "true")
        monkeypatch.setenv("OPENROUTER_API_KEY", SECRET)
        with patch.object(routed, "fetch_key_limit",
                          return_value={"ok": True, "limit_remaining": 18.84}):
            rc = bm.main(["--run", "--tiers", "lem-simple", "--models", "openai/gpt-5.4-mini",
                          "--no-judge", "--no-usage-levels", "--out-dir", str(tmp_path)])
        assert rc == 1
        assert "OLLAMA_CLOUD_URL / OLLAMA_CLOUD_API_KEY must be set" in capsys.readouterr().err


class TestMeteredChampions:
    def test_the_live_config_names_the_openai_deployment_of_every_text_tier(self):
        text = (_ROOT / ".litellm" / "config.yaml").read_text()
        assert bm.metered_champions_from_config(text, list(bm.TIERS)) == {
            "lem-simple": "openai/gpt-4o-mini", "lem-medium": "openai/gpt-4o-mini",
            "lem-complex": "openai/gpt-4o", "lem-router": "openai/gpt-4o-mini"}

    def test_plan_targets_skips_a_candidate_that_is_its_own_champion(self):
        logged = []
        targets = bm.plan_targets({"lem-simple": _suite()}, ["openai/gpt-4o-mini", "x"],
                                  {"lem-simple": "openai/gpt-4o-mini"}, log=logged.append)
        assert targets == [("lem-simple", "openai/gpt-4o-mini", "champion"),
                           ("lem-simple", "x", "candidate")]
        assert "already serves lem-simple" in logged[0]
