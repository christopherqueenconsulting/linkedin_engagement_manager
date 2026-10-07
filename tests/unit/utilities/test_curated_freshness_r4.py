"""Showcase round 4, curated sources: the pick's diversity and the commentary's voice.

The gauntlet's four curated posts were all big-vendor announcements (two from Google), and one
read as if the author worked at OpenAI ("using our models"). The pick now prefers a fresh
publisher, an SMB-relevant item and government data or an independent writer over vendor PR; the
commentary speaks about the vendor in the third person, with one rewrite and a deterministic
last word.
"""

from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.app import run_curated_sources as rcs
from cqc_lem.utilities import curated_sources as cs
from cqc_lem.utilities.ai import curated_commentary as ccm

pytestmark = pytest.mark.unit

_CCM = "cqc_lem.utilities.ai.curated_commentary"
_RCS = "cqc_lem.app.run_curated_sources"

GOOGLE = {"id": 1, "publisher": "Google", "licence": "editorial", "platform": "rss",
          "title": "A new experimental gaming platform", "excerpt": "Developers can build games."}
OPENAI = {"id": 2, "publisher": "OpenAI", "licence": "editorial", "platform": "rss",
          "title": "Jump Trading uses our models", "excerpt": "A trading firm adopts GPT."}
MOLLICK = {"id": 3, "publisher": "One Useful Thing", "licence": "facts_only", "platform": "rss",
           "title": "What AI means for the small business owner",
           "excerpt": "Owners can draft invoices in minutes."}
BLS = {"id": 4, "publisher": "U.S. Bureau of Labor Statistics", "licence": "public_domain",
       "platform": "gov_data", "title": "Average hourly earnings rose",
       "excerpt": "Wages rose 0.3% in September."}
LI = {"id": 5, "publisher": "", "licence": "linkedin_native", "platform": "linkedin",
      "title": "A founder's note", "excerpt": "Some thoughts."}


class TestPickDiversity:
    @pytest.mark.parametrize("item,expected", [
        (BLS, cs.SOURCE_CLASS_GOV), (dict(MOLLICK), cs.SOURCE_CLASS_NEWSLETTER),
        (GOOGLE, cs.SOURCE_CLASS_VENDOR), (LI, cs.SOURCE_CLASS_OTHER),
        ({"platform": "rss", "licence": "Public Domain"}, cs.SOURCE_CLASS_GOV),
    ])
    def test_source_class(self, item, expected):
        assert cs.source_class(item) == expected

    def test_smb_relevance_reads_title_and_excerpt(self):
        assert cs.smb_relevant(MOLLICK) and cs.smb_relevant(BLS)
        assert not cs.smb_relevant(GOOGLE)

    def test_gov_data_and_independent_writers_outrank_vendor_pr(self):
        ranked = cs.rank_candidates([GOOGLE, OPENAI, MOLLICK, BLS])
        assert [c["id"] for c in ranked] == [4, 3, 1, 2]

    def test_a_recently_used_publisher_drops_behind_everything_fresh(self):
        ranked = cs.rank_candidates([MOLLICK, GOOGLE, BLS], recent_publishers=["one useful thing"])
        assert [c["id"] for c in ranked] == [4, 1, 3]

    def test_freshness_breaks_the_last_tie_and_junk_is_dropped(self):
        other_google = dict(GOOGLE, id=9)
        assert [c["id"] for c in cs.rank_candidates([GOOGLE, other_google, "junk", None])] == [1, 9]
        assert cs.rank_candidates(None) == []

    def test_the_slot_drafts_from_the_ranked_pool(self, monkeypatch):
        monkeypatch.setenv("CURATED_SOURCES_ENABLED", "true")
        with patch(f"{_RCS}.curated_enabled", return_value=True), \
             patch(f"{_RCS}.get_curated_neighbors", return_value=([], [])), \
             patch(f"{_RCS}.get_draftable_curated_sources",
                   return_value=[GOOGLE, OPENAI, MOLLICK, BLS]) as pool, \
             patch(f"{_RCS}.get_recent_curated_publishers", return_value=["Google"]) as recent, \
             patch(f"{_RCS}.draft_curated_post", return_value=None) as draft:
            assert rcs.curated_content_for_slot(1, 10, "value") is None
        pool.assert_called_once_with(1, limit=rcs.CANDIDATE_POOL)
        recent.assert_called_once_with(1, limit=cs.PUBLISHER_DIVERSITY_WINDOW)
        tried = [c.args[2]["id"] for c in draft.call_args_list]
        assert tried == [4, 3, 2]  # Google was used recently; only DRAFT_CANDIDATES are tried


