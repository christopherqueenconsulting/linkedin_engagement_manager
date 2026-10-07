"""The live research probe (issue #2255): scripts/probe_research.py. Every call is mocked."""

import pathlib
import sys
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_SCRIPTS = pathlib.Path(__file__).resolve().parents[3] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import probe_research as probe  # noqa: E402


def _run(result=None, exc=None, route="proxy"):
    lines = []

    def call(route_, query, max_sources):
        assert route_ == route and "LinkedIn post" in query and max_sources == 5
        if exc:
            raise exc
        return result
    rc = probe.probe(route, "AI agents", 5, call=call, out=lines.append)
    return rc, "\n".join(lines)


class TestProbe:
    def test_findings_with_a_citation_pass(self):
        rc, out = _run({"findings": "41% of SMBs ...", "sources": [{"url": "https://a.example"}]})
        assert rc == 0
        assert "citations=1" in out and "https://a.example" in out and "PASS" in out

    def test_findings_without_a_citation_fail(self):
        rc, out = _run({"findings": "uncited", "sources": []})
        assert rc == 1 and "no citation URL" in out and "(none)" in out

    def test_no_findings_fail(self):
        rc, out = _run({"findings": "", "sources": [{"url": "https://a.example"}]})
        assert rc == 1 and "no findings text" in out

    def test_an_error_fails_and_is_redacted(self, monkeypatch):
        monkeypatch.setenv("PERPLEXITY_API_KEY", "pplx-SECRETSECRET")
        rc, out = _run(exc=RuntimeError("401 bad key pplx-SECRETSECRET"), route="direct")
        assert rc == 1 and "FAIL route=direct: RuntimeError" in out
        assert "pplx-SECRETSECRET" not in out and "[redacted]" in out

    def test_the_routes_call_the_production_functions(self):
        with patch("cqc_lem.utilities.ai.content_research._research_via_litellm",
                   return_value={"findings": "f", "sources": []}) as proxy:
            assert probe._call("proxy", "q", 3) == {"findings": "f", "sources": []}
        proxy.assert_called_once_with("q", 3)
        with patch("cqc_lem.utilities.ai.tools.search_with_perplexity",
                   return_value={"answer": " a ", "sources": [{"url": "u"}]}) as direct:
            assert probe._call("direct", "q", 3) == {"findings": "a", "sources": [{"url": "u"}]}
        direct.assert_called_once_with("q", max_sources=3)

    def test_main_parses_the_route(self):
        with patch.object(probe, "probe", return_value=0) as run:
            assert probe.main(["--route", "direct", "--subject", "s", "--max-sources", "2"]) == 0
        run.assert_called_once_with("direct", "s", 2)
