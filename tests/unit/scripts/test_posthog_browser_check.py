"""Unit tests for scripts/posthog_browser_check.py — the browser-install preflight (issue #1676).

Two properties carry the whole script. A bundle that shipped WITHOUT the inlined project key must
FAIL (that is the silent build-arg failure it exists to catch), and an ingest query that returns no
rows must FAIL too — the opposite of `posthog_key_check.py`, where an empty result is a pass.
"""

import importlib.util
import json
import pathlib
import sys

import pytest

pytestmark = pytest.mark.unit

_ROOT = pathlib.Path(__file__).resolve().parents[3]
_PATH = _ROOT / "scripts" / "posthog_browser_check.py"
_spec = importlib.util.spec_from_file_location("posthog_browser_check", _PATH)
bc = importlib.util.module_from_spec(_spec)
sys.modules["posthog_browser_check"] = bc
_spec.loader.exec_module(bc)

SITE = "https://lem.example.com"
# Deliberately repetitive, low-entropy stand-ins: they only have to satisfy `_TOKEN_RE`
# (`phc_` + 20 alphanumerics). A realistic-looking random token here trips the GitGuardian scan.
TOKEN = "phc_testtesttesttesttest1111"
OTHER_TOKEN = "phc_othrothrothrothrothr2222"

INDEX_HTML = """<!doctype html><html><head>
<script type="module" crossorigin src="/assets/index-abc123.js"></script>
<link rel="modulepreload" crossorigin href="/assets/runtime-def456.js">
<link rel="stylesheet" href="/assets/index-abc123.css">
</head><body><div id="root"></div></body></html>"""

# The shape a current build has: captures addressed at the reverse proxy, with `ui_host` — a
# `*.posthog.com` URL that is NOT an ingestion host — inlined right beside it (issue #1677).
PROXY = bc.DEFAULT_API_HOST
UI_HOST = "https://us.posthog.com"
BUNDLE_WITH_KEY = (
    f'e.init("{TOKEN}",{{api_host:"{PROXY}",ui_host:"{UI_HOST}",autocapture:!0}})'
)
BUNDLE_WITHOUT_KEY = f'e.init(void 0,{{api_host:"{PROXY}",ui_host:"{UI_HOST}",autocapture:!0}})'
# A bundle from before the switch — the one shape `api-host` must not wave through.
BUNDLE_DIRECT_HOST = (
    f'e.init("{TOKEN}",{{api_host:"https://us.i.posthog.com",autocapture:!0}})'
)


class _FakeSite:
    """Replays canned (status, body) answers per URL and records the fetch order."""

    def __init__(self, pages=None, raises=None):
        self.pages = dict(pages or {})
        self.raises = raises
        self.calls = []

    def get(self, url):
        self.calls.append(url)
        if self.raises is not None:
            raise self.raises
        return self.pages.get(url, (404, ""))


class _FakePostHog:
    """Replays one canned (status, body) answer and records the query it was handed."""

    def __init__(self, answer=(200, "{}"), raises=None):
        self.answer = answer
        self.raises = raises
        self.calls = []

    def query(self, url, hogql, api_key):
        self.calls.append({"url": url, "hogql": hogql, "api_key": api_key})
        if self.raises is not None:
            raise self.raises
        return self.answer


def _rows(views=12, people=3, last_seen="2026-08-18T16:32:32Z"):
    return json.dumps({"results": [[views, people, last_seen]]})


def _site(with_key=True):
    bundle = BUNDLE_WITH_KEY if with_key else BUNDLE_WITHOUT_KEY
    return _FakeSite({
        f"{SITE}/": (200, INDEX_HTML),
        f"{SITE}/assets/index-abc123.js": (200, bundle),
        f"{SITE}/assets/runtime-def456.js": (200, "export const x=1"),
    })


def _named(results):
    return {result["name"]: result for result in results}


# --- parsing -----------------------------------------------------------------------------------

