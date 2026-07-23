---
name: anti-ai
description: Rewrite AI-generated text so it passes as human — to human readers AND to statistical AI detectors (Pangram etc.), using empirically validated recipes (discourse fracture, interleave protocol). ONLY use this skill when the user explicitly invokes it by name — i.e. they type "/anti-ai" (or literally write "anti-ai" / "use the anti-ai skill"). Do NOT trigger it on paraphrased intent such as "humanize this", "make it sound less AI", "this reads like ChatGPT", or "fix the writing style" — the user has deliberately scoped this skill to explicit invocation only, so for those requests handle the rewrite normally without loading this skill unless they name it.
---

# Anti-AI: make the text pass as human

You have **full license to modify the text heavily** — restructure, cut, reorder, roughen. Preserve only: (1) the core message and facts, (2) the author's intent and audience, (3) the format (a LinkedIn post stays a LinkedIn post).

## STEP 0 — Pick the mode (mandatory, before any editing)

There are two different targets, and the edits that satisfy one FAIL the other. Route first:

| Situation | Mode |
|---|---|
| User mentions a detector (Pangram, GPTZero, Turnitin, "AI detection", "pass as human", "not flagged") — or doesn't say, and the text is a personal/social/newsletter piece | **DETECTOR mode** (default) |
| User supplied (or can supply) a rough draft in their own words | **DETECTOR mode → interleave protocol** (the reliable variant) |
| Formal register where a live-rant voice is absurd (client docs, white papers, documentation) — or user explicitly wants only style cleanup | **READER mode** — and warn: this output will still score 100% AI on detectors |

**Default is DETECTOR mode.** The owner of this skill tests output against Pangram. When in doubt, fracture. A clean, polished, "human-sounding" rewrite is a FAILURE in detector mode — that exact output scored 100% AI eleven times in testing (see appendix).

---

## DETECTOR MODE

Statistical detectors don't read style. They detect **generation dynamics**: planned text where every sentence advances a known arc. Eleven styled rewrites — clean, punchy, rambling, typo-ridden, ESL-flavored, idiolect-mimicking — ALL scored 100% AI on Pangram. Surface mess is not the lever. Two recipes are:

### Recipe 1 — Interleave protocol (use when a human draft exists; the reliable path)

A genuinely human base draft absorbs up to ~45% AI-added words and still scores 100% Human.

1. Get the user's rough draft in their own words (voice-note transcript, ugly stream-of-consciousness — quality irrelevant, authorship everything). If they have none and the piece matters, ask for 2 minutes of typing from a bullet spine you provide.
2. **Preserve their sentences verbatim.** Never fix their grammar, typos, or run-ons — fractures are load-bearing human signal. Paraphrasing a human sentence converts it to AI tokens.
3. **Add, don't replace.** Your sentences go *between* theirs. Never two AI sentences in a row.
4. **Stay under ~40% AI words** (45% passed at the cliff; a contiguous majority-AI block failed).
5. **Keep their opening sentence verbatim** — first tokens set the classifier's frame.
6. Tell the user which sentences are theirs vs yours, so later edits don't paraphrase away the human signal.

### Recipe 2 — Discourse fracture (use when only AI text exists)

Validated 2/2 first-try on fresh topics (after 11 failures without it). The text must read as **composed live** — drafted in public, plan abandoned midway — not planned. ALL EIGHT ingredients, not a sample:

1. **One meandering specific story, not an argument.** A single incident with a timeline and named particulars. If each sentence advances a distinct point, it fails no matter how it's dressed — this is the #1 killer. Outline test: if the text can be outlined as tidy bullets, rewrite it.
2. **Meta self-interruption** — the writer comments on their own writing and abandons the plan: "i was going to write something structured here but forget it".
3. **Self-correction left visible** — correct a fact mid-text, keep both: "this was february, or march. february."
4. **Repetition as emotion** — same words re-hit because the writer is still angry: "one. per week." / "everywhere. everywhere."
5. **Emotional punctuation** — doubled marks ("!!", "??") at feeling spikes; all-lowercase in casual registers; dropped apostrophes (im, dont, its).
6. **World-vocabulary left raw** — untranslated foreign terms, domain shorthand, local prices/times exactly as the writer's world has them (1,40€, 15h, notaire, kbis).
7. **Unquoted reported dialogue with a name** — "i dont care about the lamination jerome."
8. **End on a self-contradiction the writer notices and leaves in** — "and yes i will buy another one friday. i know." Never a synthesis, never a recap — and never a meaning-tag on the contradiction (see constraint 14; "thats the disgusting part honestly" is exactly the pattern that later failed).