class TestRecentPublishers:
    def test_reads_the_drafted_publishers_newest_first(self, fake_cursor):
        conn, cur = fake_cursor(fetch_all=[("Google",), (None,), ("OpenAI",)])
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_recent_curated_publishers
            assert get_recent_curated_publishers(7, limit=3) == ["Google", "OpenAI"]
        sql, params = cur.execute.call_args[0]
        assert "ORDER BY id DESC" in sql and "post_id IS NOT NULL" in sql and params == (7, 3)

    def test_an_error_is_no_history(self, fake_cursor):
        import mysql.connector
        conn, _ = fake_cursor(execute_error=mysql.connector.Error("boom"))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_recent_curated_publishers
            assert get_recent_curated_publishers(7) == []


SOURCE = {"id": 4, "platform": "rss", "url": "https://openai.com/a", "author": "",
          "publisher": "OpenAI", "title": "Jump Trading and GPT",
          "excerpt": "Jump Trading cut research time by 30% with GPT models last year.",
          "licence": "editorial"}
AS_VENDOR = ("OpenAI reports that Jump Trading is using our models to cut research time by 30%. "
             "For a small business the lesson is simpler: pick the one task your team repeats "
             "every morning, time it for a week, and see whether a model halves it before you "
             "buy anything bigger. That is the whole playbook for most owners I talk to.")
AS_AUTHOR = AS_VENDOR.replace("our models", "OpenAI's models")


@pytest.fixture
def passthrough_lint():
    with patch("cqc_lem.utilities.ai.ai_helper.lint_repaired",
               side_effect=lambda text, *a, **k: text):
        yield


class TestCommentaryVoice:
    @pytest.mark.parametrize("text,hits", [
        ("Jump Trading is using our models.", ["our models"]),
        ("Our latest API is faster, and we launched it today.", ["Our latest API", "we launched"]),
        ("Our clients and our team built this.", []),
        ("We built our own tool for a client.", []),
    ])
    def test_vendor_voice_hits(self, text, hits):
        assert ccm.vendor_voice_hits(text) == hits

    def test_third_person_rewrite_names_the_vendor(self):
        assert ccm.third_person_vendor("They use our models; we launched it.", "OpenAI") == \
            "They use OpenAI's models; OpenAI launched it."

    def test_the_prompt_names_the_vendor_in_the_third_person(self):
        system = ccm.commentary_messages(SOURCE, "link", "VOICE")[0]["content"]
        assert "'OpenAI's models'" in system and "never 'our models'" in system

    def test_a_vendor_voiced_draft_is_rewritten_once(self, passthrough_lint):
        with patch(f"{_CCM}._complete", side_effect=[AS_VENDOR, AS_AUTHOR]) as complete:
            out = ccm.generate_curated_commentary(1, SOURCE, "link", profile_synthesis="voice")
        assert complete.call_count == 2
        assert "SPOKE AS IF YOU WORKED AT OPENAI" in complete.call_args.args[0][1]["content"]
        assert "our models" not in out and "OpenAI's models" in out

    def test_a_rewrite_that_still_speaks_as_the_vendor_is_repointed(self, passthrough_lint):
        with patch(f"{_CCM}._complete", side_effect=[AS_VENDOR, AS_VENDOR]):
            out = ccm.generate_curated_commentary(1, SOURCE, "link", profile_synthesis="voice")
        assert "our models" not in out and "OpenAI's models" in out

    def test_an_author_voiced_draft_costs_no_rewrite(self, passthrough_lint):
        with patch(f"{_CCM}._complete", return_value=AS_AUTHOR) as complete:
            ccm.generate_curated_commentary(1, SOURCE, "link", profile=MagicMock(),
                                            profile_synthesis="voice")
        assert complete.call_count == 1
