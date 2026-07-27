"""ONE content-framework core for every content type LEM writes. A BLUEPRINT is a named, testable
{format (archetype), hook_style, structure, cta_style} assignment; each content type gets its own
MENU (long-form newsletter archetypes, short-form post archetypes, comment angles) while the
selection, normalization, rotation/anti-repetition, and writer-directive machinery is SHARED — so
newsletters, posts, and comments can never drift apart in how variety is enforced.

This generalizes the newsletter-only blueprint system (V50): the newsletter menu is unchanged, and
posts/comments now rotate through their own menus with the same guarantees — no two consecutive
pieces share a format or hook style, and nothing repeats within the recent-history window."""

import json
import os
import random
import re
from typing import Optional

# How many most-recent historical formats/hook styles a new piece must also avoid (beyond the
# strict no-consecutive-repeat rule). 3 keeps rotation fresh while the menus stay pickable.
_AVOID_WINDOW = 3

# Performance-aware selection (issue #389 / B4 — close the feedback loop). Below this many total
# observed samples for a shape KIND (format or hook) we have too little signal to steer, so
# selection stays pure rotation (behavior unchanged). The exploration floor is the smallest weight
# an under-performing shape may be reduced to — never 0, so we keep sampling every shape and never
# collapse to a single winner. Both read at call time so ops/tests can tune without a restart.
PERF_MIN_SAMPLES_DEFAULT = 5
PERF_EXPLORATION_FLOOR_DEFAULT = 0.25

# ---------------------------------------------------------------------------
# NEWSLETTER menu (long-form editions) — unchanged from the V50 blueprint system.
# ---------------------------------------------------------------------------

NEWSLETTER_FORMATS: dict = {
    "deep_dive": {
        "label": "Deep Dive / How-To",
        "guidance": ("Teach ONE topic exhaustively enough that the reader can act without further "
                     "reading. Action-oriented section headers; every claim backed by an example, "
                     "number, or step — depth is the whole value."),
        "structure": [
            "Hook/lede in the assigned hook style",
            "Why this matters right now — the stakes and the cost of getting it wrong",
            "The core idea, explained through one concrete, worked example",
            "Going deeper: the nuances, second-order effects, or common failure modes most people miss",
            "How to apply it — specific, sequential steps the reader can run this week",
            "Key takeaways — 3-5 crisp lines the reader could screenshot",
            "CTA in the assigned CTA style",
        ],
    },
    "framework": {
        "label": "Framework / Playbook",
        "guidance": ("Package the author's expertise into a NAMED, memorable framework (give it a "
                     "sticky name). Walk each part with an example, and be honest about where the "
                     "framework breaks — limits build trust."),
        "structure": [
            "Hook/lede in the assigned hook style",
            "The recurring problem this framework solves and why ad-hoc approaches fail",
            "Name the framework and give a one-line overview of its parts",
            "Part-by-part walkthrough — each part gets its own subhead, explanation, and example",
            "When it breaks: the situations where the framework does NOT apply",
            "Key takeaways — the framework recapped in screenshot form",
            "CTA in the assigned CTA style",
        ],
    },
    "case_study": {
        "label": "Case Study / Story Breakdown",
        "guidance": ("A real problem→solution→outcome narrative with SPECIFIC numbers. The subject of "
                     "the story is the hero; the author is the guide. Concrete beats abstract advice "
                     "every time."),
        "structure": [
            "Hook/lede in the assigned hook style — drop the reader into the moment",
            "The setup: who was involved, what they were trying to do, what was at stake",
            "The turning point: what went wrong, or what forced a change",
            "What was actually done, step by step — decisions, trade-offs, dead ends included",
            "The outcome, with specific numbers or observable results",
            "The transferable lessons — what the reader can apply without living the story",
            "CTA in the assigned CTA style",
        ],
    },
    "contrarian": {
        "label": "Contrarian / Myth-Bust",
        "guidance": ("Challenge a piece of conventional wisdom the audience genuinely believes. State "
                     "the common view FAIRLY before dismantling it with evidence — a straw man kills "
                     "credibility; a steel man builds it."),
        "structure": [
            "Hook/lede in the assigned hook style — surface the belief being challenged",
            "The conventional wisdom, stated fairly and at its strongest",
            "Why it is wrong or incomplete — evidence, data, or lived counterexamples",
            "The better mental model to replace it",
            "What to do differently starting Monday morning",
            "Key takeaways — the old belief vs. the new model, side by side",
            "CTA in the assigned CTA style",
        ],
    },
    "listicle": {
        "label": "Tactical Listicle",
        "guidance": ("A numbered list of 5-7 parallel, equally-developed items — tips, mistakes, or "
                     "tools. Each item: a clear header line, why it matters, how to do it, and a "
                     "quick example. Highly scannable; the number in the title must match."),
        "structure": [
            "Hook/lede in the assigned hook style, ending with the promise of the list",
            "The numbered items (5-7), each with its own header line, the why, the how, and a quick example",
            "Which single item to start with if the reader only does one thing",
            "CTA in the assigned CTA style",
        ],
    },
    "teardown": {
        "label": "Teardown / Analysis",
        "guidance": ("Pick one real artifact — a launch, a post, a strategy, a trend — and dissect it "
                     "publicly: what works, what fails, and why. The reader learns judgment by "
                     "watching the author exercise it."),
        "structure": [
            "Hook/lede in the assigned hook style — introduce the artifact under the knife",
            "What we are analyzing and why it is instructive for this audience",
            "Section-by-section breakdown: what works, what does not, and the reasoning behind each call",
            "The transferable principles the reader can steal for their own work",
            "Key takeaways — the principles in screenshot form",
            "CTA in the assigned CTA style",
        ],
    },
    "roundup": {
        "label": "Curated Roundup + Commentary",
        "guidance": ("3-5 recent developments or ideas, curated for THIS audience. The value is the "
                     "author's TAKE on each — never just summarize; say what it means and what to do "
                     "about it. Close by naming the bigger pattern connecting them."),
        "structure": [
            "Hook/lede in the assigned hook style — the theme connecting the items",
            "3-5 developments or ideas, each with: what happened, why it matters to this reader, and the author's take",
            "The bigger pattern across all of them — the thesis the items add up to",
            "Key takeaways — what to watch and what to do",
            "CTA in the assigned CTA style",
        ],
    },
    "personal_lesson": {
        "label": "Personal Story → Lesson",
        "guidance": ("One true story from the author's own experience, told with sensory-specific "
                     "detail, that lands on a transferable principle. Vulnerability first, lesson "
                     "second — never a humble-brag."),
        "structure": [
            "Hook/lede in the assigned hook style — open inside the story",
            "The full story, with specific details (when, where, who, what it cost)",
            "The moment of realization — where the author's thinking changed",
            "The principle extracted from the story, stated plainly",
            "How the reader applies the principle without living the story",
            "CTA in the assigned CTA style",
        ],
    },
}

# Hook styles are content-type agnostic (a scroll-stopping opener is the same craft for an edition
# lede and a post's first line), so newsletters and posts SHARE this menu — one definition, no drift.
HOOK_STYLES: dict = {
    "question": {
        "label": "Pointed Question",
        "guidance": ("Open with ONE pointed question the reader has genuinely asked themselves — "
                     "specific to their situation, not a rhetorical 'Have you ever...?' cliché. Then "
                     "one line acknowledging why it is hard to answer."),
    },
    "surprising_stat": {
        "label": "Surprising Statistic",
        "guidance": ("Open with a specific, current NUMBER that upends an assumption, then one line "
                     "on what it means for the reader. Use a real figure from the source material — "
                     "never invent one."),
    },
    "bold_claim": {
        "label": "Bold / Contrarian Claim",
        "guidance": ("Open with a confident, defensible claim most of the audience currently "
                     "disagrees with — then spend the piece earning it. The unresolved tension is "
                     "what pulls the reader through."),
    },
    "micro_story": {
        "label": "Micro-Story",
        "guidance": ("Open mid-scene in a real moment — a time, a place, a tension — in 2-3 short "
                     "lines before zooming out to the point. Specific detail ('a Tuesday in March', "
                     "'the third slide') is what makes it land."),
    },
    "pattern_interrupt": {
        "label": "Pattern Interrupt",
        "guidance": ("Open with a line the reader does NOT expect from this author — a blunt "
                     "confession, an unusual image, a one-word sentence — that breaks the scroll "
                     "rhythm, then bridge to the topic within two lines."),
    },
    "direct_promise": {
        "label": "Direct Promise",
        "guidance": ("Open by naming EXACTLY what the reader will be able to do by the end of this "
                     "piece — a specific outcome with a specific scope, zero hype words."),
    },
    "common_frustration": {
        "label": "Voiced Frustration",
        "guidance": ("Open by voicing the exact frustration the reader feels but has not articulated "
                     "— in their words, not the author's jargon — so they feel seen before being "
                     "taught."),
    },
    "mistake_confession": {
        "label": "Mistake Confession",
        "guidance": ("Open by owning a specific mistake and what it cost — a real number, a real "
                     "consequence. Vulnerability first; the lesson comes after the reader trusts the "
                     "scar is real."),
    },
}

# The hook styles that can honestly LEAD WITH A REAL NUMBER. Save-targeted archetypes (issue #619 /
# G4) restrict their hook menu to these: a build receipt or a bookmarkable compendium earns the
# scroll-stop with the count/cost/duration in the first line, not with a rhetorical opener.
NUMBER_LED_HOOK_STYLES: tuple = ("surprising_stat", "direct_promise", "mistake_confession")

# How much `select_blueprint(prefer_save_targeted=True)` multiplies a save-targeted archetype's
# tie-break weight. 2x surfaces them roughly twice as often WITHOUT breaking rotation — recency is
# still the primary axis, so a save-targeted archetype can never repeat back-to-back.
SAVE_TARGETED_BOOST = 2.0

# ---------------------------------------------------------------------------
# 2026 CTA + hashtag policy (issue #393 / C4). Two things flipped under the 2026 algorithm:
# engagement-bait closes ("comment YES", "tag 3 people", "follow for more") are demoted instead of
# rewarded, and hashtags no longer expand reach — hashtag-free posts run roughly +5-10%. Both
# policies live HERE, once, so newsletters, posts, comments, and carousels can never drift apart
# on them.
# ---------------------------------------------------------------------------

# The cap for a user who explicitly opts back INTO hashtags: minimal, never decorative.
HASHTAG_MAX = 3

# Named in the writer directive so the model knows exactly what is banned. Detection of these in
# generated text is `linkedin_formatter.contains_engagement_bait` — the ONE bait detector.
ENGAGEMENT_BAIT_EXAMPLES: tuple = (
    "comment YES", "type INFO below", "tag 3 people", "like if you agree",
    "follow for more", "smash the like button", "repost if you agree",
)


def cta_policy_directive() -> str:
    """The invariant CTA rule injected wherever a CTA style is assigned: earn a real reply, never
    farm a reflex — and never a meeting ask (the artifact-CTA half lives in content_alignment, which
    also owns the detector and the deterministic repair)."""
    from cqc_lem.utilities.ai.content_alignment import ARTIFACT_CTA_POLICY
    bait = ", ".join(f"'{e}'" for e in ENGAGEMENT_BAIT_EXAMPLES[:4])
    return ("CTA RULE: the close must invite a genuine, specific response the reader has to think "
            f"about. NEVER engagement bait — no {bait}, and never ask for likes, follows, reposts, "
            "tags, or one-word replies. LinkedIn demotes bait in 2026; only a question worth "
            "answering earns the comment. " + ARTIFACT_CTA_POLICY)


def hashtag_directive(prefs: Optional[dict] = None) -> str:
    """The hashtag instruction for any prompt. The default is NONE — in 2026 hashtags no longer
    expand reach and hashtag-free posts measurably out-perform tagged ones — so tags appear only
    when the user explicitly turned `use_hashtags` on, and even then they stay minimal."""
    if prefs and prefs.get("use_hashtags"):
        return (f"Use at most {HASHTAG_MAX} highly relevant hashtags, together on the final line — "
                "hashtags no longer expand reach, so include only ones a human would actually follow.")
    return ("Do NOT include any hashtags — hashtag-free posts reach more people in 2026; remove any "
            "present in the draft.")


CTA_STYLES: dict = {
    "reply_question": {
        "label": "Reply-Driving Question",
        "guidance": ("Close with ONE open, specific question about the reader's own situation and "
                     "explicitly invite them to answer in the comments — specific questions get "
                     "replies; 'thoughts?' gets silence. Never a yes/no or one-word ask."),
    },
    "challenge": {
        "label": "This-Week Challenge",
        "guidance": ("Close by assigning one small, concrete action to take this week, and invite the "
                     "reader to report back in the comments with what actually happened — the result, "
                     "not a checkmark."),
    },
    "debate": {
        "label": "Invite Disagreement",
        "guidance": ("Close by inviting pushback — name the most likely objection and ask readers to "
                     "tell you where you're wrong, and why, in the comments. Reasoned disagreement is "
                     "the highest-value reply there is."),
    },
    "share_forward": {
        "label": "Pass It To One Person",
        "guidance": ("Close by naming the ONE specific kind of person this edition would genuinely "
                     "help right now and suggesting the reader forward it to them, and invite new "
                     "readers to subscribe. Never a blanket share-or-repost request."),
    },
    "teaser_next": {
        "label": "Next-Edition Teaser",
        "guidance": ("Close by teasing what the NEXT edition covers — a specific open loop worth "
                     "subscribing for — and invite readers to subscribe so they don't miss it."),
    },
    "poll_prompt": {
        "label": "Either/Or With Reasons",
        "guidance": ("Close with a crisp either/or question (option A vs option B) and ask which the "
                     "reader would pick AND why in one line — the reason is the point; a bare vote "
                     "is bait and teaches the feed nothing."),
    },
}

