"""Calibrated authenticity rubric for the A1 authenticity-scoring gate (issue #382) and the A3
Topic-DNA governor (issue #384).

Deliverable of the R2 360Brew rubric spike (issue #405). This module is the SINGLE source of truth
for HOW authentic-vs-generic-AI content is scored, WHERE the gate threshold sits, and WHICH labeled
drafts the A1/A3 acceptance tests run against. It carries NO LLM calls and NO I/O — A1 imports the
weights, threshold, and judge-criteria text into its `lem-medium` LLM-judge prompt, and the golden
set drives the acceptance tests that prove the gate flags generic drafts while NOT demoting good
AI-assisted-but-personal ones.

The calibration principle (from the 2026 LinkedIn Authenticity Update + Richard van der Blom's
Algorithm Insights): the enemy is *generic* content that could have been written by anyone, NOT
content that was written with AI help. Polish, correct grammar, and AI assistance are NEUTRAL. What
earns reach is lived first-person proof, checkable specificity, a consistent human voice, on-niche
topic authority, and a non-obvious point of view. The rubric therefore rewards those signals and
only penalizes their absence — so an AI-assisted post that carries real experience passes.

See docs/AUTHENTICITY_RUBRIC.md for the human-readable rubric, sources, and worked examples.
"""

# ---------------------------------------------------------------------------
# Scoring dimensions. Each dimension is scored 0-100 by the A1 LLM-judge; the composite is their
# weighted average. Weights sum to 1.0. The two heaviest dimensions (first_person_proof,
# specificity) are the ones that most cleanly separate generic AI from lived expertise, and are the
# same signals the deterministic A2 proof-slot detector already steers writers toward.
# ---------------------------------------------------------------------------

AUTHENTICITY_DIMENSIONS: dict = {
    "first_person_proof": {
        "weight": 0.28,
        "label": "First-person lived proof",
        "scores_high_when": (
            "Carries at least one concrete detail only THIS author could write — a real number, a "
            "moment in time, a named person/tool/company, or a specific outcome from their own work "
            "— owned in the first person (\"I\"/\"we\")."
        ),
        "scores_low_when": (
            "Only abstract, could-be-anyone claims; advice stated in the second/third person with no "
            "evidence the author has actually done it."
        ),
    },
    "specificity": {
        "weight": 0.22,
        "label": "Checkable specificity",
        "scores_high_when": (
            "Claims are grounded in checkable particulars — figures, dates, before/after deltas, "
            "concrete scenarios — instead of platitudes."
        ),
        "scores_low_when": (
            "Vague superlatives and buzzwords (\"game-changing\", \"leverage synergies\", \"in "
            "today's fast-paced world\") stand in for substance."
        ),
    },
    "voice_consistency": {
        "weight": 0.18,
        "label": "Consistent human voice",
        "scores_high_when": (
            "Reads in the author's established tone; natural rhythm; a real point of view. Matches "
            "the voice/expertise reference for this author."
        ),
        "scores_low_when": (
            "Flat corporate-neutral register; tone unlike the author's other writing; no discernible "
            "person behind the words."
        ),
    },
    "topic_authority": {
        "weight": 0.17,
        "label": "On-niche topic authority (A3)",
        "scores_high_when": (
            "The subject sits inside the author's focus_topics / profile headline & about — 360Brew "
            "'Topic DNA' consistency. This is the dimension the A3 governor consumes."
        ),
        "scores_low_when": (
            "Off-niche drift into a topic the author has no established authority in; a subject the "
            "profile gives no reason to trust them on."
        ),
    },
    "originality": {
        "weight": 0.15,
        "label": "Non-obvious insight",
        "scores_high_when": (
            "Offers a specific, non-obvious, or mildly contrarian point — teaches something the "
            "reader could not have guessed from the headline."
        ),
        "scores_low_when": (
            "Restates consensus everyone already agrees with; a listicle of obvious tips; ends on a "
            "hollow \"Thoughts?\" with nothing earned."
        ),
    },
}