def test_parse_asset_urls_takes_module_scripts_and_preloads_but_not_css():
    urls = bc.parse_asset_urls(INDEX_HTML, f"{SITE}/")
    assert urls == [f"{SITE}/assets/index-abc123.js", f"{SITE}/assets/runtime-def456.js"]


def test_parse_asset_urls_dedupes_and_honours_the_cap():
    html = '<script src="/a.js"></script><script src="/a.js"></script><script src="/b.js"></script>'
    assert bc.parse_asset_urls(html, f"{SITE}/") == [f"{SITE}/a.js", f"{SITE}/b.js"]
    assert bc.parse_asset_urls(html, f"{SITE}/", limit=1) == [f"{SITE}/a.js"]


def test_find_project_tokens_and_hosts():
    assert bc.find_project_tokens(BUNDLE_WITH_KEY) == [TOKEN]
    assert bc.find_project_tokens(BUNDLE_WITHOUT_KEY) == []
    assert bc.find_ingest_hosts(BUNDLE_DIRECT_HOST) == ["https://us.i.posthog.com"]


def test_find_ingest_hosts_never_reads_ui_host_as_an_ingestion_host():
    """`ui_host` is a `*.posthog.com` URL that captures never go to (issue #1677).

    The looser pattern this replaces matched it, so a bundle correctly pointed at the proxy would
    have PASSed while REPORTING `https://us.posthog.com` as its ingestion host — a confident wrong
    answer, which is worse for a preflight than a failure.
    """
    assert bc.find_ingest_hosts(BUNDLE_WITH_KEY) == []


def test_find_ingest_hosts_finds_a_proxy_only_because_it_is_expected():
    """A proxy origin is a LEM domain with nothing PostHog-shaped about it."""
    assert bc.find_ingest_hosts(BUNDLE_WITH_KEY, PROXY) == [PROXY]
    assert bc.find_ingest_hosts(BUNDLE_DIRECT_HOST, PROXY) == ["https://us.i.posthog.com"]


def test_site_host_tolerates_a_missing_scheme_and_a_port():
    assert bc.site_host("lem.example.com") == "lem.example.com"
    assert bc.site_host("https://LEM.example.com:8443/x") == "lem.example.com"


def test_site_host_rejects_a_typo_before_anything_is_requested():
    assert bc.site_host("not a url") == ""
    assert bc.site_host("ftp://lem.example.com") == ""
    assert bc.site_host("") == ""


def test_mask_token_never_prints_a_whole_long_token():
    masked = bc.mask_token(TOKEN)
    assert masked != TOKEN and TOKEN[:9] in masked and TOKEN[-4:] in masked
    assert bc.mask_token("") == "-"


# --- classification ----------------------------------------------------------------------------

def test_classify_shell_fails_on_a_bad_status_or_no_assets():
    assert bc.classify_shell(200, ["a.js"])["ok"] is True
    assert bc.classify_shell(503, ["a.js"])["ok"] is False
    empty = bc.classify_shell(200, [])
    assert empty["ok"] is False and "no JS asset" in empty["detail"]


def test_classify_bundle_fails_when_no_token_is_inlined():
    result = bc.classify_bundle([])
    assert result["ok"] is False
    assert "VITE_POSTHOG_KEY" in result["detail"]


def test_classify_bundle_passes_on_presence_when_no_expectation_is_supplied():
    assert bc.classify_bundle([TOKEN])["ok"] is True


def test_classify_bundle_fails_on_a_token_from_a_different_project():
    result = bc.classify_bundle([OTHER_TOKEN], expected=TOKEN)
    assert result["ok"] is False
    assert "different project" in result["detail"]
    assert OTHER_TOKEN not in result["detail"]  # masked, never echoed whole


def test_classify_bundle_passes_when_the_token_matches():
    assert bc.classify_bundle([TOKEN], expected=TOKEN)["ok"] is True


def test_classify_api_host_fails_when_nothing_is_inlined():
    assert bc.classify_api_host([])["ok"] is False
    assert bc.classify_api_host(["https://us.i.posthog.com"])["ok"] is True


