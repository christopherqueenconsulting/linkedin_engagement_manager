"""The comment-only claim parser (#2626).

Wider vocabulary and per-sentence source matching, with `specific_tokens` (shared with newsletters
and group posts) left untouched.
"""
import pytest

from cqc_lem.utilities.ai import story_bank as sb

_POST = "We cut pick times 18% across two sites. Layout beats software here."


@pytest.mark.parametrize("text,token", [
    ("Roughly thirty percent of it.", "30"),
    ("About twenty-five people.", "25"),
    ("It fell by a third.", "1/3"),
    ("Half the queue vanished.", "1/2"),
    ("We saw 2x throughput.", "2x"),
    ("Two hundred tickets a week.", "200"),
    ("Dozens of clients asked.", "dozens"),
])
def test_comment_tokens_read_what_specific_tokens_misses(text, token):
    assert token in sb.comment_specific_tokens(text)


def test_shared_specific_tokens_is_unchanged():
    assert sb.specific_tokens("roughly thirty percent, a third, half, dozens") == set()
    assert sb.specific_tokens("we did twelve") == {"12"}


def test_invented_event_with_a_hedged_number_is_flagged():
    assert sb.unsourced_comment_specifics("We cut it by roughly thirty percent.", [_POST]) == ["30"]


@pytest.mark.parametrize("claim", ["We lost about a third of it.", "I saw half of that."])
def test_fractions_not_in_the_sources_are_flagged(claim):
    assert sb.unsourced_comment_specifics(claim, [_POST])


def test_number_in_the_post_but_a_different_sentence_does_not_launder_it():
    # 18% sits in the post, but not alongside "3 weeks".
    assert sb.unsourced_comment_specifics("We cut it 18% in 3 weeks.", [_POST]) == ["18", "3"]


def test_claim_matching_one_source_sentence_passes():
    assert sb.unsourced_comment_specifics("We saw 18% across two sites too.", [_POST]) == []


def test_story_bank_fact_passes():
    bank = ["Cut month-end close from 9 days to 4 by automating reconciliations."]
    assert sb.unsourced_comment_specifics("We took our close from nine days to four.", bank) == []


def test_counterpoint_without_a_claim_needs_no_source():
    assert sb.unsourced_comment_specifics("Layout only helps once volume is stable.", []) == []


def test_third_party_numbers_are_not_first_person_claims():
    assert sb.unsourced_comment_specifics("The market grew 12% last year.", []) == []