# ---------------------------------------------------------------------------
# POST menu (short-form feed posts, ~1300-2000 chars). Archetypes encode the highest-performing
# 2026 post patterns: contrarian takes, specific numbers, confession/vulnerability, save-worthy
# frameworks/checklists, story→lesson, and conversation-starting questions.
# ---------------------------------------------------------------------------

POST_FORMATS: dict = {
    "personal_lesson": {
        "label": "Story → Lesson",
        "guidance": ("One TRUE moment from the author's experience, told in 3-6 short lines with a "
                     "specific detail, landing on ONE transferable lesson. Vulnerability first, "
                     "lesson second — never a humble-brag."),
        "structure": [
            "Hook in the assigned hook style — open inside the moment",
            "The story beats: what happened, what it cost or changed, told in short lines",
            "The lesson, stated plainly in one or two lines",
            "How the reader applies it without living the story",
            "CTA in the assigned CTA style",
        ],
    },
    "contrarian_take": {
        "label": "Contrarian Take",
        "guidance": ("Challenge one piece of conventional wisdom the audience genuinely believes. "
                     "State the common view fairly in a line, then dismantle it with evidence or "
                     "lived experience — a steel man, never a straw man."),
        "structure": [
            "Hook in the assigned hook style — surface the belief being challenged",
            "The conventional wisdom, stated fairly in one line",
            "Why it is wrong or incomplete — one piece of evidence or a lived counterexample",
            "The better mental model, in plain words",
            "CTA in the assigned CTA style",
        ],
    },
    "tactical_list": {
        "label": "Tactical List",
        "guidance": ("A scannable list of 3-5 parallel, concrete items — tips, mistakes, or steps — "
                     "each one line of what plus one line of why/how. Save-worthy is the goal: "
                     "readers should want to come back to it."),
        "structure": [
            "Hook in the assigned hook style, ending with the promise of the list",
            "The 3-5 items, each on its own short lines: the what, then the why or how",
            "Which single item to start with if the reader only does one thing",
            "CTA in the assigned CTA style",
        ],
    },
    "how_to": {
        "label": "Mini How-To",
        "guidance": ("Teach ONE narrow, immediately-usable process in numbered steps the reader can "
                     "run today. Specific inputs and outputs; no theory without an action attached."),
        "structure": [
            "Hook in the assigned hook style — name the outcome this unlocks",
            "The 3-5 numbered steps, each concrete enough to act on",
            "The one mistake people make when trying this",
            "CTA in the assigned CTA style",
        ],
    },
    "case_snapshot": {
        "label": "Case Snapshot",
        "guidance": ("One real example — a client, a company, a project — compressed into "
                     "problem → what was done → outcome WITH a specific number or observable "
                     "result. Concrete beats abstract every time."),
        "structure": [
            "Hook in the assigned hook style — lead with the outcome or the stakes",
            "The setup: who, what they were trying to do, what was in the way",
            "What was actually done — the one or two decisive moves",
            "The result, with a specific number or observable change",
            "The transferable takeaway for the reader",
            "CTA in the assigned CTA style",
        ],
    },
    "industry_observation": {
        "label": "Trend Observation + Take",
        "guidance": ("Name ONE current, real development in the author's industry and give a clear "
                     "TAKE on it — what it means, who it affects, what to do. The author's judgment "
                     "is the value; never just report the news."),
        "structure": [
            "Hook in the assigned hook style — the development, framed for this audience",
            "What is actually happening, in two or three plain lines (real facts only)",
            "The author's take: what it means and who should care",
            "What to do about it this quarter",
            "CTA in the assigned CTA style",
        ],
    },
    "myth_vs_reality": {
        "label": "Myth vs. Reality",
        "guidance": ("Pick ONE widely-repeated myth in the author's field and put the reality next "
                     "to it, backed by data or lived experience. The side-by-side contrast is the "
                     "engine — keep both sides sharp."),
        "structure": [
            "Hook in the assigned hook style — name the myth",
            "The myth: what everyone repeats and why it sounds right",
            "The reality: what the data or experience actually shows",
            "What to do differently once you accept the reality",
            "CTA in the assigned CTA style",
        ],
    },
    # --- Save-targeted archetypes (issue #619 / G4) ------------------------------------------
    # Both trade entirely on REAL specifics, so both are fact-anchored: the writer may only state a
    # number that comes from a verified fact, otherwise it must ship as a marked placeholder the
    # author fills in at approval (see `fact_anchor_directive` / `fact_grounding_report`).
    "build_receipt": {
        "label": "Build Receipt",
        "guidance": ("A receipt for something the author ACTUALLY built: what it is, the exact "
                     "stack, what broke on the way, and what it can be pointed at. The credibility "
                     "comes from the unglamorous specifics — the dead end, the tool that did not "
                     "work, the real number — so never smooth them out and never invent them."),
        "structure": [
            "Hook in the assigned hook style — lead with the real number (what was built, at what "
            "scale, cost, or time)",
            "What it actually is, in two plain lines: the problem it solves and who it is for",
            "The stack: the specific tools, models, or services used, and what each one does here",
            "What broke: the failure, dead end, or wrong assumption that cost real time — and what "
            "was done instead",
            "Concrete use cases: 2-4 specific jobs a reader could point this at tomorrow",
            "CTA in the assigned CTA style, closed as a SOFT ARTIFACT offer (the build log, the "
            "template, the checklist) — never a pitch, never 'book a call'",
        ],
        "save_targeted": True,
        "fact_anchored": True,
        "hook_styles": NUMBER_LED_HOOK_STYLES,
    },
    "resource_compendium": {
        "label": "Bookmarkable Compendium",
        "guidance": ("A reference the reader expects to need again: a counted, curated set of "
                     "resources, tools, prompts, or checks, each with the exact job it does. Written "
                     "to be SAVED, not scrolled past — every entry must be real and something the "
                     "author has actually used."),
        "structure": [
            "Hook in the assigned hook style — lead with the real count of what is inside and who "
            "it is for",
            "Why this was compiled: the specific problem that made the list worth assembling",
            "The numbered entries — each one line naming the resource plus one line on the exact "
            "job it does and when to reach for it",
            "The single entry to start with if the reader only uses one",
            "What was deliberately LEFT OFF the list, and why",
            "CTA in the assigned CTA style, closed as a SOFT ARTIFACT offer — never a pitch",
        ],
        "save_targeted": True,
        "fact_anchored": True,
        "hook_styles": NUMBER_LED_HOOK_STYLES,
    },
    "question_starter": {
        "label": "Conversation Starter",
        "guidance": ("A short setup that frames ONE genuinely open question the author actually "
                     "wants answers to. The post's job is the comment thread — give just enough "
                     "context and personal stance to make answering easy and worth it."),
        "structure": [
            "Hook in the assigned hook style — the tension behind the question",
            "Two or three lines of context: why this is genuinely undecided or hard",
            "The author's current lean, stated briefly so replies have something to push against",
            "CTA in the assigned CTA style",
        ],
    },
}

# Post CTAs: the conversation-driving newsletter CTA styles apply verbatim to posts (shared object
# references — ONE definition), minus the subscribe-focused ones, plus a save-focused close that
# matches 2026's save-signal weighting.
POST_CTA_STYLES: dict = {
    "reply_question": CTA_STYLES["reply_question"],
    "challenge": CTA_STYLES["challenge"],
    "debate": CTA_STYLES["debate"],
    "poll_prompt": CTA_STYLES["poll_prompt"],
    "save_worthy": {
        "label": "Worth Saving",
        "guidance": ("Close with ONE short, soft 'worth saving for later' style line (saves are the "
                     "strongest 2026 engagement signal) plus a specific question so commenters still "
                     "have a real way in. Never beg for the save."),
    },
}

# ---------------------------------------------------------------------------
# DAY-TYPE CALENDAR (issue #621 / G6). Cadence, not volume, is the 2026 lever: van der Blom's
# 1.3M-post sample puts daily posting at roughly -26% average reach per post, and the creators who
# compound run a FIXED weekly calendar where every slot has a known job (Alić: Mon mindset / Tue
# guide / Wed story / Thu polarizing; van der Blom rotates formats weekly). A fixed slot builds
# Topic Authority and reader anticipation — it replaces the old uniform buyer-journey round-robin,
# which gave every day a differently-shaped post and no recognisable rhythm.
#
# One entry per weekday (Mon=0 … Sun=6):
#   formats  — the POST_FORMATS archetype family this day draws from. The shared rotation engine
#              still varies the shape WITHIN the family, so two guide days don't read alike.
#   stage    — the buyer-journey stage that day's job implies (replaces the round-robin).
#   priority — which slots a user's posts_per_week actually fills, best posting windows first:
#              Tue/Thu/Wed are the strongest 2026 windows, Mon/Fri next, weekends last. At the
#              default 3/week that yields Tue build-receipt · Wed story · Thu spiky POV.
# ---------------------------------------------------------------------------

POST_DAY_TYPES: dict = {
    0: {"key": "guide", "label": "Practical guide", "priority": 4, "stage": "consideration",
        "formats": ["how_to", "tactical_list"],
        "job": "Open the week with ONE narrow thing the reader can run today."},
    1: {"key": "build_receipt", "label": "Build receipt", "priority": 1, "stage": "decision",
        "formats": ["case_snapshot", "how_to"],
        "job": ("Show real work with real numbers — what was built, what broke, what it produced. "
                "The receipt IS the credibility.")},
    2: {"key": "story", "label": "Story → lesson", "priority": 3, "stage": "awareness",
        "formats": ["personal_lesson"],
        "job": "One true moment from the author's own experience, landing on one transferable lesson."},
    3: {"key": "spiky_pov", "label": "Spiky POV", "priority": 2, "stage": "awareness",
        "formats": ["contrarian_take", "myth_vs_reality"],
        "job": ("Take a defensible position against a named common practice — spiky, never "
                "contrarian for its own sake.")},
    4: {"key": "observation", "label": "Observation + take", "priority": 5, "stage": "awareness",
        "formats": ["industry_observation", "question_starter"],
        "job": "Read one real development in the author's field and say what it actually means."},
    5: {"key": "conversation", "label": "Conversation starter", "priority": 7, "stage": "awareness",
        "formats": ["question_starter"],
        "job": "A genuinely open question the author wants answers to — the thread is the point."},
    6: {"key": "reflection", "label": "Reflection", "priority": 6, "stage": "consideration",
        "formats": ["personal_lesson", "tactical_list"],
        "job": "A quieter week-in-review note: what changed, what it taught, what to carry forward."},
}

# The calendar covers a full week, so this is also the hard ceiling on slots per week. The
# PREFERENCE bounds (what a user may save) live with the other prefs bounds in utilities/db.py.
POST_DAY_TYPE_MAX_SLOTS = len(POST_DAY_TYPES)


def weekly_post_slots(posts_per_week: int, allowed_days: Optional[list] = None) -> list:
    """The weekdays (Mon=0 … Sun=6) a user posting `posts_per_week` times publishes on, in weekday
    order. Slots fill by the calendar's `priority`, so raising the cadence ADDS days without moving
    the ones already in use — a user who goes 3→4/week keeps Tue/Wed/Thu and gains Monday.

    `allowed_days` (issue #581) narrows the candidates to the user's configured `posting_days`:
    cadence still says HOW MANY, the allow-list says WHICH ARE ELIGIBLE. A cadence higher than the
    allow-list can hold is capped by it — you cannot publish on a day you have switched off — and an
    empty/unusable allow-list means "not configured", which falls back to the full calendar rather
    than scheduling nothing.
    """
    try:
        count = int(posts_per_week)
    except (TypeError, ValueError):
        count = 0
    candidates = sorted(POST_DAY_TYPES, key=lambda wd: POST_DAY_TYPES[wd]["priority"])
    if allowed_days is not None:
        narrowed = [wd for wd in candidates if wd in set(allowed_days)]
        if narrowed:
            candidates = narrowed
    count = max(1, min(POST_DAY_TYPE_MAX_SLOTS, len(candidates), count))
    return sorted(candidates[:count])


def day_type_for_weekday(weekday: int) -> Optional[dict]:
    """The day type owning a weekday, or None for anything outside Mon..Sun."""
    try:
        return POST_DAY_TYPES.get(int(weekday))
    except (TypeError, ValueError):
        return None


def day_type_formats(weekday: int) -> list:
    """The archetype family for a weekday — the `preferred_formats` a blueprint is picked from."""
    day_type = day_type_for_weekday(weekday)
    return list(day_type["formats"]) if day_type else []


def day_type_stage(weekday: int, default: str = "awareness") -> str:
    """The buyer-journey stage this weekday's job implies."""
    day_type = day_type_for_weekday(weekday)
    return day_type["stage"] if day_type else default


# ---------------------------------------------------------------------------
# COMMENT menu (feed-comment angles). Every angle stays GROUNDED IN THE TARGET POST and ends
# inviting the author to reply — the archetype only varies HOW the comment adds its value, so a
# user's comments across a day don't all read from the same template.
# ---------------------------------------------------------------------------