def test_classify_api_host_names_the_expected_proxy_when_nothing_is_inlined():
    result = bc.classify_api_host([], expected=PROXY)
    assert result["ok"] is False and PROXY in result["detail"]


def test_classify_api_host_fails_a_bundle_still_addressed_at_posthog_directly():
    """A stale `api_host` is invisible from PostHog's side (issue #1677).

    Events keep arriving from every visitor who was not blocking the direct domain — which is
    exactly the population that never had a problem — so only the artifact can report it.
    """
    result = bc.classify_api_host(["https://us.i.posthog.com"], expected=PROXY)
    assert result["ok"] is False
    assert "https://us.i.posthog.com" in result["detail"] and PROXY in result["detail"]


def test_classify_api_host_passes_when_the_proxy_is_the_inlined_host():
    result = bc.classify_api_host([PROXY], expected=PROXY)
    assert result["ok"] is True and result["detail"] == f"api_host: {PROXY}"


# --- the ingest query --------------------------------------------------------------------------

def test_pageview_hogql_pins_the_browser_lib_and_the_site_host():
    query = bc.pageview_hogql("lem.example.com", 6)
    assert "event = '$pageview'" in query
    assert "properties.$lib = 'web'" in query  # server-side captures must not mask a dead SPA
    assert "properties.$host = 'lem.example.com'" in query
    assert "INTERVAL 6 HOUR" in query


def test_pageview_hogql_floors_the_window_and_strips_quotes_from_the_host():
    assert "INTERVAL 24 HOUR" in bc.pageview_hogql("h", 0)
    assert "INTERVAL 1 HOUR" in bc.pageview_hogql("h", -3)
    assert "properties.$host = 'ab'" in bc.pageview_hogql("a'b", 1)


def test_classify_ingest_fails_on_zero_rows_unlike_the_key_preflight():
    empty = bc.classify_ingest(200, json.dumps({"results": []}), "h", 24)
    assert empty["ok"] is False and "not reaching PostHog" in empty["detail"]
    zero = bc.classify_ingest(200, _rows(views=0, people=0), "h", 24)
    assert zero["ok"] is False and "0 $pageview" in zero["detail"]


def test_classify_ingest_passes_with_rows_and_reports_the_volume():
    result = bc.classify_ingest(200, _rows(), "h", 24)
    assert result["ok"] is True and "12 $pageview" in result["detail"]


def test_classify_ingest_names_the_likely_cause_of_an_http_failure():
    assert "query:read" in bc.classify_ingest(403, "denied", "h", 24)["detail"]
    assert bc.classify_ingest(401, "", "h", 24)["ok"] is False


def test_parse_query_rows_survives_a_non_json_body():
    assert bc.parse_query_rows("<html>gateway timeout</html>") == []
    assert bc.parse_query_rows("") == []


# --- the artifact run --------------------------------------------------------------------------

def test_check_artifacts_passes_on_a_correctly_built_bundle():
    results = _named(bc.check_artifacts(SITE, _site(), expected_token=TOKEN,
                                        expected_api_host=PROXY))
    assert [r["ok"] for r in results.values()] == [True, True, True]


def test_check_artifacts_fails_the_bundle_when_the_build_arg_was_empty():
    results = _named(bc.check_artifacts(SITE, _site(with_key=False), expected_token=TOKEN,
                                        expected_api_host=PROXY))
    assert results["shell"]["ok"] is True
    assert results["bundle"]["ok"] is False
    assert results["api-host"]["ok"] is True  # the host literal survives a keyless build


def test_check_artifacts_fails_api_host_on_a_bundle_that_predates_the_proxy_switch():
    site = _FakeSite({
        f"{SITE}/": (200, INDEX_HTML),
        f"{SITE}/assets/index-abc123.js": (200, BUNDLE_DIRECT_HOST),
        f"{SITE}/assets/runtime-def456.js": (200, ""),
    })
    results = _named(bc.check_artifacts(SITE, site, expected_token=TOKEN,
                                        expected_api_host=PROXY))
    assert results["bundle"]["ok"] is True  # the key is fine; only the destination is stale
    assert results["api-host"]["ok"] is False


