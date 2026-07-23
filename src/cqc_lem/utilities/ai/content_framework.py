"""ONE content-framework core for every content type LEM writes. A BLUEPRINT is a named, testable
{format (archetype), hook_style, structure, cta_style} assignment; each content type gets its own
MENU (long-form newsletter archetypes, short-form post archetypes, comment angles) while the
selection, normalization, rotation/anti-repetition, and writer-directive machinery is SHARED — so
newsletters, posts, and comments can never drift apart in how variety is enforced.

This generalizes the newsletter-only blueprint system (V50): the newsletter menu is unchanged, and
posts/comments now rotate through their own menus with the same guarantees — no two consecutive
pieces share a format or hook style, and nothing repeats within the recent-history window."""

import os
import random
import re
from typing import Optional

# How many most-recent historical formats/hook styles a new piece must also avoid (beyond the
# strict no-consecutive-repeat rule). 3 keeps rotation fresh while the menus stay pickable.
_AVOID_WINDOW = 3

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

CTA_STYLES: dict = {
    "reply_question": {
        "label": "Reply-Driving Question",
        "guidance": ("Close with ONE open, specific question about the reader's own situation and "
                     "explicitly invite them to answer in the comments — specific questions get "
                     "replies; 'thoughts?' gets silence."),
    },
    "challenge": {
        "label": "This-Week Challenge",
        "guidance": ("Close by assigning one small, concrete action to take this week, and invite the "
                     "reader to report back in the comments with what happened."),
    },
    "debate": {
        "label": "Invite Disagreement",
        "guidance": ("Close by inviting pushback — name the most likely objection and ask readers to "
                     "tell you where you're wrong in the comments. Disagreement is engagement."),
    },
    "share_forward": {
        "label": "Share With Someone",
        "guidance": ("Close by asking the reader to share this edition with ONE specific kind of "
                     "person who needs it right now, and invite new readers to subscribe."),
    },
    "teaser_next": {
        "label": "Next-Edition Teaser",
        "guidance": ("Close by teasing what the NEXT edition covers — a specific open loop worth "
                     "subscribing for — and invite readers to subscribe so they don't miss it."),
    },
    "poll_prompt": {
        "label": "Either/Or Poll",
        "guidance": ("Close with a crisp either/or question (option A vs option B) readers can answer "
                     "in one comment — a low-effort on-ramp that starts real conversations."),
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
                     "have an easy way in. Never beg — no 'smash that save button'."),
    },
}

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


def _pick(options: dict, recency: list, forbidden: set) -> str:
    """Pick the least-recently-used option outside `forbidden`, choosing RANDOMLY among ties (so a
    user with no history still gets variety instead of always the first menu key). `recency` is
    most-recent-first; options never used rank best. Relaxes `forbidden` rather than ever failing."""
    keys = list(options.keys())
    candidates = [k for k in keys if k not in forbidden]
    if not candidates:  # everything forbidden → only hard rule left is "not the immediate previous"
        head = recency[0] if recency else None
        candidates = [k for k in keys if k != head] or keys

    def rank(k: str) -> int:
        return recency.index(k) if k in recency else len(recency) + 1

    best = max(rank(k) for k in candidates)
    return random.choice([k for k in candidates if rank(k) == best])


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
                     guidance: str = None) -> dict:
    """A fresh blueprint for ONE piece of any content type, chosen in code (no LLM call): rotate
    away from the recent formats/hooks — including the piece's own previous shape — so consecutive
    pieces change form, not just words. Free-text `guidance` may name a format (e.g. 'make it a
    case study'); honor it when it does."""
    menu = _menu(content_type)
    formats, hooks, ctas = menu["formats"], menu["hooks"], menu["ctas"]
    hinted = _normalize(guidance, formats) if guidance else None
    if not hinted and guidance:
        low = guidance.lower()
        for k, meta in formats.items():
            if k.replace("_", " ") in low or meta["label"].lower() in low:
                hinted = k
                break
    rf = [f for f in (_normalize(x, formats) for x in (recent_formats or [])) if f]
    rh = [h for h in (_normalize(x, hooks) for x in (recent_hook_styles or [])) if h]
    fmt = hinted or _pick(formats, rf, {rf[0] if rf else None} | set(rf[:_AVOID_WINDOW]))
    out = {"subject": subject, "angle": angle or "", "format": fmt,
           "structure": list(formats[fmt]["structure"])}
    out["hook_style"] = _pick(hooks, rh, {rh[0] if rh else None} | set(rh[:_AVOID_WINDOW])) if hooks else None
    out["cta_style"] = _pick(ctas, [], set()) if ctas else None
    return out


def blueprint_directive(content_type: str, blueprint: dict) -> str:
    """The WRITER-side injection: the assigned format's guidance + ordered structure skeleton, the
    hook style, and the CTA style. Returns '' when the blueprint carries no known format."""
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
    cta = _normalize(blueprint.get("cta_style"), menu["ctas"])
    if cta:
        c_meta = menu["ctas"][cta]
        lines.append(f"CTA STYLE: {c_meta['label']}. {c_meta['guidance']}")
    if content_type in _PROOF_SLOT_TYPES:
        lines.append(PERSONAL_PROOF_SLOT)
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


def post_similarity_max() -> float:
    """The similarity ceiling, read at call time (same live-env pattern as the research toggles in
    content_research) so ops/tests can tune POST_SIMILARITY_MAX without a restart."""
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
        "- No engagement-bait ('comment YES', 'tag someone', 'smash like') and no external links in "
        "the body.\n"
        "- If hashtags are allowed by the style requirements, at most 3-5 relevant ones on the final "
        "line; otherwise none.\n"
        "- " + PLAIN_PUNCTUATION_DIRECTIVE + "\n"
        "- Output ONLY the final post text — no quotes, no labels, no explanation.\n"
    )