COMMENT_FORMATS: dict = {
    "expander": {
        "label": "Expander",
        "guidance": ("Add ONE insight, nuance, or second-order effect the post itself did not "
                     "cover, staying strictly on the post's topic — extend the author's thinking, "
                     "never redirect it."),
        "structure": [
            "React to one SPECIFIC point from the post (paraphrase or lightly quote it)",
            "Add the one nuance or implication the post did not cover",
            "End with an open question to the author about that nuance",
        ],
    },
    "storyteller": {
        "label": "Storyteller",
        "guidance": ("Connect the post's point to ONE short, true first-hand moment (1-2 lines, a "
                     "specific detail) that proves or complicates it — the experience serves the "
                     "post's topic, it never becomes about the commenter."),
        "structure": [
            "Name the specific point in the post the experience relates to",
            "The 1-2 line first-hand moment with one concrete detail",
            "End with a question inviting the author's read on it",
        ],
    },
    "questioner": {
        "label": "Thoughtful Questioner",
        "guidance": ("Ask ONE genuinely curious, specific question that extends the post — the kind "
                     "the author will actually want to answer. Show you understood the post first; "
                     "never ask something the post already answered."),
        "structure": [
            "One line showing you understood the post's specific argument",
            "The one specific, open question that extends it",
        ],
    },
    "respectful_contrarian": {
        "label": "Respectful Contrarian",
        "guidance": ("Agree with part of the post, then offer ONE evidence-or-experience-backed "
                     "counterpoint — challenge the idea, never the person, and leave genuine room "
                     "to be wrong."),
        "structure": [
            "Name the specific part of the post you agree with",
            "The counterpoint, backed by one piece of evidence or experience",
            "End with a question inviting the author to push back",
        ],
    },
    "connector": {
        "label": "Pattern Connector",
        "guidance": ("Tie the post's specific point to ONE broader pattern, trend, or adjacent "
                     "development the author didn't name — the connection must genuinely illuminate "
                     "the post's topic, not show off breadth."),
        "structure": [
            "React to the post's specific point",
            "The one broader pattern or adjacent development it connects to",
            "End with a question about whether the author sees the same connection",
        ],
    },
    "practical_add": {
        "label": "Practical Add",
        "guidance": ("Contribute ONE concrete, immediately-usable tactic or step that helps readers "
                     "act on the post's idea — specific enough to try today, still entirely about "
                     "the post's topic."),
        "structure": [
            "Name the post's point the tactic serves",
            "The one concrete tactic or step, stated plainly",
            "End with a question asking the author how they handle that part",
        ],
    },
    # Completes the rotation's coverage of the four contract value-adds below (experience →
    # storyteller, disagreement → respectful_contrarian, question → questioner, DATA → here).
    "evidence_add": {
        "label": "Evidence Add",
        "guidance": ("Bring ONE concrete number, benchmark, or measured result that supports or "
                     "complicates the post's specific claim — a real figure the commenter actually "
                     "knows, never an invented statistic. If no real number is available, use a "
                     "measured observation from their own work instead."),
        "structure": [
            "Name the specific claim in the post the number speaks to",
            "The number or measured result, with where it came from",
            "End with a question asking whether the author sees the same numbers",
        ],
    },
}

# ---------------------------------------------------------------------------
# The per-content-type menu registry — the ONE place that maps a content type to its menus.
# ---------------------------------------------------------------------------

MENUS: dict = {
    "newsletter": {"formats": NEWSLETTER_FORMATS, "hooks": HOOK_STYLES, "ctas": CTA_STYLES},
    "post": {"formats": POST_FORMATS, "hooks": HOOK_STYLES, "ctas": POST_CTA_STYLES},
    "comment": {"formats": COMMENT_FORMATS, "hooks": {}, "ctas": {}},
}

_DIRECTIVE_NOUN = {"newsletter": "EDITION", "post": "POST", "comment": "COMMENT"}


def _menu(content_type: str) -> dict:
    try:
        return MENUS[content_type]
    except KeyError:
        raise ValueError(f"Unknown content type: {content_type!r} (known: {', '.join(MENUS)})")


def _normalize(value, options: dict) -> Optional[str]:
    if not value or not options:
        return None
    key = str(value).strip().lower().replace("-", "_").replace(" ", "_").replace("/", "_")
    if key in options:
        return key
    # Tolerate labels or partial names ("Case Study", "deep dive how-to") from the LLM.
    for k, meta in options.items():
        if k in key or key in k or key == meta["label"].strip().lower().replace("-", "_").replace(" ", "_").replace("/", "_"):
            return k
    plain = str(value).strip().lower()
    for k, meta in options.items():
        if plain and (plain in meta["label"].lower() or meta["label"].lower() in plain):
            return k
    return None


def normalize_key(content_type: str, kind: str, value) -> Optional[str]:
    """Normalize a model-supplied format/hook/CTA value to a known key for that content type.
    `kind` is 'format', 'hook' or 'cta'."""
    menu = _menu(content_type)
    options = {"format": menu["formats"], "hook": menu["hooks"], "cta": menu["ctas"]}[kind]
    return _normalize(value, options)


def structure_for(content_type: str, format_key) -> list:
    meta = _menu(content_type)["formats"].get(normalize_key(content_type, "format", format_key) or "")
    return list(meta["structure"]) if meta else []


def format_meta(content_type: str, format_key) -> dict:
    """The menu entry for a format key, or {} when it is unknown/absent."""
    key = normalize_key(content_type, "format", format_key)
    return _menu(content_type)["formats"].get(key or "") or {}


def allowed_hooks(content_type: str, format_key=None) -> dict:
    """The hook menu a given format may draw from. Most formats use the whole menu; a save-targeted
    archetype (issue #619) narrows it to the hook styles that can honestly lead with a real number.
    An unknown format or an empty intersection falls back to the full menu rather than to nothing."""
    hooks = _menu(content_type)["hooks"]
    allowed = format_meta(content_type, format_key).get("hook_styles")
    if not allowed or not hooks:
        return hooks
    return {k: hooks[k] for k in allowed if k in hooks} or hooks


def is_save_targeted(content_type: str, format_key) -> bool:
    """True for the archetypes written to be SAVED rather than scrolled past — the save signal is
    weighted ~5x a like in 2026 and feeds evergreen redistribution, so scheduling can prefer them."""
    return bool(format_meta(content_type, format_key).get("save_targeted"))


def save_targeted_formats(content_type: str) -> list:
    """Every save-targeted format key for a content type, in menu order."""
    return [k for k, m in _menu(content_type)["formats"].items() if m.get("save_targeted")]


def fact_anchored_formats(content_type: str) -> list:
    """Every fact-anchored format key for a content type, in menu order — the archetypes a caller
    that has NO verified facts may need to keep out of the rotation (a carousel bakes its text into
    rendered slide images, so a placeholder there can never be edited away)."""
    return [k for k, m in _menu(content_type)["formats"].items() if m.get("fact_anchored")]


def requires_fact_anchor(content_type: str, format_key) -> bool:
    """True for archetypes whose whole value IS the specifics (build receipt, compendium). Their
    drafts run through the no-fabrication guard: a number that no verified fact backs must be a
    marked placeholder, never an invention (#416 policy)."""
    return bool(format_meta(content_type, format_key).get("fact_anchored"))


def options_text(content_type: str) -> str:
    """The menu of formats/hooks/CTAs given to a PLANNER so it assigns real, known values."""
    menu = _menu(content_type)
    lines = ["Available FORMATS (use the key on the left):"]
    lines += [f"- {k}: {m['label']} — {m['guidance']}" for k, m in menu["formats"].items()]
    if menu["hooks"]:
        lines.append("\nAvailable HOOK STYLES (use the key on the left):")
        lines += [f"- {k}: {m['label']} — {m['guidance']}" for k, m in menu["hooks"].items()]
    if menu["ctas"]:
        lines.append("\nAvailable CTA STYLES (use the key on the left):")
        lines += [f"- {k}: {m['label']} — {m['guidance']}" for k, m in menu["ctas"].items()]
    return "\n".join(lines)


def _perf_min_samples() -> int:
    raw = (os.environ.get("PERF_MIN_SAMPLES") or "").strip()
    try:
        return max(1, int(raw)) if raw else PERF_MIN_SAMPLES_DEFAULT
    except ValueError:
        return PERF_MIN_SAMPLES_DEFAULT


def _perf_exploration_floor() -> float:
    raw = (os.environ.get("PERF_EXPLORATION_FLOOR") or "").strip()
    try:
        val = float(raw) if raw else PERF_EXPLORATION_FLOOR_DEFAULT
    except ValueError:
        return PERF_EXPLORATION_FLOOR_DEFAULT
    return min(1.0, max(0.0, val))


def _shape_engagement(agg: dict, rate_mode: bool = False) -> float:
    """Per-post engagement metric for one shape's aggregated stats. Weights comments (2×) and
    reposts (3×) above reactions since they signal stronger reach on the 2026 feed. Impression-
    normalized (engagement rate) ONLY when the caller passes `rate_mode` — i.e. EVERY sampled
    shape of this kind has full impression coverage (see `_all_impression_covered`); otherwise
    average engagement per post. The rate/per-post decision is made once by the caller so all
    shapes of a kind stay on the same scale (see `performance_weights`)."""
    samples = agg.get("samples", 0) or 0
    if samples <= 0:
        return 0.0
    engagement = (agg.get("reactions", 0) or 0) + 2 * (agg.get("comments", 0) or 0) \
        + 3 * (agg.get("reposts", 0) or 0)
    impressions = agg.get("impressions", 0) or 0
    if rate_mode and impressions > 0:
        return engagement / impressions
    return engagement / samples


def _all_impression_covered(perf: dict) -> bool:
    """True only when every sampled shape has full impression coverage — the gate for using
    engagement-RATE scoring uniformly instead of average-per-post (mixing the two scales across
    shapes would make the comparison meaningless)."""
    sampled = [a for a in perf.values() if (a.get("samples", 0) or 0) > 0]
    return bool(sampled) and all(
        a.get("impression_samples", 0) == a.get("samples", 0) and (a.get("impressions", 0) or 0) > 0
        for a in sampled)


def performance_weights(perf: Optional[dict], keys, min_samples: int = None,
                        floor: float = None) -> dict:
    """Turn per-shape outcome aggregates into multiplicative SELECTION weights for `_pick` (issue
    #389 / B4). `perf` maps shape key → aggregate (from `db.get_shape_performance`); `keys` is the
    menu's full key set for this kind. Returns {} (⇒ pure rotation, behavior unchanged) until at
    least `min_samples` total observations exist. Otherwise: shapes performing below the sampled
    mean are down-weighted proportionally, floored at the exploration floor so they're never
    dropped; at-or-above-mean and unsampled shapes stay at weight 1.0 (unsampled = keep exploring).
    """
    if not perf:
        return {}
    min_samples = _perf_min_samples() if min_samples is None else min_samples
    floor = _perf_exploration_floor() if floor is None else floor
    total = sum((a.get("samples", 0) or 0) for a in perf.values())
    if total < min_samples:
        return {}
    rate_mode = _all_impression_covered(perf)
    metrics = {}
    for key, agg in perf.items():
        if (agg.get("samples", 0) or 0) <= 0:
            continue
        # rate_mode gates the scale for ALL shapes at once: engagement-rate when every sampled
        # shape has full impression coverage, else avg-per-post everywhere. Same scale either way.
        metrics[key] = _shape_engagement(agg, rate_mode)
    if not metrics:
        return {}
    mean = sum(metrics.values()) / len(metrics)
    weights = {}
    for key in keys:
        if key not in metrics:
            weights[key] = 1.0  # unsampled shape — neutral, so exploration keeps sampling it
        elif mean <= 0:
            weights[key] = 1.0
        else:
            weights[key] = max(floor, min(1.0, metrics[key] / mean))
    return weights


def _pick(options: dict, recency: list, forbidden: set, weights: dict = None) -> str:
    """Pick the least-recently-used option outside `forbidden`, choosing among ties by `weights`
    (a shape→multiplier map; missing/None ⇒ uniform, preserving prior behavior). Recency stays the
    PRIMARY axis so variety is never sacrificed; performance only biases the random tie-break so
    under-performing shapes surface less often while every shape can still be picked. `recency` is
    most-recent-first; options never used rank best. Relaxes `forbidden` rather than ever failing."""
    keys = list(options.keys())
    candidates = [k for k in keys if k not in forbidden]
    if not candidates:  # everything forbidden → only hard rule left is "not the immediate previous"
        head = recency[0] if recency else None
        candidates = [k for k in keys if k != head] or keys

    def rank(k: str) -> int:
        return recency.index(k) if k in recency else len(recency) + 1

    best = max(rank(k) for k in candidates)
    tied = [k for k in candidates if rank(k) == best]
    if weights:
        w = [max(0.0, weights.get(k, 1.0)) for k in tied]
        if sum(w) > 0:
            return random.choices(tied, weights=w, k=1)[0]
    return random.choice(tied)


