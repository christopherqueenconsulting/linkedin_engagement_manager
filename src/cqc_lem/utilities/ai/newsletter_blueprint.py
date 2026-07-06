"""Edition BLUEPRINT system: named, testable sets of content FORMATS (archetypes), opening HOOK
styles, per-format STRUCTURE skeletons, and CTA styles — plus the variety rules that GUARANTEE
consecutive editions never share a format or hook style. The subject dedup (V49) fixed repeated
topics; this fixes repeated SHAPE — every planned edition gets an explicit
{subject, angle, format, hook_style, structure, cta_style} blueprint the writer must follow, so no
two neighboring editions open the same way or read from the same skeleton."""

from typing import Optional

# How many most-recent historical formats/hook styles a new edition must also avoid (beyond the
# strict no-consecutive-repeat rule). 3 keeps rotation fresh while 8 options stay pickable.
_AVOID_WINDOW = 3

FORMATS: dict = {
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
                     "disagrees with — then spend the edition earning it. The unresolved tension is "
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
        "guidance": ("Open with a line the reader does NOT expect from this newsletter — a blunt "
                     "confession, an unusual image, a one-word sentence — that breaks the scroll "
                     "rhythm, then bridge to the topic within two lines."),
    },
    "direct_promise": {
        "label": "Direct Promise",
        "guidance": ("Open by naming EXACTLY what the reader will be able to do by the end of this "
                     "edition — a specific outcome with a specific scope, zero hype words."),
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


def _normalize(value, options: dict) -> Optional[str]:
    if not value:
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


def normalize_format(value) -> Optional[str]:
    return _normalize(value, FORMATS)


def normalize_hook_style(value) -> Optional[str]:
    return _normalize(value, HOOK_STYLES)


def normalize_cta_style(value) -> Optional[str]:
    return _normalize(value, CTA_STYLES)


def format_structure(format_key: str) -> list:
    meta = FORMATS.get(normalize_format(format_key) or "")
    return list(meta["structure"]) if meta else []


def blueprint_options_text() -> str:
    """The menu of formats/hooks/CTAs given to the PLANNER so it assigns real, known values."""
    lines = ["Available FORMATS (use the key on the left):"]
    lines += [f"- {k}: {m['label']} — {m['guidance']}" for k, m in FORMATS.items()]
    lines.append("\nAvailable HOOK STYLES (use the key on the left):")
    lines += [f"- {k}: {m['label']} — {m['guidance']}" for k, m in HOOK_STYLES.items()]
    lines.append("\nAvailable CTA STYLES (use the key on the left):")
    lines += [f"- {k}: {m['label']} — {m['guidance']}" for k, m in CTA_STYLES.items()]
    return "\n".join(lines)


def _pick(options: dict, recency: list, forbidden: set) -> str:
    """Pick the least-recently-used option outside `forbidden`. `recency` is most-recent-first;
    options never used rank best. Relaxes `forbidden` rather than ever failing."""
    keys = list(options.keys())
    candidates = [k for k in keys if k not in forbidden]
    if not candidates:  # everything forbidden → only hard rule left is "not the immediate previous"
        head = recency[0] if recency else None
        candidates = [k for k in keys if k != head] or keys

    def rank(k: str) -> int:
        return recency.index(k) if k in recency else len(recency) + 1

    return max(candidates, key=rank)


def enforce_blueprint_variety(blueprints: list, recent_formats: list = None,
                              recent_hook_styles: list = None) -> list:
    """Guarantee IN CODE what the planner prompt merely requests: normalize each blueprint's
    format/hook/cta to known keys and reassign any that repeat — no two consecutive editions (nor an
    edition and the most recent history) share a format or hook style, no format/hook repeats within
    the batch or the recent-history window, and every blueprint carries its format's structure."""
    rf = [f for f in (normalize_format(x) for x in (recent_formats or [])) if f]
    rh = [h for h in (normalize_hook_style(x) for x in (recent_hook_styles or [])) if h]
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
        fmt = normalize_format(bp.get("format"))
        if fmt is None or fmt == prev_f or fmt in batch_f or fmt in window_f:
            fmt = _pick(FORMATS, f_recency, {prev_f} | batch_f | window_f)
        hook = normalize_hook_style(bp.get("hook_style"))
        if hook is None or hook == prev_h or hook in batch_h or hook in window_h:
            hook = _pick(HOOK_STYLES, h_recency, {prev_h} | batch_h | window_h)
        cta = normalize_cta_style(bp.get("cta_style"))
        if cta is None or cta == prev_c:
            cta = _pick(CTA_STYLES, [prev_c] if prev_c else [], {prev_c} if prev_c else set())
        item = dict(bp)
        item.update({"format": fmt, "hook_style": hook, "cta_style": cta,
                     "structure": list(FORMATS[fmt]["structure"])})
        out.append(item)
        prev_f, prev_h, prev_c = fmt, hook, cta
        batch_f.add(fmt)
        batch_h.add(hook)
        f_recency.insert(0, fmt)
        h_recency.insert(0, hook)
    return out


def build_regeneration_blueprint(subject: str = None, angle: str = None,
                                 recent_formats: list = None, recent_hook_styles: list = None,
                                 guidance: str = None) -> dict:
    """A fresh blueprint for a single REGENERATED edition, chosen in code (no LLM call): rotate away
    from the recent formats/hooks — including the edition's own previous shape — so a rewrite changes
    form, not just words. Free-text `guidance` may name a format (e.g. 'make it a case study');
    honor it when it does."""
    hinted = normalize_format(guidance) if guidance else None
    if not hinted and guidance:
        low = guidance.lower()
        for k, meta in FORMATS.items():
            if k.replace("_", " ") in low or meta["label"].lower() in low:
                hinted = k
                break
    rf = [f for f in (normalize_format(x) for x in (recent_formats or [])) if f]
    rh = [h for h in (normalize_hook_style(x) for x in (recent_hook_styles or [])) if h]
    fmt = hinted or _pick(FORMATS, rf, {rf[0] if rf else None} | set(rf[:_AVOID_WINDOW]))
    hook = _pick(HOOK_STYLES, rh, {rh[0] if rh else None} | set(rh[:_AVOID_WINDOW]))
    cta = _pick(CTA_STYLES, [], set())
    return {"subject": subject, "angle": angle or "", "format": fmt, "hook_style": hook,
            "cta_style": cta, "structure": list(FORMATS[fmt]["structure"])}


def blueprint_directive(blueprint: dict) -> str:
    """The WRITER-side injection: the assigned format's guidance + ordered structure skeleton, the
    hook style, and the CTA style. Returns '' when the blueprint carries no known format."""
    if not isinstance(blueprint, dict):
        return ""
    fmt = normalize_format(blueprint.get("format"))
    if not fmt:
        return ""
    hook = normalize_hook_style(blueprint.get("hook_style"))
    cta = normalize_cta_style(blueprint.get("cta_style"))
    f_meta = FORMATS[fmt]
    lines = [
        "\nTHIS EDITION'S ASSIGNED BLUEPRINT — it OVERRIDES the default structure above. Follow it exactly:",
        f"FORMAT: {f_meta['label']}. {f_meta['guidance']}",
        "STRUCTURE — write these sections IN THIS ORDER (plain-text subheads, no markdown):",
    ]
    lines += [f"{i}. {s}" for i, s in enumerate(f_meta["structure"], 1)]
    if hook:
        h_meta = HOOK_STYLES[hook]
        lines.append(f"OPENING HOOK STYLE: {h_meta['label']}. {h_meta['guidance']}")
    if cta:
        c_meta = CTA_STYLES[cta]
        lines.append(f"CTA STYLE: {c_meta['label']}. {c_meta['guidance']}")
    return "\n".join(lines) + "\n"


def compact_blueprint(blueprint: dict) -> Optional[dict]:
    """The persistable core of a blueprint (structure skeletons live in code, not the DB)."""
    if not isinstance(blueprint, dict):
        return None
    return {k: blueprint.get(k) for k in ("subject", "angle", "format", "hook_style", "cta_style")}