# Common AI-slop tells the A1 judge should weigh DOWN (they mostly hurt `specificity`, `voice_
# consistency`, and `originality`). Listed so the gate flags the *pattern*, not merely the phrase —
# any single tell is weak evidence; a pileup of them alongside no first-person proof is the signal.
AI_SLOP_TELLS: tuple = (
    "opening with \"In today's fast-paced/ever-evolving world\" or \"In the age of AI\"",
    "\"It's not just X, it's Y\" / \"This isn't about X. It's about Y.\" cadence on repeat",
    "rule-of-three everything (\"faster, smarter, better\")",
    "emoji-bulleted listicle with one abstract tip per bullet and no lived example",
    "hollow engagement-bait CTA (\"Thoughts?\", \"Agree?\", \"Drop a 🔥 if you relate\")",
    "grand hollow abstractions (\"unlock/leverage/harness synergies\", \"game-changer\", "
    "\"paradigm shift\") standing in for a concrete claim",
    "definitional filler that teaches nothing (\"Leadership is about people.\")",
)

# ---------------------------------------------------------------------------
# Gate threshold. Composite score below this flips the draft APPROVED -> PENDING (mirrors the
# deterministic similarity gate the A1 issue points at). Calibrated on the golden set below:
# every `authentic` and `ai_assisted_personal` draft sits at/above it; every `generic` draft sits
# below it — so the gate flags generic content with NO false demotion of good AI-assisted posts.
# ---------------------------------------------------------------------------

AUTHENTICITY_GATE_THRESHOLD: int = 60

# A soft caution band: at/above the gate but not clearly strong. A1 need not demote these, but they
# are the drafts most worth a personal-proof nudge on regeneration.
AUTHENTICITY_CAUTION_CEILING: int = 72

# Text block A1 drops verbatim into the lem-medium judge prompt. Kept here (not in A1) so the
# scoring criteria and the golden set that validates them can never drift apart.
AUTHENTICITY_JUDGE_CRITERIA: str = (
    "You are scoring a LinkedIn draft for AUTHENTICITY under LinkedIn's 2026 Authenticity Update, "
    "which demotes generic AI content. Score generic-AI RISK, not writing quality: AI assistance, "
    "polish, and correct grammar are NEUTRAL and must never lower the score on their own. Reward "
    "lived first-person proof, checkable specificity, a consistent human voice, on-niche topic "
    "authority, and a non-obvious point of view. Penalize only their ABSENCE and the AI-slop tells. "
    "Return an integer 0-100 for each dimension and a short reason per dimension; higher = more "
    "authentic / lower generic-AI risk."
)


def _weighted_score_raw(dimension_scores: dict) -> float:
    """Unrounded composite score. Gate logic uses this so rounding can't flip a borderline draft
    across the threshold (e.g. 59.996 -> 60.0).
    """
    total = 0.0
    for name, spec in AUTHENTICITY_DIMENSIONS.items():
        raw = dimension_scores.get(name, 0)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = 0.0
        value = max(0.0, min(100.0, value))
        total += value * spec["weight"]
    return total


def weighted_score(dimension_scores: dict) -> float:
    """Composite 0-100 authenticity score from per-dimension 0-100 scores, rounded to 2 decimals
    for display. Gate logic uses the raw (unrounded) value via ``gate_draft``.

    Missing dimensions are treated as 0 (a judge that could not evidence a dimension gets no credit
    for it). Unknown keys are ignored so the caller can pass extra judge metadata safely.
    """
    return round(_weighted_score_raw(dimension_scores), 2)


def classify_score(score: float) -> str:
    """Map a composite score to a gate action: 'demote' (< threshold), 'caution' (threshold..
    ceiling), or 'pass' (>= ceiling).
    """
    if score < AUTHENTICITY_GATE_THRESHOLD:
        return "demote"
    if score < AUTHENTICITY_CAUTION_CEILING:
        return "caution"
    return "pass"


def gate_draft(dimension_scores: dict) -> bool:
    """True when the draft should be demoted (APPROVED -> PENDING) — composite below the gate
    threshold. The A1 gate calls this so the threshold has exactly one definition.
    """
    return _weighted_score_raw(dimension_scores) < AUTHENTICITY_GATE_THRESHOLD


# ---------------------------------------------------------------------------
# Golden set. Labeled reference drafts the A1/A3 acceptance tests run against. Labels:
#   - "generic"               : known generic AI slop — the gate MUST demote (expected 'demote').
#   - "authentic"             : human, specific, first-person — MUST pass (expected 'pass').
#   - "ai_assisted_personal"  : polished/AI-assisted BUT carries real lived proof & stays on-niche —
#                               the false-demotion guard; MUST NOT be demoted (expected 'pass').
# `expected_dimension_scores` are the calibration target the A1 judge is tuned against (illustrative,
# not asserted exactly — tests assert the resulting gate ACTION via classify_score). Kept realistic
# so weighted_score(expected) lands in the labeled band.
# ---------------------------------------------------------------------------