def enforce_variety(content_type: str, blueprints: list, recent_formats: list = None,
                    recent_hook_styles: list = None) -> list:
    """Guarantee IN CODE what a planner prompt merely requests: normalize each blueprint's
    format/hook/cta to known keys for the content type and reassign any that repeat — no two
    consecutive pieces (nor a piece and the most recent history) share a format or hook style, no
    format/hook repeats within the batch or the recent-history window, and every blueprint carries
    its format's structure."""
    menu = _menu(content_type)
    formats, hooks, ctas = menu["formats"], menu["hooks"], menu["ctas"]
    rf = [f for f in (_normalize(x, formats) for x in (recent_formats or [])) if f]
    rh = [h for h in (_normalize(x, hooks) for x in (recent_hook_styles or [])) if h]
    f_recency, h_recency = list(rf), list(rh)
    window_f, window_h = set(rf[:_AVOID_WINDOW]), set(rh[:_AVOID_WINDOW])
    prev_f = rf[0] if rf else None
    prev_h = rh[0] if rh else None
    batch_f: set = set()
    batch_h: set = set()
    prev_c = None
    out = []
    for bp in blueprints or []:
        if not isinstance(bp, dict):
            continue
        fmt = _normalize(bp.get("format"), formats)
        if fmt is None or fmt == prev_f or fmt in batch_f or fmt in window_f:
            fmt = _pick(formats, f_recency, {prev_f} | batch_f | window_f)
        item = dict(bp)
        item.update({"format": fmt, "structure": list(formats[fmt]["structure"])})
        # A save-targeted archetype narrows the hook menu to the number-led styles, so the hook is
        # validated against THIS format's menu, not the content type's whole one.
        hooks = allowed_hooks(content_type, fmt)
        if hooks:
            hook = _normalize(bp.get("hook_style"), hooks)
            if hook is None or hook == prev_h or hook in batch_h or hook in window_h:
                hook = _pick(hooks, h_recency, {prev_h} | batch_h | window_h)
            item["hook_style"] = hook
            prev_h = hook
            batch_h.add(hook)
            h_recency.insert(0, hook)
        if ctas:
            cta = _normalize(bp.get("cta_style"), ctas)
            if cta is None or cta == prev_c:
                cta = _pick(ctas, [prev_c] if prev_c else [], {prev_c} if prev_c else set())
            item["cta_style"] = cta
            prev_c = cta
        out.append(item)
        prev_f = fmt
        batch_f.add(fmt)
        f_recency.insert(0, fmt)
    return out


def select_blueprint(content_type: str, subject: str = None, angle: str = None,
                     recent_formats: list = None, recent_hook_styles: list = None,
                     guidance: str = None, performance: Optional[dict] = None,
                     prefer_save_targeted: bool = False,
                     exclude_formats: Optional[list] = None,
                     preferred_formats: list = None) -> dict:
    """A fresh blueprint for ONE piece of any content type, chosen in code (no LLM call): rotate
    away from the recent formats/hooks — including the piece's own previous shape — so consecutive
    pieces change form, not just words. Free-text `guidance` may name a format (e.g. 'make it a
    case study'); honor it when it does. `performance` (from `db.get_shape_performance`, keys
    'format'/'hook') closes the feedback loop — under-performing shapes are surfaced less often
    while rotation and exploration are preserved (issue #389 / B4). `prefer_save_targeted` biases
    the tie-break toward the save-optimized archetypes (issue #619 / G4) — a bias, never a
    forced pick, so rotation and variety still hold. `exclude_formats` takes archetypes OFF the menu
    entirely (guidance included) for callers that cannot honor their contract — see
    `fact_anchored_formats`; excluding every format falls back to the full menu rather than to
    nothing. `preferred_formats` narrows the format menu to one family (the day-type calendar's
    archetypes, issue #621) — rotation, recency and performance still apply WITHIN it, and an
    unrecognised family is ignored rather than emptying the menu."""
    menu = _menu(content_type)
    formats, hooks, ctas = menu["formats"], menu["hooks"], menu["ctas"]
    if exclude_formats:
        dropped = {_normalize(x, formats) for x in exclude_formats}
        formats = {k: v for k, v in formats.items() if k not in dropped} or formats
    # An explicit `guidance` format is a direct instruction, so it is resolved against the (post
    # exclusion) FULL menu and wins over the day-type family below.
    hinted = _normalize(guidance, formats) if guidance else None
    if not hinted and guidance:
        low = guidance.lower()
        for k, meta in formats.items():
            if k.replace("_", " ") in low or meta["label"].lower() in low:
                hinted = k
                break
    pickable = formats
    if preferred_formats and not hinted:
        family = dict.fromkeys(f for f in (_normalize(x, formats) for x in preferred_formats) if f)
        pickable = {k: formats[k] for k in family} or formats
    rf = [f for f in (_normalize(x, formats) for x in (recent_formats or [])) if f]
    rh = [h for h in (_normalize(x, hooks) for x in (recent_hook_styles or [])) if h]
    fmt_weights = performance_weights((performance or {}).get("format"), pickable.keys())
    hook_weights = performance_weights((performance or {}).get("hook"), hooks.keys())
    if prefer_save_targeted:
        fmt_weights = dict(fmt_weights or {k: 1.0 for k in pickable})
        for key in save_targeted_formats(content_type):
            fmt_weights[key] = fmt_weights.get(key, 1.0) * SAVE_TARGETED_BOOST
    fmt = hinted or _pick(pickable, rf, {rf[0] if rf else None} | set(rf[:_AVOID_WINDOW]), fmt_weights)
    out = {"subject": subject, "angle": angle or "", "format": fmt,
           "structure": list(formats[fmt]["structure"])}
    # Number-led archetypes draw their hook from the narrowed menu (see `allowed_hooks`).
    hooks = allowed_hooks(content_type, fmt)
    out["hook_style"] = _pick(hooks, rh, {rh[0] if rh else None} | set(rh[:_AVOID_WINDOW]),
                              hook_weights) if hooks else None
    out["cta_style"] = _pick(ctas, [], set()) if ctas else None
    return out


def blueprint_directive(content_type: str, blueprint: dict,
                        fact_anchors: Optional[list] = None) -> str:
    """The WRITER-side injection: the assigned format's guidance + ordered structure skeleton, the
    hook style, and the CTA style. Returns '' when the blueprint carries no known format.

    For a save-targeted, fact-anchored archetype (issue #619 / G4) it also injects the number-led
    hook constraints and the no-fabrication rules — `fact_anchors` is the list of VERIFIED,
    human-sourced facts the writer may state as its own (the story bank, issue #620 / G5, is the
    source). Callers that already thread a blueprint through several prompt builders can instead hang
    the anchors on the blueprint itself as `fact_anchors`; the explicit argument wins. With no anchors
    either way the writer may not state a specific at all: every number ships as a marked placeholder
    the author fills in at approval."""
    if not isinstance(blueprint, dict):
        return ""
    menu = _menu(content_type)
    fmt = _normalize(blueprint.get("format"), menu["formats"])
    if not fmt:
        return ""
    noun = _DIRECTIVE_NOUN.get(content_type, "PIECE")
    f_meta = menu["formats"][fmt]
    lines = [
        f"\nTHIS {noun}'S ASSIGNED BLUEPRINT — it OVERRIDES the default structure above. Follow it exactly:",
        f"FORMAT: {f_meta['label']}. {f_meta['guidance']}",
    ]
    if content_type == "comment":
        lines.append("APPROACH — cover these beats IN THIS ORDER (as natural flowing sentences, "
                     "never labeled sections):")
    else:
        lines.append("STRUCTURE — write these sections IN THIS ORDER (plain-text subheads, no markdown):")
    lines += [f"{i}. {s}" for i, s in enumerate(f_meta["structure"], 1)]
    hook = _normalize(blueprint.get("hook_style"), menu["hooks"])
    if hook:
        h_meta = menu["hooks"][hook]
        lines.append(f"OPENING HOOK STYLE: {h_meta['label']}. {h_meta['guidance']}")
    if f_meta.get("hook_styles"):
        lines.append(hook_constraint_directive())
    cta = _normalize(blueprint.get("cta_style"), menu["ctas"])
    if cta:
        c_meta = menu["ctas"][cta]
        lines.append(f"CTA STYLE: {c_meta['label']}. {c_meta['guidance']}")
        lines.append(cta_policy_directive())
    if content_type in _PROOF_SLOT_TYPES:
        lines.append(PERSONAL_PROOF_SLOT)
    if f_meta.get("fact_anchored"):
        lines.append(fact_anchor_directive(
            blueprint.get("fact_anchors") if fact_anchors is None else fact_anchors))
    return "\n".join(lines) + "\n"


def compact_blueprint(blueprint: dict) -> Optional[dict]:
    """The persistable core of a blueprint (structure skeletons live in code, not the DB)."""
    if not isinstance(blueprint, dict):
        return None
    return {k: blueprint.get(k) for k in ("subject", "angle", "format", "hook_style", "cta_style")}


# ---------------------------------------------------------------------------
# Personal-expertise / first-person proof slot (A2). 2026 authenticity guidance: AI content that
# reads as generic gets demoted, so every long/short-form piece must carry at least ONE concrete,
# FIRST-PERSON lived detail — a real number, a moment in time, a named example, or an outcome from
# the author's own experience, not an abstract claim. blueprint_directive() injects the mandatory
# "proof slot" below into the writer prompt (the specific material is the author's voice/expertise
# reference already in the prompt — see content_alignment.personal_proof_directive), and the
# deterministic (no-LLM) detector further down is the gate run_content_plan uses to reject and
# regenerate a draft whose proof slot came back empty or generic (the signal the A1 anti-slop gate
# also consumes).
# ---------------------------------------------------------------------------

# Content types that must fill the proof slot. Comments stay grounded in the TARGET post (that is
# their proof), so forcing the author's own numbers into every comment would drift them off-topic —
# comments are exempt.
_PROOF_SLOT_TYPES = frozenset({"post", "newsletter"})

# The mandatory A2 proof-slot line appended to the writer directive. Kept distinct from self-promo:
# it asks for the author's lived EXPERTISE (credibility), never a plug for anything they are building.
PERSONAL_PROOF_SLOT = (
    "MANDATORY PERSONAL-PROOF SLOT: somewhere in the body, land at least ONE specific, first-person "
    "lived detail only THIS author could write — a real number, a moment in time, a named example, "
    "or a concrete outcome from their own experience or work (draw it from the author voice & "
    "expertise reference above). Own it in the first person (\"I\"/\"we\"). Generic, could-be-anyone "
    "claims do NOT satisfy this — this is what separates real expertise from generic AI content. This "
    "is proof of experience, not self-promotion: never turn it into a plug for anything the author "
    "is building."
)

# First-person ownership markers — the "this happened to ME" half of a lived detail.
_FIRST_PERSON_RE = re.compile(
    r"\b(?:i|i'm|i've|i'd|i'll|me|my|myself|mine|we|we're|we've|we'd|we'll|our|ours|us)\b",
    re.IGNORECASE)

_PROOF_MONTHS = ("january|february|march|april|may|june|july|august|september|october|november|"
                 "december")
_PROOF_WEEKDAYS = "monday|tuesday|wednesday|thursday|friday|saturday|sunday"

# Concrete-specificity signals — the "and here is the CHECKABLE particular" half. A number (digit or
# spelled), a relative-time anchor, or a named day/month grounds the claim in a real moment instead
# of an abstraction. The gate flags genuinely generic drafts (no number AND no time anchor tied to a
# first-person clause), not merely digit-free ones — it only regenerates, never hard-blocks, and the
# proof slot steers writers toward exactly these anchors. Erring toward "counts as proof" keeps the
# extra generation cost down: a false pass ships a slightly-less-proven post, a false fail burns a
# regeneration.
_SPECIFICITY_RE = re.compile(
    r"\d"
    r"|\b(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"dozens?|hundreds?|thousands?|millions?|billions?)\b"
    r"|\b(?:years?|months?|weeks?|weekends?|days?|hours?|decades?)\s+ago\b"
    r"|\b(?:last|past|next|first|second|third)\s+"
    r"(?:year|month|week|weekend|quarter|decade|time|day|night|morning)\b"
    r"|\bback\s+in\b"
    # A marked fact placeholder (issue #619) IS a concrete particular — one the author fills in at
    # approval rather than one the model invented. Without this the proof-slot regeneration would
    # fight the no-fabrication guard, which forbids stating the very number the proof slot wants.
    r"|\[\[[^\]]+\]\]"
    rf"|\b(?:{_PROOF_MONTHS})\b"
    rf"|\b(?:{_PROOF_WEEKDAYS})\b",
    re.IGNORECASE)

_PROOF_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")


def first_person_proof_sentences(text: Optional[str]) -> list:
    """Sentences carrying BOTH a first-person marker AND a concrete-specificity signal — i.e. a
    lived, first-person detail rather than an abstract claim. Deterministic, no LLM. The same-sentence
    tie is what keeps an unrelated stat elsewhere in a generic post from counting as the author's own
    proof."""
    out = []
    for sentence in _PROOF_SENTENCE_SPLIT.split(text or ""):
        s = sentence.strip()
        if s and _FIRST_PERSON_RE.search(s) and _SPECIFICITY_RE.search(s):
            out.append(s)
    return out


def has_first_person_proof(text: Optional[str]) -> bool:
    """True when the draft fills the A2 proof slot — at least one concrete first-person lived detail.
    Empty/None or purely-generic content → False, so the caller reject/regenerates it."""
    return bool(first_person_proof_sentences(text))


# ---------------------------------------------------------------------------
# Post-history uniqueness engine — the newsletter's subject-dedup (V49) + avoid_openers steering
# applied to POSTS, plus a cheap deterministic similarity gate. Lives here (not in a parallel
# module) because "never repeat yourself" is the same variety contract the blueprint rotation
# above enforces for SHAPE — this enforces it for CONTENT.
# ---------------------------------------------------------------------------

