"""The comment anchor ladder (issue #2020).

LinkedIn does not serve one DOM. What a page renders varies by day, by viewer, by the connection
degree between viewer and author, and by the route taken to the page — so a single live-grounded
selector is a snapshot of one rendering, and every silent outage in this repo's history is the same
story: the one anchor stopped matching and the walk reported "nothing here" rather than "I no longer
know how to look".

Live evidence behind these tests, 2026-09-10, on a post whose own page said "4 comments":

    button[aria-label='Reply']  (exact)   0     <- what both shipped readers depended on
    button[aria-label^='Reply'] (prefix)  4     <- the label is now "Reply to Matthew B.'s comment"
    [data-testid*='commentList']          0
    main [data-testid]  (any testid)      0     <- the testid vocabulary was gone entirely
    main article                          4     <- the container was back to being an <article>

So the reader is a CHAIN of independent strategies, first-answer-wins, and only an exhausted chain
means "no comments" — which is then graded against the page's own count by the zero-walk tripwire.
"""

from unittest.mock import MagicMock

import pytest

from cqc_lem.utilities.linkedin.composer import (
    _COMMENT_ANCHOR_LADDER,
    _comment_containers,
)

pytestmark = pytest.mark.unit


def _driver(hits: dict, container_of=None):
    """A driver whose `find_elements` answers per selector, and whose walk resolves a container."""
    drv = MagicMock()

    def _find(_by, selector):
        return hits.get(selector, [])

    drv.find_elements.side_effect = _find
    drv.execute_script.side_effect = (
        container_of if container_of else lambda _js, anchor: f"container-of-{anchor}")
    return drv


def _selector(rung: str) -> str:
    return next(sel for name, sel in _COMMENT_ANCHOR_LADDER if name == rung)


class TestTheLadderIsOrdered:
    def test_the_current_rendering_answers_first(self):
        """2026-09-10: the entity-scoped reply control is the live rung."""
        drv = _driver({_selector("reply_prefix"): ["a", "b", "c", "d"]})
        containers, rung = _comment_containers(drv)
        assert rung == "reply_prefix"
        assert len(containers) == 4

    def test_a_rendering_that_still_uses_the_old_label_is_read_by_the_next_rung(self):
        """The exact label was the shape on 2026-07-24. Removing that rung would break whichever
        viewers LinkedIn still serves it to — the point of a ladder is that both are alive.
        """
        drv = _driver({_selector("reply_exact"): ["x", "y"]})
        containers, rung = _comment_containers(drv)
        assert rung == "reply_exact"
        assert len(containers) == 2

    def test_the_structural_rung_survives_a_vocabulary_change(self):
        """`main article` names no label and no testid, so it is the rung that answers when
        LinkedIn renames everything — which is exactly what happened on 2026-09-10.
        """
        drv = _driver({_selector("article"): ["art1", "art2"]})
        containers, rung = _comment_containers(drv)
        assert rung == "article"
        assert len(containers) == 2

    def test_an_earlier_rung_wins_even_when_a_later_one_would_also_answer(self):
        drv = _driver({_selector("reply_prefix"): ["a"], _selector("article"): ["art1", "art2"]})
        _containers, rung = _comment_containers(drv)
        assert rung == "reply_prefix"

    def test_the_testid_rung_is_still_carried(self):
        """It returned zero on 2026-09-10. Kept anyway: a rendering LinkedIn stopped serving is one
        it can serve again, and an unused rung costs one find_elements.
        """
        assert "commentlist_testid" in [name for name, _sel in _COMMENT_ANCHOR_LADDER]


class TestExhaustionIsTheOnlyEmptyAnswer:
    def test_no_rung_answering_returns_an_empty_rung_name(self):
        containers, rung = _comment_containers(_driver({}))
        assert containers == []
        assert rung == ""

    def test_a_rung_that_raises_does_not_stop_the_chain(self):
        """A selector the browser rejects must not be able to hide a comment a later rung can see."""
        drv = MagicMock()

        def _find(_by, selector):
            if selector == _selector("reply_prefix"):
                raise RuntimeError("invalid selector")
            return ["art1"] if selector == _selector("article") else []

        drv.find_elements.side_effect = _find
        drv.execute_script.side_effect = lambda _js, anchor: f"container-of-{anchor}"
        containers, rung = _comment_containers(drv)
        assert rung == "article" and len(containers) == 1

    def test_an_anchor_whose_walk_finds_nothing_is_dropped_not_guessed(self):
        """The old walk fell back to `arguments[0].parentElement`, which returns a fragment of the
        page and calls it a comment. A container we cannot resolve is not addressable, so it is
        dropped and the next rung gets its turn.
        """
        drv = _driver({_selector("reply_prefix"): ["a", "b"]}, container_of=lambda _js, _a: None)
        containers, rung = _comment_containers(drv)
        assert containers == [] and rung == ""

    def test_duplicate_anchors_resolving_to_one_comment_count_once(self):
        """A comment renders more than one control; the reading is comments, not buttons."""
        drv = _driver({_selector("reply_prefix"): ["a", "b", "c"]},
                      container_of=lambda _js, _a: "the-one-comment")
        containers, _rung = _comment_containers(drv)
        assert len(containers) == 1


class TestCarriedLadderCopy:
    """The probe is piped into the DEPLOYED image, so grounding a fix before it ships needs a
    carried copy — and a carried copy that drifts grounds a read nothing ships.
    """

    def test_the_carried_ladder_matches_the_shipped_one(self):
        import importlib.util
        import pathlib

        spec = importlib.util.spec_from_file_location(
            "llv_ladder", pathlib.Path("scripts/linkedin_live_validation.py"))
        llv = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(llv)
        assert llv._CARRIED_COMMENT_LADDER == _COMMENT_ANCHOR_LADDER

    def test_the_carried_author_reader_matches_the_shipped_one(self):
        import importlib.util
        import pathlib

        from cqc_lem.utilities.linkedin.composer import _COMMENT_HEADER_AUTHOR_JS

        spec = importlib.util.spec_from_file_location(
            "llv_ladder2", pathlib.Path("scripts/linkedin_live_validation.py"))
        llv = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(llv)
        assert llv._CARRIED_COMMENT_AUTHOR_JS == _COMMENT_HEADER_AUTHOR_JS