def test_check_artifacts_reports_the_proxy_and_never_the_ui_host():
    """End-to-end over a real-shaped bundle carrying BOTH origins (issue #1677).

    `find_ingest_hosts` is asserted on this above, but the value of the `api-host` line is what an
    operator reads, so the whole run has to be the thing that never names `ui_host`.
    """
    results = _named(bc.check_artifacts(SITE, _site(), expected_token=TOKEN,
                                        expected_api_host=PROXY))
    detail = results["api-host"]["detail"]
    assert results["api-host"]["ok"] is True
    assert detail == f"api_host: {PROXY}"
    assert UI_HOST not in detail


def test_check_artifacts_never_fetches_the_lazy_posthog_chunk():
    site = _site()
    bc.check_artifacts(SITE, site, expected_token=TOKEN)
    assert not any("posthog" in url for url in site.calls)


def test_check_artifacts_reports_every_check_failed_when_the_site_is_unreachable():
    results = bc.check_artifacts(SITE, _FakeSite(raises=RuntimeError("dns")), expected_token=TOKEN)
    assert [result["ok"] for result in results] == [False, False, False]
    assert all("dns" in result["detail"] for result in results)


def test_check_artifacts_does_not_claim_a_bundle_pass_when_the_shell_is_down():
    site = _FakeSite({f"{SITE}/": (502, "")})
    results = _named(bc.check_artifacts(SITE, site, expected_token=TOKEN))
    assert [result["ok"] for result in results.values()] == [False, False, False]
    assert "not checked" in results["bundle"]["detail"]


def test_check_artifacts_ignores_an_asset_that_will_not_load():
    site = _FakeSite({
        f"{SITE}/": (200, INDEX_HTML),
        f"{SITE}/assets/index-abc123.js": (404, "nope"),
        f"{SITE}/assets/runtime-def456.js": (200, BUNDLE_WITH_KEY),
    })
    results = _named(bc.check_artifacts(SITE, site, expected_token=TOKEN))
    assert results["bundle"]["ok"] is True


# --- the ingest run ----------------------------------------------------------------------------