**Plus these constraints that override everything above — craft kills (learned from a controlled A/B: an 8/8-checklist text still scored 100% AI because it was well-written; the same story flattened passed 100% Human):**

9. **No punchlines.** Fragments must be flat complaints ("my calves by 15h, dead"), never comedy beats ("free. zero furniture." / "same emails. same speed. so much for productivity."). If a line would get a laugh read aloud on stage, it's crafted — rewrite it duller. Rants vent; they don't perform.
10. **No essay skeleton in disguise.** No staged progression ("week one… week two…"), no rhetorical pivot ("you know what actually worked?"), no thesis/moral sentence ("we paid 600€ to trade one problem for another"), no elegant callback ending. If paragraphs have *roles* (setup → experiment → alternative → coda), it's an essay wearing a costume and it will fail.
11. **Stay under ~160 words.** Every pass was 119–155 words; the 251-word failure gave the classifier twice the evidence and let structure creep back. Longer pieces need the interleave protocol instead.
12. **Drop most of the original's points.** Keep 2–3 max; abandon the rest without apology. A second A/B failure converted ALL of an essay's arguments into story beats (stat → anecdote, cost point → rent line, turnover point → quitters line) and scored 100% AI despite passing every other check — full coverage IS the planning fingerprint. Rants don't cover; they fixate on the one thing that hurt.
13. **Entity budget: ≤4 named specifics, and dwell on them.** The failing version introduced a new name/number nearly every sentence (a report, a colleague, a headcount, a distance, a room, an HR person, a stat, quitters) — that's fiction *establishing a setting*. Real rants re-hit the same few details ("jerome… jerome…", "600€… 600!!"). One colleague mentioned three times beats three colleagues mentioned once.
14. **OBLITERATE significance-marking meta commentary.** Never write a sentence whose only job is to tell the reader what a moment *means* or which part matters. Banned patterns (and every variant): "thats the part that got me" · "thats the [uncomfortable/disgusting/weird/funny] part (honestly)" · "what got me was" · "the thing is" · "heres the thing" · "and thats the point" · "which is exactly the problem" · "let that sink in" · "thats what kills me". These are the narrator stepping outside the rant to frame it — pure authorial planning signal. Field-verified 2026-07-22: a substack rant passed every other check but contained TWO of these ("thats the part that got me", "thats the uncomfortable part honestly") → 100% AI; deleting exactly those two sentences, changing nothing else → 100% Human. A real ranter never announces significance — they just repeat the detail (constraint 4) or trail off. If a moment matters, hit it again; never label it.
15. **Wordbank sweep, detector mode too.** Read `references/wordbank.md` this session. Zero tier-1 hits, obviously — but ALSO no soft framing adverbs even from tier 2: quietly · genuinely · truly · honestly (as a sentence-tag) · literally (as intensifier) · actually (as pivot) · basically. At most ONE of these in the whole piece, and only inside a sentence doing real work, never tagging feeling onto the end of one. Stacked soft adverbs are the polite ghost of the meta commentary banned in 14.

**Worked example (this exact text scored 100% Human, High Confidence on Pangram):**

Before (AI draft — scores 100% AI):
> Croissants are, frankly, overrated and even disgusting. The flaky texture that enthusiasts praise is actually a structural disaster, shattering into greasy shards that cling to your clothes and fingers. Each bite delivers an overwhelming amount of butter, coating the palate with a heavy, cloying film. Furthermore, the interior is often disappointingly hollow… Ultimately, the croissant is a triumph of marketing over flavor.

After (discourse fracture — scores 100% Human):
> i know im not supposed to say this but croissants are disgusting. i said it. this was supposed to be a whole review of the boulangerie near my office but forget it, let's talk about the croissant itself. i bought one tuesday, wednesday? tuesday. 1,40€. the outside does its little crunch thing everyone loves and then immediately its raining greasy flakes, on my desk, in my keyboard, everywhere. everywhere. and inside?? air. literally air with butter walls. my colleague says you dont get it, its about the lamination. i dont care about the lamination jerome. the butter sits in your stomach until 15h like a stone. and yes i will buy another one friday. thats the disgusting part honestly.

