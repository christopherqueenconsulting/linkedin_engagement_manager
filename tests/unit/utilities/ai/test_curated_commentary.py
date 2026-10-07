"""The curated writer and the chart-fact reader (docs/curated-sources.md).

Acceptance (#2260): a re-chart whose figure is missing from the source snapshot is refused before
render; the commentary states no number the source does not; every draft carries a credit line.
The LLM is mocked at `_complete`, the slop-lint loop at `ai_helper.lint_repaired`.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.utilities.ai import curated_commentary as ccm
from cqc_lem.utilities.ai.image_graphics import UngroundedFactError, render_graphic

pytestmark = pytest.mark.unit

_M = "cqc_lem.utilities.ai.curated_commentary"

SOURCE = {"id": 4, "platform": "rss", "url": "https://example.com/a", "author": "Jane Doe",
          "publisher": "Example Co", "title": "Agents in small finance teams",
          "excerpt": "Teams using agents cut invoice handling time by 30% last year. "
                     "Manual teams saw 12% growth in backlog.", "licence": "editorial"}

GOOD = ("Jane Doe found that finance teams using agents cut invoice handling time by 30%. "
        "That matches what I see with clients: the win is not the model, it is the boring "
        "intake step nobody owned before. If your backlog grew 12% last year, start with the "
        "one form your team retypes every morning and measure it for a week.")


@pytest.fixture
def passthrough_lint():
    with patch("cqc_lem.utilities.ai.ai_helper.lint_repaired",
               side_effect=lambda text, *a, **k: text):
        yield


class TestPrompt:
    def test_names_and_credits_and_carries_the_core(self):
        messages = ccm.commentary_messages(SOURCE, "reshare", "VOICE", {"use_emojis": False},
                                           "authority", "RETRY")
        system, user = messages[0]["content"], messages[1]["content"]
        assert "Jane Doe found" in system
        assert "NOT its author" in system
        assert "No politics" in system
        assert "VOICE" in system
        assert "LinkedIn will embed the original post" in system
        assert "<source_text>Teams using agents" in user
        assert user.rstrip().endswith("RETRY")


class TestGenerate:
    def test_a_grounded_draft_gets_its_credit_line(self, passthrough_lint):
        with patch(f"{_M}._complete", return_value=GOOD):
            out = ccm.generate_curated_commentary(1, SOURCE, "link", profile_synthesis="voice")
        assert out.startswith("Jane Doe found")
        assert out.rstrip().endswith('Source: Jane Doe, "Agents in small finance teams", '
                                     'Example Co.')

    def test_an_invented_number_gets_one_retry_then_is_refused(self, passthrough_lint):
        bad = GOOD.replace("30%", "45%")
        with patch(f"{_M}._complete", side_effect=[bad, bad]) as complete:
            assert ccm.generate_curated_commentary(1, SOURCE, "link") is None
        assert complete.call_count == 2
        assert "45%" in complete.call_args.args[0][1]["content"]   # the retry names the number

    def test_the_retry_can_fix_it(self, passthrough_lint):
        bad = GOOD.replace("30%", "45%")
        with patch(f"{_M}._complete", side_effect=[bad, GOOD]):
            assert ccm.generate_curated_commentary(1, SOURCE, "link") is not None

    def test_too_thin_is_refused(self, passthrough_lint):
        with patch(f"{_M}._complete", return_value="Great post by Jane Doe!"):
            assert ccm.generate_curated_commentary(1, SOURCE, "reshare") is None

    def test_placeholders_and_empty_are_refused(self, passthrough_lint):
        with patch(f"{_M}._complete", return_value=GOOD + " We saved [[NUMBER: hours]]."):
            assert ccm.generate_curated_commentary(1, SOURCE, "link") is None
        with patch(f"{_M}._complete", return_value=""):
            assert ccm.generate_curated_commentary(1, SOURCE, "link") is None

    def test_voice_from_profile(self, passthrough_lint):
        profile = MagicMock()
        profile.model_dump_json.return_value = '{"name": "Chris"}'
        with patch(f"{_M}._complete", return_value=GOOD) as complete:
            ccm.generate_curated_commentary(1, SOURCE, "link", profile=profile)
        assert '{"name": "Chris"}' in complete.call_args.args[0][0]["content"]

    def test_complete_passes_json_mode(self):
        response = MagicMock()
        response.choices = [MagicMock(message=MagicMock(content=" hi "))]
        with patch(f"{_M}.client") as client:
            client.chat.completions.create.return_value = response
            assert ccm._complete([], json_mode=True) == "hi"
        assert client.chat.completions.create.call_args.kwargs["response_format"] == {
            "type": "json_object"}


class TestChartFacts:
    RAW = {"thesis_stat": {"label": "invoice handling time", "value": "30", "unit": "%",
                           "source_sentence": "Teams using agents cut invoice handling time by "
                                              "30% last year."}}

    def test_a_figure_from_the_source_validates_with_our_credit(self):
        graphic = ccm.validated_chart(SOURCE, self.RAW)
        assert graphic["stat"]["display"] == "30%"
        assert graphic["source_line"] == "Source: Example Co, Agents in small finance teams"

    def test_a_figure_missing_from_the_source_is_refused_before_render(self):
        invented = json.loads(json.dumps(self.RAW))
        invented["thesis_stat"]["value"] = "45"
        assert ccm.validated_chart(SOURCE, invented) is None
        not_in_source = {"thesis_stat": {**self.RAW["thesis_stat"],
                                         "source_sentence": "Teams cut costs by 30% overall."}}
        assert ccm.validated_chart(SOURCE, not_in_source) is None

    def test_a_tampered_validated_figure_is_refused_by_render(self):
        graphic = ccm.validated_chart(SOURCE, self.RAW)
        graphic["stat"]["display"] = "45%"
        with pytest.raises(UngroundedFactError):
            render_graphic("stat_card", graphic, surface="post_image", hook="Agents at work")

    def test_extract_reads_json_from_the_model(self):
        with patch(f"{_M}._complete", return_value=json.dumps(self.RAW)) as complete:
            assert ccm.extract_chart_facts(SOURCE) == self.RAW
        assert complete.call_args.kwargs == {"model": "lem-simple", "json_mode": True}

    def test_extract_failures_are_empty(self):
        with patch(f"{_M}._complete", return_value="not json"):
            assert ccm.extract_chart_facts(SOURCE) == {}
        with patch(f"{_M}._complete", return_value="[1]"):
            assert ccm.extract_chart_facts(SOURCE) == {}
        assert ccm.extract_chart_facts({**SOURCE, "excerpt": " "}) == {}

    def test_gov_data_is_read_deterministically_and_validates(self):
        gov = {"platform": "gov_data", "publisher": "U.S. Bureau of Labor Statistics",
               "title": "Average hourly earnings",
               "excerpt": "Average hourly pay for private-sector employees was $36.12 in "
                          "August 2026."}
        with patch(f"{_M}._complete") as complete:
            raw = ccm.extract_chart_facts(gov)
        complete.assert_not_called()
        assert raw["thesis_stat"]["value"] == "36.12"
        assert raw["thesis_stat"]["unit"] == "$"
        graphic = ccm.validated_chart(gov, raw)
        assert graphic["stat"]["display"] == "$36.12"
        pct = {**gov, "excerpt": "The US unemployment rate was 4.3 percent in August 2026."}
        assert ccm.gov_graphic_facts(pct)["thesis_stat"]["unit"] == "%"
        assert ccm.gov_graphic_facts({**gov, "excerpt": "no reading"}) == {}