# Default ceiling for the token-set overlap between a new post and any recent post. ~0.55 flags
# rewordings of the same post (which score 0.6+) while leaving two distinct posts on the same broad
# theme (which score ~0.2-0.4) alone. Override per-deploy with POST_SIMILARITY_MAX.
POST_SIMILARITY_MAX_DEFAULT = 0.55


def post_similarity_max(prefs: dict = None) -> float:
    """The similarity ceiling. The user's own setting (engagement_preferences.post_similarity_max_pct,
    a whole percent — issue #421) wins when set; otherwise read at call time (same live-env pattern as
    the research toggles in content_research) so ops/tests can tune POST_SIMILARITY_MAX without a
    restart."""
    override = (prefs or {}).get("post_similarity_max_pct")
    if override is not None:
        try:
            return min(100, max(10, int(override))) / 100.0
        except (TypeError, ValueError):
            pass
    raw = (os.environ.get("POST_SIMILARITY_MAX") or "").strip()
    try:
        return float(raw) if raw else POST_SIMILARITY_MAX_DEFAULT
    except ValueError:
        return POST_SIMILARITY_MAX_DEFAULT


# Minimal English stopword set (incl. the common 2-letter words) — enough to keep function-word
# overlap from inflating similarity between two unrelated posts, while keeping meaningful short
# tokens like 'ai' or 'ml' that focus-topic matching depends on.
_STOPWORDS = frozenset((
    "a an and are as at be been but by can could did do does for from had has have he her here his "
    "how i if in into is it its just me more most my no nor not of on or our out over she so some "
    "than that the their them then there these they this those to too up us was we were what when "
    "where which who why will with would you your").split())


def content_tokens(text: str) -> set:
    """Normalized meaningful-token set: lowercased word tokens minus stopwords and 1-char noise."""
    words = re.findall(r"[a-z0-9']+", (text or "").lower())
    return {w for w in words if len(w) > 1 and w not in _STOPWORDS}


def text_similarity(a: str, b: str) -> float:
    """Deterministic normalized token-set OVERLAP COEFFICIENT: |A∩B| / min(|A|, |B|). Chosen over
    SequenceMatcher because near-duplicate posts are usually REWORDINGS (same vocabulary, different
    order/length) — a set measure catches those where a sequence measure misses them, and it stays
    cheap at generation volume. The min-denominator keeps a short near-copy of a longer post from
    hiding behind the length difference. Range 0.0-1.0."""
    ta, tb = content_tokens(a), content_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def cosine_similarity(a: Optional[list], b: Optional[list]) -> float:
    """Cosine of two equal-length embedding vectors, clamped to 0.0-1.0. Mismatched lengths,
    empties, and zero-norm vectors are 0.0 (not an error) so a half-written embedding can never
    claim a match. Negative cosines are clamped to 0.0 — "less than unrelated" is still unrelated.

    Lives here, next to `text_similarity`, because this module is the ONE similarity toolbox every
    dedup gate shares: the feedback loop's duplicate detection imports it from here (issue #498),
    and so does the comment-side gate below (issue #617)."""
    if not a or not b or len(a) != len(b):
        return 0.0
    try:
        dot = sum(float(x) * float(y) for x, y in zip(a, b))
        norm_a = sum(float(x) * float(x) for x in a) ** 0.5
        norm_b = sum(float(y) * float(y) for y in b) ** 0.5
    except (TypeError, ValueError):
        return 0.0
    if norm_a <= 0 or norm_b <= 0:
        return 0.0
    return max(0.0, min(1.0, dot / (norm_a * norm_b)))