def test_check_ingest_fails_loudly_with_no_key(monkeypatch):
    for name in ("POSTHOG_QUERY_API_KEY", "POSTHOG_PERSONAL_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    reader = _FakePostHog()
    result = bc.check_ingest("h", reader, "475262", "https://us.posthog.com")
    assert result["ok"] is False and "POSTHOG_QUERY_API_KEY" in result["detail"]
    assert reader.calls == []


def test_check_ingest_names_the_env_var_that_supplied_the_key(monkeypatch):
    monkeypatch.delenv("POSTHOG_QUERY_API_KEY", raising=False)
    monkeypatch.setenv("POSTHOG_PERSONAL_API_KEY", "phx_shared")
    reader = _FakePostHog(answer=(200, _rows()))
    result = bc.check_ingest("lem.example.com", reader, "475262", "https://us.posthog.com/")
    assert result["ok"] is True
    assert "POSTHOG_PERSONAL_API_KEY" in result["detail"]
    assert reader.calls[0]["url"] == "https://us.posthog.com/api/projects/475262/query/"
    assert reader.calls[0]["api_key"] == "phx_shared"


def test_check_ingest_turns_a_transport_error_into_one_fail(monkeypatch):
    monkeypatch.setenv("POSTHOG_QUERY_API_KEY", "phx_scoped")
    result = bc.check_ingest("h", _FakePostHog(raises=RuntimeError("timeout")),
                             "475262", "https://us.posthog.com")
    assert result["ok"] is False and "timeout" in result["detail"]


# --- reporting ---------------------------------------------------------------------------------

def test_exit_code_is_one_when_nothing_was_checked():
    assert bc.exit_code([]) == 1
    assert bc.exit_code([{"ok": True}]) == 0
    assert bc.exit_code([{"ok": True}, {"ok": False}]) == 1


def test_summarize_and_format_result():
    assert bc.summarize([{"ok": True}, {"ok": False}]) == "1 passed, 1 failed"
    line = bc.format_result({"name": "bundle", "ok": False, "detail": "no phc_ token"})
    assert line.startswith("FAIL  bundle")


# --- CLI ---------------------------------------------------------------------------------------

def test_main_skip_ingest_makes_no_posthog_request(monkeypatch, capsys):
    monkeypatch.setattr(bc, "SiteReader", lambda timeout=30: _site())
    monkeypatch.setattr(bc, "PostHogReader",
                        lambda timeout=30: pytest.fail("ingest must not run"))
    code = main_code = bc.main(["--site", SITE, "--project-token", TOKEN, "--skip-ingest"])
    out = capsys.readouterr().out
    assert main_code == 0 and code == 0
    assert "3 passed, 0 failed" in out
    assert "ingest" not in out


def test_main_returns_one_when_the_bundle_lost_its_key(monkeypatch, capsys):
    monkeypatch.setattr(bc, "SiteReader", lambda timeout=30: _site(with_key=False))
    assert bc.main(["--site", SITE, "--project-token", TOKEN, "--skip-ingest"]) == 1
    assert "FAIL  bundle" in capsys.readouterr().out


def test_main_runs_the_full_chain_and_passes(monkeypatch, capsys):
    monkeypatch.setenv("POSTHOG_QUERY_API_KEY", "phx_scoped")
    monkeypatch.setattr(bc, "SiteReader", lambda timeout=30: _site())
    monkeypatch.setattr(bc, "PostHogReader", lambda timeout=30: _FakePostHog((200, _rows())))
    assert bc.main(["--site", SITE, "--project-token", TOKEN, "--hours", "6"]) == 0
    assert "4 passed, 0 failed" in capsys.readouterr().out


def test_main_rejects_a_site_without_a_hostname_and_a_zero_asset_cap(capsys):
    assert bc.main(["--site", "not a url"]) == 2
    assert bc.main(["--site", SITE, "--max-assets", "0", "--skip-ingest"]) == 2


def test_main_defaults_the_expected_api_host_to_the_proxy(monkeypatch, capsys):
    """With no `--api-host` and no `$VITE_POSTHOG_HOST`, the proxy is what a build must carry."""
    monkeypatch.delenv("VITE_POSTHOG_HOST", raising=False)
    site = _FakeSite({
        f"{SITE}/": (200, INDEX_HTML),
        f"{SITE}/assets/index-abc123.js": (200, BUNDLE_DIRECT_HOST),
        f"{SITE}/assets/runtime-def456.js": (200, ""),
    })
    monkeypatch.setattr(bc, "SiteReader", lambda timeout=30: site)
    assert bc.main(["--site", SITE, "--project-token", TOKEN, "--skip-ingest"]) == 1
    assert "FAIL  api-host" in capsys.readouterr().out


def test_main_lets_the_build_env_move_the_expected_api_host(monkeypatch, capsys):
    """`VITE_POSTHOG_HOST` is what a build actually reads, so it is what the check follows."""
    monkeypatch.setenv("VITE_POSTHOG_HOST", "https://us.i.posthog.com")
    site = _FakeSite({
        f"{SITE}/": (200, INDEX_HTML),
        f"{SITE}/assets/index-abc123.js": (200, BUNDLE_DIRECT_HOST),
        f"{SITE}/assets/runtime-def456.js": (200, ""),
    })
    monkeypatch.setattr(bc, "SiteReader", lambda timeout=30: site)
    assert bc.main(["--site", SITE, "--project-token", TOKEN, "--skip-ingest"]) == 0
    assert "3 passed, 0 failed" in capsys.readouterr().out


def test_main_defaults_the_expected_token_to_the_build_env(monkeypatch, capsys):
    monkeypatch.setenv("VITE_POSTHOG_KEY", OTHER_TOKEN)
    monkeypatch.setattr(bc, "SiteReader", lambda timeout=30: _site())
    assert bc.main(["--site", SITE, "--skip-ingest"]) == 1
    assert "different project" in capsys.readouterr().out


# --- the I/O clients ----------------------------------------------------------------------------

class _FakeResponse:
    """The one attribute pair the readers touch on a `requests` response."""

    def __init__(self, status_code: int, text: str) -> None:
        self.status_code = status_code
        self.text = text


def _realistic_query_body() -> str:
    """A HogQL response the size PostHog actually returns — well over a kilobyte.

    `results` is a few dozen bytes of it; `clickhouse`, `hogql`, `modifiers` and `timings` are the
    rest. That is why the reader must not cap the body.
    """
    return json.dumps({
        "cache_key": "cache_" + "0" * 32,
        "clickhouse": "SELECT count() AS views FROM events WHERE " + "and(equals(a, b), " * 12 + "1" + ")" * 12,
        "columns": ["views", "people", "last_seen"],
        "hogql": bc.pageview_hogql("lem.example.com", 24),
        "is_cached": False,
        "modifiers": {f"modifier_{index}": "auto" for index in range(20)},
        "results": [[717, 8, "2026-08-18T16:32:11Z"]],
        "timings": [{"k": f".step_{index}", "t": 0.01} for index in range(10)],
        "types": [["views", "UInt64"], ["people", "UInt64"], ["last_seen", "DateTime"]],
    })


def test_posthog_reader_returns_the_whole_body_so_a_healthy_answer_still_parses(monkeypatch):
    """A capped body reads a WORKING install as the very defect this script disproves.

    `json.loads` fails on the truncation, `classify_ingest` sees zero rows, and it prints "the
    browser is not reaching PostHog" about a healthy deployment.
    """
    import requests

    body = _realistic_query_body()
    assert len(body) > 600, "the regression only bites on a body longer than the old cap"
    sent = {}

    def _post(url, headers=None, json=None, timeout=None):  # noqa: A002 - mirrors requests' kwarg
        sent.update(url=url, headers=headers, payload=json, timeout=timeout)
        return _FakeResponse(200, body)

    monkeypatch.setattr(requests, "post", _post)
    status, returned = bc.PostHogReader(timeout=7).query("https://us.posthog.com/api/q/", "SELECT 1",
                                                         "phx_scoped")
    assert (status, returned) == (200, body)
    assert sent["timeout"] == 7
    assert sent["payload"]["query"]["kind"] == "HogQLQuery"
    result = bc.classify_ingest(status, returned, "lem.example.com", 24)
    assert result["ok"] is True and "717 $pageview" in result["detail"]


def test_site_reader_gets_the_url_with_no_cache(monkeypatch):
    import requests

    seen = {}

    def _get(url, timeout=None, headers=None):
        seen.update(url=url, timeout=timeout, headers=headers)
        return _FakeResponse(200, INDEX_HTML)

    monkeypatch.setattr(requests, "get", _get)
    assert bc.SiteReader(timeout=5).get(SITE) == (200, INDEX_HTML)
    assert seen["headers"]["Cache-Control"] == "no-cache"


# --- the --site argument ------------------------------------------------------------------------

def test_normalize_site_supplies_the_scheme_site_host_already_assumed():
    assert bc.normalize_site("lem.example.com") == "https://lem.example.com"
    assert bc.normalize_site("http://lem.example.com") == "http://lem.example.com"


def test_main_fetches_a_scheme_less_site_instead_of_failing_transport(monkeypatch, capsys):
    """A bare hostname must be FETCHED, not just validated.

    `site_host` accepts one, so a site that is up would otherwise report three `request failed`
    lines from `requests.MissingSchema`.
    """
    site = _site()
    monkeypatch.setattr(bc, "SiteReader", lambda timeout=30: site)
    assert bc.main(["--site", "lem.example.com", "--project-token", TOKEN, "--skip-ingest"]) == 0
    assert site.calls[0] == "https://lem.example.com/"
    assert "3 passed, 0 failed" in capsys.readouterr().out