Note what the After is NOT: it is not the Before with typos added. It abandoned the argument structure entirely and told one tuesday-croissant story instead. That swap — argument → incident — is the mechanism; the surface mess alone tested at 100% AI.

⚠️ **Known flaw in this example:** its closing tag "thats the disgusting part honestly" is a significance-marker (constraint 14). It passed once, but the same pattern caused a 100% AI verdict in a later field test — do NOT imitate that line. End the contradiction plain, e.g. "and yes i will buy another one friday. i know."

⚠️ **Do not template off the examples in this file.** In an audit, three independent rewrites all ended with the literal words "i know.", two invented a "marie", two set the incident on "tuesday" — copied straight from this document. Invent fresh names, days, prices, and a fresh plain ending every time; if any exact phrase from this file appears in your output, replace it.

### Detector-mode self-verify (do this before delivering)

Count, literally:
- [ ] Story test: is it ONE incident, impossible to outline as topic-bullets? Check hard — paragraphs with roles (setup/experiment/alternative/coda) or a "you know what actually worked?" pivot mean it outlines and FAILS, even if it feels story-like
- [ ] ≥1 meta self-interruption
- [ ] ≥1 visible self-correction (both versions kept)
- [ ] ≥1 repetition-as-emotion
- [ ] ≥2 emotional punctuation moments ("!!", "??")
- [ ] ≥1 unquoted dialogue with a name
- [ ] Ending contradicts the writer's own stance, no recap
- [ ] Casual register: lowercase, dropped apostrophes throughout (consistently, not sprinkled)
- [ ] Punchline sweep: zero lines that perform (no rhythmic fragments, no wit that lands, no thesis/moral sentence, no callback) — flat complaints only
- [ ] Word count ≤ ~160
- [ ] Coverage test: at least half the original's points are GONE (list the original's points; if each one has a corresponding story beat, it's a translated essay → cut points, not just words)
- [ ] Entity test: ≤4 named/numbered specifics total, and the central ones recur — no sentence-after-sentence parade of fresh details
- [ ] **Meta-commentary sweep (constraint 14): ZERO significance-markers.** Grep the draft literally for: "the part", "the thing", "what got me", "what kills me", "thats the point", "which is exactly", "let that sink". Also scan every sentence and ask: does this sentence exist only to tell the reader what another sentence meant? If yes → delete it (don't reword it — deletion is what flipped the verdict in testing)
- [ ] **Wordbank sweep (constraint 15):** zero tier-1 hits; ≤1 soft framing adverb total (quietly/genuinely/truly/honestly/literally/actually/basically), and never as a sentence-final feeling-tag

Any unchecked box → not done. Deliver only when 14/14. A text that satisfies the checklist but is *well-written* still fails — this is the one rewrite where your instinct to write well is the enemy. Write worse: dwell, repeat, fixate, leave arguments unmade.

### Detector-mode output format

1. The rewritten text, ready to copy.
2. A flag list of **invented specifics** (names, prices, dates you fabricated as scaffolding) — the user must swap in real ones before publishing as their own experience. Never silently present invented facts as real.
3. One line: "Re-test this on the detector — recipes ride the classifier's boundary and detectors retrain." If browser access to the user's detector account is available in the session, offer to test it for them.

### Hard truths to state when relevant (don't oversell)

- No output is promised detector-proof; the fracture recipe passed 2/2 but detectors update.
- Character-level hacks (zero-width chars, homoglyphs) move scores but corrupt text — never apply them; mention only if asked how evasion tools work, with the downsides.
- A discourse-fractured white paper is absurd to a human reader — if the register can't carry a live-rant voice, detector mode needs the interleave protocol (human draft) instead, or the goal isn't reachable.

---

## READER MODE

Target: no *human reader* pegs it as machine-written. This is classic de-AI-ing — it will NOT pass statistical detectors (say so if the user might care). Work top-down: structure → sentences → words.

**Audit first.** Score the text against `references/tells.md` (read it this session) and `references/wordbank.md`: lexicon hits, constructions (negative parallelism, rule of three, rhetorical Q&A, copula dodges, participial tails, hedge stacks, vague authority), punctuation (count em dashes), structure (uniform sentence lengths, fractal summaries, signposted conclusions), voice (no specifics, no stance). Keep the tally as your before-score.

**Rewrite, in order:**
1. **Re-architect.** Delete throat-clearing openers and recap conclusions — start where the point starts, end on a specific (image, number, flat statement). Break the intro→3-points→conclusion mold. Cut restatement: AI says everything 1.5 times; say it once where it lands hardest.
2. **Restore burstiness.** At least one ≤6-word sentence and one 25+-word sentence; visibly uneven paragraphs; a fragment where it punches; start a sentence with And/But/Because. Max one or two single-sentence paragraphs — a drumbeat of them is the AI pattern.
3. **Kill constructions on sight** (full list + fixes in `references/tells.md`): "It's not X — it's Y" → say Y. Rule-of-three → best item only, or four with one oddly specific. "serves as" → "is". Hedge stacks → commit. "Experts say" → name the source or cut.
4. **Swap the lexicon** per `references/wordbank.md` — the plain spoken word ("use" not "leverage"), or better, a concrete noun from the text's own world.
5. **Ground it in the real.** The strongest single move: numbers, days, names, brands, prices, places, one irrelevant-but-true detail. If the draft has no real specifics, use plausible placeholders and flag them — never silently invent facts presented as real.
6. **Punctuation & format.** Em dashes ≤1. Strip emoji bullets, bold-first bullets, title-case headings. Contractions on.
7. **Humanity pass, calibrated to register:** a purposeful comma splice, a parenthetical with attitude, an admission, dry humor, a mild unhedged opinion. Micro-imperfections: at most one or two, casual registers only, never in headlines or names. (This 1–2 cap applies in READER mode only — DETECTOR mode's fracture deliberately exceeds it.)

**Verify.** Re-run the audit: 0 tier-1 lexicon hits, em dashes ≤1, no signposted conclusion, burstiness real, picture visible in the first three sentences, every sentence sayable aloud. Then the over-scrubbing check: zero tells + zero personality is still AI-shaped — the goal is a voice, not an absence.

**Output:** the rewrite first, then a 3–6 bullet change log (tell-count before → after, biggest structural changes, placeholders to replace). No inline annotations, no permission-asking.

---

## Appendix — evidence base (Pangram v3.3.2, tested 2026-07-22)

- **11 pure-AI styled rewrites, all 100% AI High Confidence:** punchy/clipped (77w), rambling prose (204w), lowercase texting-mess (124w), notes-with-file-paths (101w), literary/witty (235w), keyboard-noise typos (196w), 63-word minimum, reader-mode skill rewrite with concrete specifics, ESL circling rant, idiolect mimicry of the owner's hand-written text, and more. No gradient — always 100%/High. Style, length, typos, register: not the lever.
- **Discourse fracture: 4/4 passes with the full constraint set** (French-startup deal story 155w; croissant rant 119w; standing-desk rant 144w; open-plan rant 141w) — all 100% Human, High Confidence. **Two controlled A/Bs (2026-07-22) from fresh-session failures:** (1) a 251-word rant with all 8 base ingredients but *crafted* — punchline fragments, staged progression, rhetorical pivot, moral sentence, callback — scored 100% AI; the same story flattened passed → constraints 9–11. (2) a 141-word rant that converted EVERY point of the source essay into a story beat and introduced a new named specific nearly every sentence scored 100% AI; the same incident with coverage dropped to 2 points and ~4 recurring entities passed → constraints 12–13. Fresh sessions following the recipe went 0/2 before these constraints existed; both failures taught the recipe something.
- **Interleave battery on a genuine human draft:** ~13% AI interleaved → Human; ~35% → Human; ~45% strictly alternating → Human; ~70% with one contiguous AI block → 100% AI. Levers: ratio ≤ ~40% and no 2+ consecutive AI sentences.
- **Controls:** owner's genuine 2008 blog and hand-written rant → 100% Human.
- **Significance-marker isolation test (2026-07-22, field):** a substack/instagram-ads rant satisfying the full 13-point recipe scored 100% AI. The owner deleted exactly two sentences — "thats the part that got me" and "thats the uncomfortable part honestly" — changed nothing else → 100% Human. The cleanest single-variable result in the evidence base: significance-marking meta commentary is, by itself, sufficient for an AI verdict → constraints 14–15.

Interpretation: the classifier keys on planned-text generation dynamics and dominant authorship, not surface style. These thresholds are a snapshot, not a contract — re-test everything.