def as_vector(raw: object) -> Optional[list]:
    """An embedding as a list of floats. A stored one (`feedback.embedding` is a JSON column) comes
    back as a str on some connector versions and a list on others; junk becomes None."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    if not isinstance(raw, list) or not raw:
        return None
    try:
        return [float(x) for x in raw]
    except (TypeError, ValueError):
        return None


def find_most_similar(text: str, candidates: list) -> tuple:
    """(best_score, best_candidate_text) of `text` against each candidate; (0.0, None) when there is
    nothing to compare."""
    best_score, best_match = 0.0, None
    for cand in candidates or []:
        score = text_similarity(text, cand)
        if score > best_score:
            best_score, best_match = score, cand
    return best_score, best_match


def opening_line(text: str, max_chars: int = 200) -> str:
    """First non-empty line of a piece of content — the post-side twin of the newsletter's
    opening_line history (V50)."""
    return next((ln.strip() for ln in (text or "").splitlines() if ln.strip()), "")[:max_chars]


def infer_post_subject(text: str, max_keywords: int = 5) -> str:
    """Cheap deterministic subject fingerprint: the most frequent meaningful tokens (ties broken by
    first appearance), joined as a keyword phrase. No LLM call — posts have no stored subject column
    (unlike newsletter_editions.subject), so the subject is derived from content on demand."""
    words = re.findall(r"[a-z0-9']+", (text or "").lower())
    counts: dict = {}
    first_pos: dict = {}
    for i, w in enumerate(words):
        if len(w) <= 1 or w in _STOPWORDS:
            continue
        counts[w] = counts.get(w, 0) + 1
        first_pos.setdefault(w, i)
    top = sorted(counts, key=lambda w: (-counts[w], first_pos[w]))[:max_keywords]
    return ", ".join(sorted(top, key=lambda w: first_pos[w]))


def history_avoidance_directive(recent_texts: list, offending_text: str = None,
                                max_items: int = 10) -> str:
    """The AVOID block injected into post prompts, mirroring how newsletter regeneration passes
    avoid_subjects/avoid_openers: recent posts' opening lines + subject fingerprints the new post
    must not reuse. `offending_text` (a similarity-gate retry) adds the specific too-similar post so
    the retry steers hard away from it. Returns '' when there is nothing to avoid."""
    openers, subjects = [], []
    seen_o, seen_s = set(), set()
    for t in recent_texts or []:
        o = opening_line(t)
        s = infer_post_subject(t)
        if o and o.lower() not in seen_o and len(openers) < max_items:
            openers.append(o)
            seen_o.add(o.lower())
        if s and s not in seen_s and len(subjects) < max_items:
            subjects.append(s)
            seen_s.add(s)
    if not openers and not subjects and not offending_text:
        return ""
    lines = ["\n\nUNIQUENESS RULES — do NOT repeat the author's recent posts (repetition kills "
             "reach and reads as automation). Bring a genuinely fresh subject, opening, and take:"]
    if openers:
        lines.append("- Recent posts OPENED with these exact lines. Your opening line must NOT "
                     "reuse or resemble ANY of them — different wording, different rhetorical "
                     "device, different rhythm:\n  * " + "\n  * ".join(openers))
    if subjects:
        lines.append("- Recent posts covered these subjects (keyword fingerprints). Cover "
                     "materially different ground — a different problem, angle, or takeaway, never "
                     "a rephrasing:\n  * " + "\n  * ".join(subjects))
    if offending_text:
        lines.append("- IMPORTANT: a previous draft was TOO SIMILAR to this earlier post. Choose a "
                     "clearly different subject, opening, and structure than:\n  \""
                     + str(offending_text).strip()[:400] + "\"")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Dwell-time optimization (issue #391 / C2). Dwell — how long a reader HOLDS on a post — is the
# dominant 2026 ranking signal (a 60s+ hold correlates with ~15.6% engagement vs ~1.2% for a sub-3s
# scroll-past), and it is not something a like/comment count can proxy. Two halves, both here so the
# writer side and the measuring side can never drift: `dwell_directive()` steers generation (fold
# placement, read-time target, scannable structure) and `dwell_report()` measures the finished draft
# deterministically (no LLM) into a 0-100 dwell PROXY score stored next to the authenticity score.
# ---------------------------------------------------------------------------

# Characters LinkedIn shows in the feed before the '...more' fold — the entire budget the hook has
# to earn the expand click that starts the dwell clock.
LINKEDIN_FOLD_CHARS = 210
# LinkedIn's hard post limit; used as the no-truncation ceiling when reflowing (never as a target).
LINKEDIN_MAX_CHARS = 3000
# Average considered-reading speed for feed prose. 200 wpm is the conservative middle of the
# 180-240 range, so read-time estimates lean long rather than over-promising a 60s hold.
DWELL_READ_WPM = 200
# Word band that lands a post in the 55-120 second read window: enough substance for a 60s+ hold,
# short enough that a scroller still finishes. Matches the 1300-2000 char craft rule (~6 chars/word).
DWELL_TARGET_WORDS_MIN = 180
DWELL_TARGET_WORDS_MAX = 400
# A paragraph longer than this reads as a wall and gets scrolled past; `shape_for_dwell` reflows it.
DWELL_PARAGRAPH_MAX_CHARS = 300
# Fewer paragraphs than this means no white space to descend through — the other wall-of-text shape.
DWELL_MIN_PARAGRAPHS = 4
# Below this proxy score a draft is logged as dwell-weak. Advisory only (like the authenticity
# score's demote-not-block posture, minus the demotion) — it never blocks or regenerates a post.
DWELL_SCORE_MIN_DEFAULT = 60

# Re-readable structure — a numbered step, a bullet, or a checkbox line. This is the shape readers
# scroll back up through and SAVE, which is both a dwell multiplier and the strongest 2026 signal.
_LIST_LINE_RE = re.compile(r"^\s*(?:\d+[.)]\s+|[-*•‣▪✓✔☑]\s+|step\s+\d+\b)", re.IGNORECASE | re.MULTILINE)

_PARAGRAPH_SPLIT_RE = re.compile(r"\n\s*\n")


def dwell_score_min() -> int:
    """The dwell-proxy warning threshold, read at call time (the POST_SIMILARITY_MAX live-env
    pattern) so ops can tune DWELL_SCORE_MIN without a restart."""
    raw = (os.environ.get("DWELL_SCORE_MIN") or "").strip()
    try:
        return max(0, min(100, int(raw))) if raw else DWELL_SCORE_MIN_DEFAULT
    except ValueError:
        return DWELL_SCORE_MIN_DEFAULT


def read_seconds(text: Optional[str]) -> float:
    """Estimated read time in seconds at DWELL_READ_WPM — the closest cheap proxy for dwell there
    is without impression data."""
    words = len(re.findall(r"\S+", text or ""))
    return round(words / DWELL_READ_WPM * 60, 1)


def dwell_metrics(text: Optional[str]) -> dict:
    """The raw deterministic dwell measurements of a finished draft: length/read-time, where the
    hook lands relative to the '...more' fold, and how scannable the body is. No LLM, no I/O."""
    body = (text or "").strip()
    words = len(re.findall(r"\S+", body))
    paragraphs = [p.strip() for p in _PARAGRAPH_SPLIT_RE.split(body) if p.strip()]
    hook = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
    longest = max((len(p) for p in paragraphs), default=0)
    return {
        "chars": len(body),
        "words": words,
        "read_seconds": read_seconds(body),
        "hook_chars": len(hook),
        # The hook must be READABLE IN FULL before the fold — a first line that runs past it gets
        # truncated mid-thought, which reads as noise instead of an open loop.
        "hook_within_fold": bool(hook) and len(hook) <= LINKEDIN_FOLD_CHARS,
        "fold_text": body[:LINKEDIN_FOLD_CHARS],
        "paragraphs": len(paragraphs),
        "longest_paragraph_chars": longest,
        "wall_of_text": longest > DWELL_PARAGRAPH_MAX_CHARS,
        "has_list": bool(_LIST_LINE_RE.search(body)),
    }


def dwell_report(text: Optional[str]) -> dict:
    """The dwell PROXY: `dwell_metrics` folded into a 0-100 score with its per-component breakdown
    and the human-readable issues behind any lost points. Weights follow what actually buys hold
    time: the fold decides whether the post is read AT ALL (30), read-time is the dwell itself (30),
    scannability decides whether the reader survives the body (25), and re-readable structure is
    what earns the save/scroll-back (15)."""
    m = dwell_metrics(text)
    issues = []

    if not m["chars"]:
        return {"score": 0, "components": {"hook_fold": 0.0, "read_time": 0.0,
                                           "scannability": 0.0, "structure": 0.0},
                "metrics": m, "issues": ["empty content"]}

    if m["hook_within_fold"]:
        hook_points = 30.0
    elif m["hook_chars"] <= LINKEDIN_FOLD_CHARS * 1.5:
        hook_points = 15.0  # spills just past the fold — truncated, but the gist still lands
        issues.append(f"hook runs {m['hook_chars']} chars, past the {LINKEDIN_FOLD_CHARS}-char fold")
    else:
        hook_points = 0.0
        issues.append(f"no hook before the fold (first line is {m['hook_chars']} chars)")

    words = m["words"]
    if DWELL_TARGET_WORDS_MIN <= words <= DWELL_TARGET_WORDS_MAX:
        read_points = 30.0
    else:
        ratio = (words / DWELL_TARGET_WORDS_MIN) if words < DWELL_TARGET_WORDS_MIN \
            else (DWELL_TARGET_WORDS_MAX / words)
        read_points = round(30.0 * max(0.0, min(1.0, ratio)), 2)
        issues.append(f"read time {m['read_seconds']}s ({words} words) outside the "
                      f"{DWELL_TARGET_WORDS_MIN}-{DWELL_TARGET_WORDS_MAX} word dwell target")

    para_points = round(15.0 * min(1.0, m["paragraphs"] / DWELL_MIN_PARAGRAPHS), 2)
    if m["paragraphs"] < DWELL_MIN_PARAGRAPHS:
        issues.append(f"only {m['paragraphs']} paragraph(s) — too little white space to descend through")
    wall_points = 0.0 if m["wall_of_text"] else 10.0
    if m["wall_of_text"]:
        issues.append(f"wall-of-text paragraph ({m['longest_paragraph_chars']} chars > "
                      f"{DWELL_PARAGRAPH_MAX_CHARS})")
    scan_points = round(para_points + wall_points, 2)

    if m["has_list"]:
        structure_points = 15.0
    elif m["paragraphs"] >= DWELL_MIN_PARAGRAPHS:
        structure_points = 8.0  # rhythmic paragraphs still give the eye somewhere to land
        issues.append("no numbered/bulleted block — nothing obvious to scroll back to or save")
    else:
        structure_points = 0.0
        issues.append("no re-readable structure (list, steps, or checklist)")

    components = {"hook_fold": hook_points, "read_time": read_points,
                  "scannability": scan_points, "structure": structure_points}
    return {"score": int(round(sum(components.values()))), "components": components,
            "metrics": m, "issues": issues}


def dwell_score(text: Optional[str]) -> int:
    """The 0-100 dwell-proxy score alone — what gets persisted alongside the authenticity score."""
    return dwell_report(text)["score"]


def meets_dwell_heuristics(text: Optional[str]) -> bool:
    """True when a draft passes the structural dwell contract: a full hook before the '...more'
    fold, no wall-of-text paragraph, and enough paragraphs to give the reader white space. Read
    time is scored (and steered) but deliberately NOT part of the pass/fail — a slightly short post
    is weaker, not broken."""
    m = dwell_metrics(text)
    return bool(m["hook_within_fold"] and not m["wall_of_text"]
                and m["paragraphs"] >= DWELL_MIN_PARAGRAPHS)


def shape_for_dwell(text: Optional[str]) -> Optional[str]:
    """Deterministic dwell REPAIR of the one failure a rewrite keeps re-introducing: prose that came
    back as wall-of-text paragraphs. Reflows only over-long paragraphs (reusing the formatter's
    sentence-boundary reflow — no parallel implementation). The only trim is the formatter's
    sentence-boundary cap at LINKEDIN_MAX_CHARS — LinkedIn's own hard limit, which such a post
    already exceeds — so within a postable draft no CTA, hashtag line, or proof detail can be lost.
    Falsy input and already-scannable text are returned unchanged."""
    from cqc_lem.utilities.linkedin_formatter import enforce_post_readability
    if not text or not dwell_metrics(text)["wall_of_text"]:
        return text
    return enforce_post_readability(text, max_chars=LINKEDIN_MAX_CHARS,
                                    target_paragraph_chars=220,
                                    long_paragraph_chars=DWELL_PARAGRAPH_MAX_CHARS)


def dwell_directive() -> str:
    """The WRITER-side dwell rules appended to every post prompt (see `post_writing_directive`):
    hold the reader past the fold, past a minute, and give them something worth coming back to."""
    return (
        "\n\nDWELL TIME (the dominant 2026 ranking signal — a reader who holds for 60+ seconds is "
        "worth far more than a like):\n"
        f"- The first {LINKEDIN_FOLD_CHARS} characters are ALL that show before the '...more' fold. "
        "Open an information gap there and do NOT close it: name the stakes, the number, or the "
        "surprise, and put the payoff BELOW the fold so expanding is the obvious next move.\n"
        "- Never resolve the hook in the opening line. A first line that already gave the answer "
        "gets scrolled past in under three seconds.\n"
        f"- Target {DWELL_TARGET_WORDS_MIN}-{DWELL_TARGET_WORDS_MAX} words — roughly "
        f"{int(DWELL_TARGET_WORDS_MIN / DWELL_READ_WPM * 60)}-"
        f"{int(DWELL_TARGET_WORDS_MAX / DWELL_READ_WPM * 60)} seconds of reading. Earn the length "
        "with substance; padding to hit the count is worse than a short post.\n"
        f"- At least {DWELL_MIN_PARAGRAPHS} paragraphs, each separated by a BLANK LINE, and no "
        f"paragraph longer than {DWELL_PARAGRAPH_MAX_CHARS} characters. White space is what keeps "
        "the eye descending.\n"
        "- Every paragraph must earn the next one: end sections on a small open loop, a question, "
        "or a turn, so stopping mid-post feels like leaving something unfinished.\n"
        "- Where the content allows, include a short numbered list, set of steps, or checklist — "
        "re-readable structure is what readers scroll back through and SAVE.\n"
    )


def save_worthy_directive(content_type: str = "carousel") -> str:
    """Reference-framing rules that make a CAROUSEL/document worth SAVING rather than swiping past
    (issue #391 / C2). A saved carousel is the strongest 2026 signal there is: it holds the reader
    slide-by-slide (dwell) and gets re-opened later. The whole trick is to design a REFERENCE the
    reader expects to need again, not an ad they consume once."""
    noun = "carousel" if content_type == "carousel" else "document"
    return (
        f"\n\nDESIGN THIS {noun.upper()} TO BE SAVED (saves and slide-by-slide dwell are the "
        "strongest 2026 signals — a swipe-past teaches the feed to bury the next one):\n"
        f"- Frame it as a REUSABLE REFERENCE the reader will need again: a checklist, a playbook, a "
        f"named framework, or a before/after comparison — never a one-time announcement.\n"
        "- The cover slide must promise exactly what the reader gets and how many parts it has "
        "(e.g. 'The 5 checks I run before every launch'), so the count itself pulls the swipe.\n"
        "- ONE idea per slide, self-contained enough to be screenshotted alone, in a numbered "
        "sequence the reader can return to at step 3 without re-reading steps 1 and 2.\n"
        "- Each slide's body must carry a specific: a number, a named example, or a concrete "
        "action. Abstract slides are what readers swipe past.\n"
        "- The final slide recaps the whole thing as a compact list worth screenshotting, and the "
        "call to action closes with a soft 'save this for the next time you...' — never "
        "engagement-bait, never 'follow for more'.\n"
    )


def post_writing_directive() -> str:
    """Channel-craft rules for SHORT-FORM feed posts, appended to every post prompt. This replaces
    the old one-size-fits-all 'viral post framework' suffix (which forced every post into the same
    ten-word-sentence, ten-hashtag template — the exact sameness the blueprint system exists to
    kill). Structure/hook/CTA come from the assigned blueprint; these are the invariant rules."""
    from cqc_lem.utilities.linkedin_formatter import PLAIN_PUNCTUATION_DIRECTIVE
    return (
        "\n\nLinkedIn post craft rules (always apply):\n"
        "- The FIRST line is the hook and must land within the first 210 characters — that is all "
        "that shows before LinkedIn's '...more' fold. No throat-clearing, no preamble.\n"
        "- Total length roughly 1300-2000 characters: long enough to deliver real value, short "
        "enough to hold a scroller.\n"
        "- Short sentences and short paragraphs (1-3 lines), with a blank line between paragraphs "
        "for white space.\n"
        "- Plain, conversational language; no jargon, no hype words, no markdown syntax of any kind.\n"
        "- Every claim needs a specific: a number, a named example, or a concrete step — from the "
        "provided source material or genuinely well-established knowledge; NEVER invent statistics.\n"
        "- " + cta_policy_directive() + "\n"
        "- No external links in the body.\n"
        "- Hashtags are OFF unless the style requirements above explicitly allow them — posts "
        f"without hashtags reach more people in 2026. When allowed, at most {HASHTAG_MAX} highly "
        "relevant ones on the final line.\n"
        "- " + PLAIN_PUNCTUATION_DIRECTIVE + "\n"
        + dwell_directive()
        + "\n- Output ONLY the final post text — no quotes, no labels, no explanation.\n"
    )


# ---------------------------------------------------------------------------
# COMMENT QUALITY CONTRACT v2 + comment-side similarity gate (issue #617 / G2). Sampled production
# comments opened with "I totally agree that...", ran one [validate]+[generic insight]+[question]
# template, and near-duplicated each other across different posts the same day — exactly the
# semantic-sameness + pattern signal LinkedIn's May 2026 crackdown demotes out of "Most Relevant".
# Posts already had a similarity gate (POST_SIMILARITY_MAX above); comments had nothing.
#
# Both halves live HERE, in the shared core, so the writer side and the checking side can never
# drift (and so no parallel comment-only helper module appears):
#   - `comment_contract_directive()` steers generation — what a comment MUST carry, what it may
#     never open with, and the "write for the post's READERS, add what the post missed" framing.
#   - `comment_contract_report()` measures the finished draft deterministically (no LLM, no I/O),
#     and `comment_similarity_report()` rejects a draft that repeats the user's own recent comments.
# The caller (ai_helper.generate_ai_response) regenerates a failing draft a bounded number of times
# and then ships NOTHING — a skipped post costs one comment; a template comment costs reach.
# ---------------------------------------------------------------------------

# A one-liner reads as bot noise no matter how on-topic it is.
COMMENT_MIN_SENTENCES = 2
# How many of the user's most recent posted comments a fresh draft is deduped against.
COMMENT_HISTORY_LIMIT = 50
# Meaningful tokens a draft must share with the target post to count as "references a SPECIFIC
# claim from it". Capped by what the post actually offers, so a very short post stays gradeable.
COMMENT_POST_REFERENCE_TOKENS = 3
# Total generation attempts before the post is skipped (initial draft + regenerations).
COMMENT_GATE_MAX_ATTEMPTS_DEFAULT = 3
# Embedding cosine ABOVE which two comments are "the same comment again" (the gate rejects on
# `score > threshold`, matching the post gate's strictly-greater comparison). Deliberately NOT
# the POST_SIMILARITY_MAX value: that ceiling grades TOKEN-SET OVERLAP, where two unrelated texts
# score ~0.2-0.4, while two unrelated professional comments still embed at cosine ~0.4-0.6. 0.82 is
# the same "same underlying text" cosine bar the feedback-dedup uses, and it is what the lexical
# ceiling below (which IS the post gate's scale) mirrors on the fallback path.
COMMENT_SIMILARITY_MAX_DEFAULT = 0.82
COMMENT_LEXICAL_SIMILARITY_MAX_DEFAULT = POST_SIMILARITY_MAX_DEFAULT
COMMENT_EMBEDDING_MODEL = "lem-embedding"
# Comments are short; this only guards against a pathological log row inflating the embed call.
COMMENT_EMBED_CHARS = 2000

# The validation-filler openers a comment may never start on — the exact tells sampled from
# production logs 89-96. Extend per-deploy with COMMENT_FILLER_OPENERS (never shrink: these are
# the pattern LinkedIn scores against).
COMMENT_FILLER_OPENERS: tuple = (
    # The seven named in issue #617 come first — `comment_contract_directive` shows the head of
    # this tuple to the writer, so the canonical tells are the ones it always names.
    "i totally agree", "you raise an excellent", "great post", "spot on", "so insightful",
    "love this", "well said",
    # Same-shape siblings observed alongside them.
    "totally agree", "i completely agree", "couldn't agree more", "could not agree more",
    "you raise a great", "you make an excellent", "you make a great",
    "great point", "great insight", "great read", "great take",
    "very insightful", "well put", "thanks for sharing", "this resonates", "so true",
)

# Only the OPENING of a comment is graded for filler — "Great point about the 40% figure" two
# paragraphs in is a callback, not a validation opener. Two sentences covers the "Wow. Great
# post!" shape without reaching into the body.
_OPENER_SCAN_SENTENCES = 2

# A comment carrying a real number is bringing evidence rather than vibes.
_DATA_POINT_RE = re.compile(
    r"\d|\b(?:percent|dozens?|hundreds?|thousands?|millions?|billions?)\b", re.IGNORECASE)

# Respectful disagreement — the highest-value comment there is, and the one a validation template
# never produces.
_DISAGREEMENT_RE = re.compile(
    r"\b(?:disagree|push ?back|counter-?point|one caveat|the catch|less convinced|"
    r"not (?:so )?sure|i'?d (?:argue|challenge|question|push)|"
    r"where i (?:differ|part ways)|the other way (?:a)?round|in practice though)\b",
    re.IGNORECASE)

# First-hand experience: the commenter did the thing, not just read about it.
_EXPERIENCE_CUE_RE = re.compile(
    r"\b(?:in my experience|when (?:i|we)|our team|i'?ve|we'?ve|i had|we had)\b"
    r"|\b(?:i|we)\s+(?:once\s+|just\s+|recently\s+|actually\s+)?"
    r"(?:ran|run|tried|built|shipped|tested|worked|saw|watched|noticed|found|learned|spent|"
    r"switched|started|stopped|used|measured|rebuilt|rewrote|hired|migrated)\b",
    re.IGNORECASE)

# Reflex closers that ask for a reply without giving one anywhere to go — they do NOT count as the
# contract's "genuine question" value-add.
_GENERIC_QUESTIONS: frozenset = frozenset({
    "thoughts", "agree", "right", "what do you think", "any thoughts", "am i wrong", "who else",
    "make sense", "sound familiar", "curious what others think",
})

_SMART_PUNCTUATION = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"'})


def _plain(text: Optional[str]) -> str:
    """Lowercased text with smart quotes folded to ASCII, so 'couldn’t' and "couldn't" match
    the same ban entry."""
    return (text or "").translate(_SMART_PUNCTUATION).lower()


def _sentences(text: Optional[str]) -> list:
    return [s.strip() for s in _PROOF_SENTENCE_SPLIT.split(text or "") if s.strip()]


def comment_filler_openers() -> tuple:
    """The banned validation-filler openers, read at call time (the POST_SIMILARITY_MAX live-env
    pattern). `COMMENT_FILLER_OPENERS` (comma-separated) EXTENDS the built-in list so ops can ban a
    newly-observed tell without a deploy; it can never shrink it."""
    raw = (os.environ.get("COMMENT_FILLER_OPENERS") or "").split(",")
    extra = [p.strip().lower() for p in raw]
    return COMMENT_FILLER_OPENERS + tuple(
        p for p in dict.fromkeys(extra) if p and p not in COMMENT_FILLER_OPENERS)


def filler_openers_found(text: Optional[str]) -> list:
    """Every banned filler phrase present in the comment's OPENING. The trailing guard keeps
    "a great post-mortem" from matching the "great post" ban."""
    opening = _plain(" ".join(_sentences(text)[:_OPENER_SCAN_SENTENCES]))
    return [p for p in comment_filler_openers()
            if re.search(rf"\b{re.escape(p)}\b(?![-\w])", opening)]


def references_post(comment: Optional[str], post_content: Optional[str]) -> bool:
    """True when the comment demonstrably engages with THIS post: it shares enough meaningful
    (non-stopword) vocabulary with the post that it could not sit unchanged under a different one.
    A post with no extractable content can't be graded against, so it passes rather than burning
    regenerations on a grounding we cannot check."""
    post_words = content_tokens(post_content)
    if not post_words:
        return True
    shared = content_tokens(comment) & post_words
    return len(shared) >= min(COMMENT_POST_REFERENCE_TOKENS, len(post_words))


def has_genuine_question(text: Optional[str]) -> bool:
    """True when the comment asks something the author has to think about — a reflex closer
    ("Thoughts?", "Agree?") does not count."""
    for sentence in _sentences(text):
        if not sentence.endswith("?"):
            continue
        if _plain(sentence).strip("?!. ") in _GENERIC_QUESTIONS:
            continue
        if len(content_tokens(sentence)) >= 3:
            return True
    return False


def comment_value_adds(text: Optional[str]) -> list:
    """Which of the contract's four value-adds the comment actually carries: own experience, a data
    point, respectful disagreement, or a genuine question."""
    found = []
    if any(_EXPERIENCE_CUE_RE.search(s) for s in _sentences(text)):
        found.append("experience")
    if _DATA_POINT_RE.search(text or ""):
        found.append("data_point")
    if _DISAGREEMENT_RE.search(text or ""):
        found.append("disagreement")
    if has_genuine_question(text):
        found.append("question")
    return found


def comment_contract_report(comment: Optional[str], post_content: Optional[str] = None) -> dict:
    """Grade one finished comment draft against the quality contract. Deterministic — no LLM, no
    I/O — so the same draft always gets the same verdict and the tests can assert it.

    `value_add` requires AT LEAST one of the four, not exactly one: every comment blueprint closes
    on a question by design, so an exactly-one rule would reject every well-formed draft that also
    brought experience or a number — the opposite of what the contract is for."""
    text = (comment or "").strip()
    fillers = filler_openers_found(text)
    value_adds = comment_value_adds(text)
    checks = {
        "not_empty": bool(text),
        "min_sentences": len(_sentences(text)) >= COMMENT_MIN_SENTENCES,
        "no_filler_opener": not fillers,
        "references_post": references_post(text, post_content),
        "value_add": bool(value_adds),
    }
    failures = []
    if not checks["not_empty"]:
        failures.append("empty comment")
    if not checks["min_sentences"]:
        failures.append(f"fewer than {COMMENT_MIN_SENTENCES} sentences")
    if not checks["no_filler_opener"]:
        failures.append("opens with validation filler (" + ", ".join(fillers) + ")")
    if not checks["references_post"]:
        failures.append("does not reference a specific claim from the target post")
    if not checks["value_add"]:
        failures.append("adds no experience, data point, disagreement, or genuine question")
    return {"passes": all(checks.values()), "checks": checks, "failures": failures,
            "value_adds": value_adds, "filler_openers": fillers}


def meets_comment_contract(comment: Optional[str], post_content: Optional[str] = None) -> bool:
    """True when the draft satisfies every rule of the comment quality contract."""
    return comment_contract_report(comment, post_content)["passes"]


def comment_similarity_max() -> float:
    """Embedding-cosine ceiling for a fresh comment vs the user's recent ones, read at call time
    (the POST_SIMILARITY_MAX live-env pattern) so ops can tune COMMENT_SIMILARITY_MAX without a
    restart."""
    raw = (os.environ.get("COMMENT_SIMILARITY_MAX") or "").strip()
    try:
        return float(raw) if raw else COMMENT_SIMILARITY_MAX_DEFAULT
    except ValueError:
        return COMMENT_SIMILARITY_MAX_DEFAULT


def comment_lexical_similarity_max() -> float:
    """Token-overlap ceiling used when embeddings are unavailable — the post gate's scale and
    default, so the fallback path stays as strict as the one comments were missing."""
    raw = (os.environ.get("COMMENT_LEXICAL_SIMILARITY_MAX") or "").strip()
    try:
        return float(raw) if raw else COMMENT_LEXICAL_SIMILARITY_MAX_DEFAULT
    except ValueError:
        return COMMENT_LEXICAL_SIMILARITY_MAX_DEFAULT


def comment_gate_max_attempts() -> int:
    """Total generation attempts the comment gate may spend on one post before skipping it."""
    raw = (os.environ.get("COMMENT_GATE_MAX_ATTEMPTS") or "").strip()
    try:
        return max(1, min(5, int(raw))) if raw else COMMENT_GATE_MAX_ATTEMPTS_DEFAULT
    except ValueError:
        return COMMENT_GATE_MAX_ATTEMPTS_DEFAULT


def embed_comments(texts: list) -> Optional[list]:
    """Embed a batch of comment texts in ONE `lem-embedding` call (the draft plus the history it is
    compared against). Returns None on any failure — the caller then grades with the deterministic
    token-overlap measure, never with "nothing is similar"."""
    batch = [str(t or "")[:COMMENT_EMBED_CHARS] for t in (texts or [])]
    if not batch or not all(batch):
        return None
    try:
        # Lazy imports keep this module's import graph free of the AI client and the logger.
        from cqc_lem.utilities.ai.client import client
        response = client.embeddings.create(model=COMMENT_EMBEDDING_MODEL, input=batch)
        rows = sorted(response.data, key=lambda row: getattr(row, "index", 0) or 0)
        vectors = [as_vector(list(row.embedding)) for row in rows]
    except Exception as exc:
        from cqc_lem.utilities.logger import log_warning
        log_warning("Comment embedding failed — dedup falls back to token overlap", exc=exc,
                    ai_model=COMMENT_EMBEDDING_MODEL)
        return None
    if len(vectors) != len(batch) or not all(vectors):
        return None
    return vectors


def comment_similarity_report(draft: Optional[str], recent_comments: list) -> dict:
    """How close a fresh comment sits to the user's own last `COMMENT_HISTORY_LIMIT` comments.
    Prefers embedding cosine (it catches the same comment reworded into different vocabulary, which
    is exactly what a template produces) and degrades to token overlap when the embedding endpoint
    is unavailable — each measure graded against its OWN ceiling. Empty history ⇒ no API call at
    all, so a first-ever comment costs nothing."""
    text = (draft or "").strip()
    history = [c.strip() for c in (recent_comments or []) if (c or "").strip()][:COMMENT_HISTORY_LIMIT]
    if not text or not history:
        return {"score": 0.0, "threshold": comment_similarity_max(), "match": None,
                "measure": "none", "too_similar": False}
    vectors = embed_comments([text] + history)
    if vectors:
        threshold = comment_similarity_max()
        scores = [cosine_similarity(vectors[0], v) for v in vectors[1:]]
        measure = "embedding"
    else:
        threshold = comment_lexical_similarity_max()
        scores = [text_similarity(text, c) for c in history]
        measure = "lexical"
    best = max(range(len(scores)), key=lambda i: scores[i])
    return {"score": round(scores[best], 4), "threshold": threshold, "match": history[best],
            "measure": measure, "too_similar": scores[best] > threshold}


# Mirrors `experiments.COMMENT_CONTRACT_AUTHOR_QUESTION`. Kept as a literal (the same reason
# ai/client.py mirrors observability's system sentinel) so the shared content core never imports the
# experiment plumbing; tests/unit/utilities/test_experiments.py pins the two together.
COMMENT_CONTRACT_AUTHOR_QUESTION_VARIANT = "author-question"

# The pilot prompt variant (issue #652, `experiments.COMMENT_CONTRACT_AUTHOR_QUESTION`). Control
# lets the closing ask be any of the four value-adds; this arm REQUIRES the comment to end on a
# question only the post's author could answer, which is the shape most likely to earn the reply the
# whole contract exists to earn. Measured against author-reply rate (D4) from the #628 outcome sweep.
#
# It is worded as an ADDITIONAL rule rather than a rewrite of rules 1-6 on purpose: the deterministic
# `comment_contract_report` grades the same six rules in both arms, so an arm that loosened one of
# them would change what "passes the gate" means and the two arms would no longer be comparable.
COMMENT_AUTHOR_QUESTION_DIRECTIVE = (
    "7. END ON A QUESTION ONLY THIS AUTHOR COULD ANSWER — about their specific numbers, their "
    "decision, or what they saw happen. A question anyone in the thread could answer just as well "
    "(\"what do you all think?\") does not count, and neither does a question the post already "
    "answered. One question, at the end, and nothing after it.\n"
)


def comment_contract_directive(variant: Optional[str] = None) -> str:
    """The WRITER-side contract injected into every fresh feed-comment prompt. Rule 5 is the "Tip
    #8" framing: a comment's audience is the post's READERS, and its job is to add what the post
    missed — flattery for the author earns nothing and is what the 2026 ranking demotes.

    `variant` is the arm of the pilot prompt experiment (#652). An unknown or missing variant is the
    control contract, so a mis-provisioned flag can only ever produce today's prompt."""
    banned = ", ".join(f"'{p}'" for p in comment_filler_openers()[:8])
    extra = (COMMENT_AUTHOR_QUESTION_DIRECTIVE
             if variant == COMMENT_CONTRACT_AUTHOR_QUESTION_VARIANT else "")
    return (
        "\n\nCOMMENT QUALITY CONTRACT — a draft that misses ANY of these is thrown away and "
        "rewritten, so satisfy all of them:\n"
        "1. REFERENCE A SPECIFIC CLAIM from the post — quote or paraphrase the actual sentence, "
        "number, or example you are responding to. A comment that could sit unchanged under any "
        "other post has already failed.\n"
        "2. ADD AT LEAST ONE thing the post did not have: a first-hand experience of your own, a "
        "concrete data point you actually know, a respectful disagreement, or a genuinely curious "
        "question the post leaves open. One done well beats a checklist of all four, and never "
        "invent a statistic to satisfy this.\n"
        f"3. AT LEAST {COMMENT_MIN_SENTENCES} sentences. A one-liner reads as a bot.\n"
        f"4. NEVER open with validation filler — banned openers include {banned}. Open on the "
        "substance: the specific claim you are engaging with.\n"
        "5. WRITE FOR THE POST'S READERS, NOT ITS AUTHOR. Your job is to add what the post missed "
        "so the next person scrolling the thread learns something. Flattery adds nothing, and "
        "LinkedIn's 2026 ranking demotes comments that only validate.\n"
        "6. Use your own words and rhythm every time — never reuse the shape, opening, or phrasing "
        "of a comment you have already left on another post.\n"
        + extra
    )


def comment_retry_directive(failures: list, offending_comment: Optional[str] = None) -> str:
    """The regeneration steer after a draft fails the gate: name exactly what went wrong and, for a
    near-duplicate, show the earlier comment the retry must not resemble."""
    lines = ["\n\nYOUR PREVIOUS DRAFT WAS REJECTED. Fix ALL of this and write a genuinely "
             "different comment:"]
    lines += [f"- {reason}" for reason in (failures or []) if reason]
    if offending_comment:
        lines.append("- It was too close to a comment this author already left elsewhere. Take a "
                     "different angle, a different opening, and different words than:\n  \""
                     + str(offending_comment).strip()[:400] + "\"")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# SAVE-TARGETED ARCHETYPE SUPPORT: number-led hook constraints + the NO-FABRICATION guard
# (issue #619 / G4). The build receipt and the bookmarkable compendium are the two save-optimized
# post shapes, and both trade entirely on REAL specifics — which is exactly what an LLM will
# hallucinate if left alone. Both halves live HERE, in the shared core, so the writer side and the
# checking side can never drift (the same reason the comment contract above lives here):
#   - `hook_constraint_directive()` / `hook_report()` — the hook must lead with a real number and
#     fit the mobile feed's ~140-char preview.
#   - `fact_anchor_directive()` / `fact_grounding_report()` — the writer may state a specific ONLY
#     when a verified fact backs it; anything else ships as a clearly-marked placeholder, and a
#     draft that invented a number anyway is blocked from auto-publishing.
# The verified facts come from the story bank (issue #620 / G5). Until that lands, callers pass no
# anchors, so the guard runs in its strictest mode: placeholders only, approval required.
# ---------------------------------------------------------------------------

# LinkedIn's mobile feed truncates the first line well before the desktop fold; ~140 chars is what
# survives intact on a phone, which is where the scroll-stop actually happens.
MOBILE_HOOK_MAX_CHARS = 140

# The one placeholder form the writer may use for a specific it cannot verify — deliberately loud
# so it is impossible to publish by accident and trivial to find in the review UI.
FACT_PLACEHOLDER_EXAMPLE = "[[METRIC: hours saved per week]]"
_FACT_PLACEHOLDER_RE = re.compile(r"\[\[\s*([^\[\]]+?)\s*\]\]")

# A numeric specific: 20, 8%, $4,000, 2.5x. The lookbehind keeps the tail of a version string or a
# decimal from being counted a second time; the thousands group must be a real one (so "Postgres 16,
# and n8n" yields "16", not "16,").
_NUMBER_TOKEN_RE = re.compile(r"(?<![\w.$])\$?\d+(?:,\d{3})*(?:\.\d+)?\s*(?:%|percent|x)?",
                              re.IGNORECASE)
# A bare year is a public, checkable fact, not a project specific — a build receipt saying "in 2026"
# is not fabricating anything, so years never trip the guard.
_YEAR_RE = re.compile(r"^(?:19|20)\d{2}$")
# List/step numbering is structure, not a claim.
_LEADING_ENUM_RE = re.compile(r"^\s*(?:\d+[.)]\s*|step\s+\d+\s*[:.)]?\s*)", re.IGNORECASE)
# A number that is part of a PRODUCT NAME is not a claim the author has to stand behind: the build
# receipt's structure explicitly asks for the exact stack, so "GPT-4o", "Claude 3.5", "Python 3.12"
# and "Postgres 16" must not read as invented metrics (they would burn the one regeneration AND hold
# the post, with a retry directive that mangles the tool names). Only BARE numbers qualify — anything
# carrying a unit ($, %, x) is a metric no matter what precedes it.
_VERSION_AFTER_NAME_RE = re.compile(r"(?:[A-Za-z0-9]-|\b([A-Z][A-Za-z0-9.+#]*)\s)$")
# Capitalized words that routinely OPEN a sentence — they are not product names, so a number after
# one ("The 9 checks I run") stays a claim.
_NON_NAME_CAPITALS = frozenset((
    "a", "an", "the", "this", "that", "these", "those", "it", "i", "we", "you", "they", "my", "our",
    "your", "their", "his", "her", "its", "in", "on", "at", "of", "for", "to", "by", "with", "from",
    "over", "under", "about", "after", "before", "and", "but", "so", "then", "now", "today",
    "yesterday", "here", "there", "all", "some", "each", "every", "only", "just", "last", "next",
    "first", "nearly", "almost", "roughly", "around", "within", "one", "two", "three", "no", "not",
))
# "10k", "3m", "2bn" — a magnitude suffix, i.e. a metric, not a build number.
_MAGNITUDE_SUFFIXES = frozenset(("k", "m", "b", "bn", "mm"))


def hook_constraint_directive() -> str:
    """The hook rules for a number-led archetype: open on a real number, inside the mobile budget."""
    return (
        "HOOK CONSTRAINTS (this archetype lives or dies on its first line):\n"
        "- LEAD WITH A REAL NUMBER — the count, the cost, the duration, the scale. The number must "
        "appear in the FIRST line, not the second.\n"
        f"- Keep that first line to {MOBILE_HOOK_MAX_CHARS} characters or fewer so it survives the "
        "mobile feed preview whole; a hook that truncates mid-thought reads as noise.\n"
        "- The number must come from a verified fact you were given. If you do not have one, write "
        "the placeholder instead — NEVER estimate, round, or invent it."
    )


def hook_report(text: Optional[str], content_type: str = "post", format_key=None) -> dict:
    """Grade a finished draft's first line against the hook constraints. Deterministic — no LLM, no
    I/O. `required` is False (and `passes` therefore True) for an archetype that has no number-led
    constraint, so this is safe to run over every draft."""
    hook = opening_line(text or "", max_chars=LINKEDIN_MAX_CHARS)
    required = bool(format_meta(content_type, format_key).get("hook_styles")) if format_key else False
    # A placeholder in the hook IS the number slot, correctly deferred — it counts as number-led.
    number_led = bool(re.search(r"\d", _FACT_PLACEHOLDER_RE.sub("0", hook)))
    within_budget = 0 < len(hook) <= MOBILE_HOOK_MAX_CHARS
    issues = []
    if required and not number_led:
        issues.append("hook does not lead with a number")
    if required and not within_budget:
        issues.append(f"hook is {len(hook)} chars (mobile budget is {MOBILE_HOOK_MAX_CHARS})")
    return {"hook": hook, "chars": len(hook), "number_led": number_led,
            "within_mobile_budget": within_budget, "required": required,
            "passes": not issues, "issues": issues}


def fact_anchor_directive(anchors: Optional[list] = None) -> str:
    """The NO-FABRICATION rules for a fact-anchored archetype. With verified facts, they are the only
    specifics the writer may state. With none, every specific must ship as a marked placeholder —
    the #416 policy applied where it bites hardest: a receipt full of invented numbers is worse than
    no receipt at all."""
    facts = [str(f).strip() for f in (anchors or []) if str(f or "").strip()]
    lines = ["NO-FABRICATION RULE (this archetype is built on specifics, so this is absolute):"]
    if facts:
        lines.append("- These are the ONLY verified facts you may state as the author's own. Use "
                     "their numbers and names EXACTLY as written:")
        lines += [f"  * {f}" for f in facts]
        lines.append("- Any OTHER specific — a number, a duration, a cost, a tool you were not "
                     f"given — must be written as a placeholder, e.g. {FACT_PLACEHOLDER_EXAMPLE}, "
                     "for the author to fill in.")
    else:
        lines.append("- You have been given NO verified project facts, so you may not state a "
                     "single specific of your own. Every number, duration, cost, tool name, and "
                     "outcome must be written as a placeholder in double square brackets, e.g. "
                     f"{FACT_PLACEHOLDER_EXAMPLE} or [[TOOL: which model handled extraction]].")
    lines.append("- This OVERRIDES every other instruction that asks for a specific, including the "
                 "personal-proof slot: if no verified fact gives you the number, the proof detail "
                 "is a placeholder too.")
    lines.append("- Write the surrounding prose in full so the author only has to fill the brackets."
                 " NEVER estimate, round, illustrate, or invent a specific — an invented number "
                 "gets the whole draft blocked, an honest placeholder does not.")
    return "\n".join(lines)


def fact_placeholders(text: Optional[str]) -> list:
    """Every clearly-marked placeholder in a draft, in order — the specifics the author still has to
    fill in before it can publish."""
    return [m.group(1).strip() for m in _FACT_PLACEHOLDER_RE.finditer(text or "") if m.group(1).strip()]


def _normalize_number(token: str) -> str:
    """A numeric token reduced to comparable digits: '$4,000' -> '4000', '8 %' -> '8', '2.5x' -> '2.5'."""
    return re.sub(r"[^\d.]", "", (token or "").replace(",", "")).rstrip(".")


def _anchor_numbers(anchors: Optional[list]) -> set:
    """Every number the verified facts actually contain — the set a draft's specifics are checked
    against."""
    found = set()
    for fact in anchors or []:
        for match in _NUMBER_TOKEN_RE.finditer(str(fact or "")):
            value = _normalize_number(match.group(0))
            if value:
                found.add(value)
    return found


def _self_evident_counts(text: Optional[str]) -> set:
    """Counts the draft PROVES on its own page: the number of numbered/bulleted entries it contains.
    'The 7 checks I run' above a 7-item list is self-verifying, so it is not a fabrication."""
    count = len(_LIST_LINE_RE.findall(text or ""))
    return {str(count)} if count else set()


def _is_product_version(sentence: str, start: int, raw: str) -> bool:
    """True when a bare number is part of a TOOL/MODEL NAME rather than a claim — 'GPT-4o',
    'Claude 3.5', 'Postgres 16'. A number carrying a unit ($, %, x) is always a metric, and a
    capitalized word that merely opens the sentence ('The 9 checks') is not a product name."""
    if re.search(r"[$%x]|percent", raw, re.IGNORECASE):
        return False
    end = start + len(raw)  # raw is stripped, so this excludes whitespace the token regex ate
    tail = re.match(r"[A-Za-z]+", sentence[end:])
    # Letters welded to the number mean a build ("GPT-4o") — EXCEPT a magnitude suffix, which is
    # exactly how a metric gets written ("saved 10k hours").
    if tail and tail.group(0).lower() not in _MAGNITUDE_SUFFIXES:
        return True
    match = _VERSION_AFTER_NAME_RE.search(sentence[:start])
    if not match:
        return False
    name = match.group(1)
    if name is None:  # hyphen-attached to a word/number: "gpt-4", "llama-3"
        return True
    return name.lower() not in _NON_NAME_CAPITALS


def numeric_claims(text: Optional[str]) -> list:
    """Every numeric specific a draft ASSERTS, as {value, raw, context} dicts. Placeholders, list/step
    numbering, bare years, and tool/model version numbers are excluded — none of them is a claim the
    author has to stand behind."""
    body = _FACT_PLACEHOLDER_RE.sub(" ", text or "")
    body = "\n".join(_LEADING_ENUM_RE.sub("", line) for line in body.splitlines())
    claims = []
    for sentence in _PROOF_SENTENCE_SPLIT.split(body):
        stripped = sentence.strip()
        if not stripped:
            continue
        for match in _NUMBER_TOKEN_RE.finditer(stripped):
            raw = match.group(0).strip()
            value = _normalize_number(raw)
            if not value or _YEAR_RE.match(value) and not re.search(r"[$%]", raw):
                continue
            if _is_product_version(stripped, match.start(), raw):
                continue
            claims.append({"value": value, "raw": raw, "context": stripped})
    return claims


def fact_grounding_report(text: Optional[str], anchors: Optional[list] = None) -> dict:
    """Grade a fact-anchored draft for fabrication. Deterministic — no LLM, no I/O.

    `unverified` holds every numeric specific the draft asserts that no verified fact backs and the
    draft does not prove on its own page; ANY of them fails the guard, because that is precisely a
    number the model made up. `placeholders` are the honestly-deferred specifics — they do not fail
    the draft, they make it approval-gated so the author fills them in before it publishes."""
    placeholders = fact_placeholders(text)
    anchor_numbers = _anchor_numbers(anchors) | _self_evident_counts(text)
    verified, unverified = [], []
    for claim in numeric_claims(text):
        (verified if claim["value"] in anchor_numbers else unverified).append(claim)
    return {
        "placeholders": placeholders,
        "verified": verified,
        "unverified": unverified,
        "unverified_values": [c["raw"] for c in unverified],
        "needs_approval": bool(placeholders),
        "passes": not unverified,
    }


def meets_fact_grounding(text: Optional[str], anchors: Optional[list] = None) -> bool:
    """True when the draft states no specific that a verified fact does not back."""
    return fact_grounding_report(text, anchors)["passes"]


def fact_retry_directive(report: dict) -> str:
    """The regeneration steer after a draft invented specifics: name the exact numbers it may not
    keep, and how to defer them instead."""
    offenders = [str(c.get("raw")) for c in (report or {}).get("unverified", []) if c.get("raw")]
    lines = ["\n\nYOUR PREVIOUS DRAFT INVENTED SPECIFICS AND WAS REJECTED. Rewrite it with every "
             "unverified number replaced by a placeholder:"]
    if offenders:
        lines.append("- These numbers are not backed by any verified fact and must NOT appear: "
                     + ", ".join(dict.fromkeys(offenders)))
    lines.append(f"- Replace each one with a labelled placeholder, e.g. {FACT_PLACEHOLDER_EXAMPLE}, "
                 "and keep the prose around it complete.")
    return "\n".join(lines) + "\n"


def carousel_blueprint_directive(blueprint: dict, fact_anchors: Optional[list] = None) -> str:
    """The post archetype's shape mapped onto a CAROUSEL/document (issue #619 / G4). A build receipt
    renders naturally as a document post — the highest-engagement LinkedIn format there is — so the
    carousel generator draws its beats from the SAME post menu instead of a parallel carousel-only
    prompt. Returns '' for a blueprint with no known post format."""
    if not isinstance(blueprint, dict):
        return ""
    fmt = normalize_key("post", "format", blueprint.get("format"))
    if not fmt:
        return ""
    meta = POST_FORMATS[fmt]
    lines = [
        "\n\nTHIS CAROUSEL'S ASSIGNED ARCHETYPE — it OVERRIDES the generic slide guidance above:",
        f"ARCHETYPE: {meta['label']}. {meta['guidance']}",
        "Map these beats ONTO THE SLIDES IN THIS ORDER — the cover carries the first beat, the "
        "closing slide the last, and the middle beats become one slide each (merge adjacent beats "
        "only if the schema has fewer slots):",
    ]
    lines += [f"{i}. {s}" for i, s in enumerate(meta["structure"], 1)]
    if meta.get("hook_styles"):
        lines.append(hook_constraint_directive())
    if meta.get("fact_anchored"):
        lines.append(fact_anchor_directive(fact_anchors))
    return "\n".join(lines) + "\n"