GOLDEN_SET: tuple = (
    {
        "id": "generic_thought_leadership",
        "label": "generic",
        "expected_action": "demote",
        "text": (
            "In today's fast-paced world, leadership is more important than ever. Great leaders "
            "inspire their teams to be faster, smarter, and better. It's not just about managing — "
            "it's about empowering people to unlock their full potential. Success isn't a "
            "destination, it's a journey. Agree? Thoughts?"
        ),
        "expected_dimension_scores": {
            "first_person_proof": 5,
            "specificity": 10,
            "voice_consistency": 30,
            "topic_authority": 45,
            "originality": 10,
        },
    },
    {
        "id": "generic_ai_tips_listicle",
        "label": "generic",
        "expected_action": "demote",
        "text": (
            "5 ways AI will change your business:\n"
            "🚀 Boost productivity\n"
            "🤖 Automate everything\n"
            "📈 Drive growth\n"
            "💡 Unlock innovation\n"
            "🔥 Stay ahead of the curve\n"
            "The future is here. Are you ready? Drop a 🔥 if you agree!"
        ),
        "expected_dimension_scores": {
            "first_person_proof": 0,
            "specificity": 15,
            "voice_consistency": 25,
            "topic_authority": 55,
            "originality": 8,
        },
    },
    {
        "id": "authentic_personal_story",
        "label": "authentic",
        "expected_action": "keep",
        "text": (
            "Three years ago I shipped a pricing change on a Friday afternoon and took down checkout "
            "for 40 minutes. We lost about $12k in orders and I spent the weekend writing the "
            "post-mortem. The fix wasn't technical — we added a rule that no pricing change goes out "
            "after noon on a Friday. Boring, unglamorous, and we haven't had a weekend outage since. "
            "The best reliability work rarely looks impressive."
        ),
        "expected_dimension_scores": {
            "first_person_proof": 92,
            "specificity": 88,
            "voice_consistency": 82,
            "topic_authority": 78,
            "originality": 75,
        },
    },
    {
        "id": "ai_assisted_personal_polished",
        "label": "ai_assisted_personal",
        "expected_action": "keep",
        "text": (
            "I used to think onboarding was a checklist. Then I watched our Q1 cohort: the 6 new "
            "hires who paired with someone on day one shipped their first PR in 4 days; the 5 who "
            "didn't took 11. Same docs, same tooling. The variable was a human who answered the "
            "dumb questions in real time. We now assign a day-one buddy to everyone — it's the "
            "cheapest retention lever we've found."
        ),
        "expected_dimension_scores": {
            "first_person_proof": 85,
            "specificity": 90,
            "voice_consistency": 76,
            "topic_authority": 80,
            "originality": 70,
        },
    },
    {
        "id": "borderline_thin_but_personal",
        "label": "authentic",
        "expected_action": "keep",
        "text": (
            "Spent last Tuesday rewriting our incident template. Cut it from 9 sections to 3: what "
            "broke, who it hit, what we changed. Adoption went from 'nobody fills it out' to every "
            "incident this week. Shorter forms get filled."
        ),
        "expected_dimension_scores": {
            "first_person_proof": 78,
            "specificity": 72,
            "voice_consistency": 68,
            "topic_authority": 66,
            "originality": 58,
        },
    },
    {
        "id": "generic_offniche_drift",
        "label": "generic",
        "expected_action": "demote",
        "text": (
            "Everyone talks about crypto, but let's talk about mindset. Winners never quit and "
            "quitters never win. Hustle harder, dream bigger, and manifest your best life. The only "
            "limit is you. Who's with me?"
        ),
        "expected_dimension_scores": {
            "first_person_proof": 8,
            "specificity": 6,
            "voice_consistency": 22,
            "topic_authority": 12,
            "originality": 6,
        },
    },
)

GOLDEN_LABELS: frozenset = frozenset({"generic", "authentic", "ai_assisted_personal"})


def golden_by_label(label: str) -> tuple:
    """All golden-set drafts carrying `label`."""
    return tuple(d for d in GOLDEN_SET if d["label"] == label)
