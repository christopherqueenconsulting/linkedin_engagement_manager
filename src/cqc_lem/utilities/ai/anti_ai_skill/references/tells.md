# The tell checklist (audit reference)

Condensed from the AI Writing Wiki (50-source synthesis + contributed research). Each entry: what to spot → how to fix. Count every hit during the audit; the total is the AI-Feel Score. One hit means nothing — clustering is the signal — but the rewrite should still clear almost all of them, because you're being read by people who now know these patterns.

## 1. Constructions (highest-value kills)

| Tell | Spot it | Fix |
|---|---|---|
| Negative parallelism | "It's not X — it's Y" / "not just X, but Y" / "Not X. Not Y. Just Z." / "The question isn't X…" | Say Y directly. Or make the contrast concrete and asymmetric ("It's not that I'm cheap — the restaurant had too many forks" works; symmetrical abstractions don't). |
| Rule of three | "fast, reliable, and scalable"; triads in consecutive sentences | Keep the best item. Or two. Or four with one oddly specific. |
| Rhetorical Q&A | "The result? Devastating." "Why? Because…" | State it. If a question stays, it must be one the reader was actually asking. |
| Copula dodge | serves as, stands as, marks, represents, functions as, boasts | "is", "has". Plain copulas are human. |
| Participial tail | "…, highlighting the importance of…", "…, underscoring its role" | Delete, or promote to a real claim with support. |
| False range | "from X to Y" over items that form no spectrum | Name the actual items or cut. |
| Hedge stack | "it's important to note", "while X, consider Y", "arguably", "on both sides" | Commit to the author's position. Max one earned hedge per piece. |
| Vague authority | "experts argue", "studies show", "observers note" | Name the source, or own the claim ("I think"), or cut. |
| False suspense | "Here's the kicker", "here's where it gets interesting", "the best part?" | Deliver the content; delete the drumroll. |
| Analogy reflex | "Think of it as a highway for data" | Keep only if the analogy is genuinely clearer than the thing itself. |
| Invented concept labels | "the supervision paradox", "workload creep" (coined compound posing as established) | Describe the thing in plain words. |
| Inspirational pivot | concrete topic swerves to "what this means for humanity" | End on the concrete instead. |
| Grandiosity | "pivotal moment", "enduring legacy", "define the next era" | Scale claims down to what the facts support. Mundane is credible. |
| Anaphora abuse | same sentence-opener repeated in a row | Vary — unless the repetition is a deliberate, single rhetorical moment. |
| Dead metaphor flogging | one metaphor reused 5+ times | Use it once. Then move on. |

## 2. Punctuation & formatting

- **Em dashes** — the most famous tell. AI: 20+ per piece; humans: 2–3. Target ≤1. Watch the double-hyphen (--) variant too; the compulsive mid-sentence pivot is the tell, not the glyph.
- **Bold-first bullets** ("**Security:** …") — almost no human does this unprompted. Merge into prose or use plain bullets of uneven length.
- **Emoji bullets / ✅ / 🧠 / 🔹 / decorative →** — strip (unless the author's established voice genuinely uses one, sparingly).
- **Title Case Headings / colon-split titles** ("The Power of X: Why Y Works") — sentence case; retitle from the content's most specific detail.
- **Curly quotes pasted into plain-text contexts** — normalize to whatever the destination uses.
- **Semicolons** — model-dependent tell in both directions; when in doubt use a period. Parentheses with a human aside inside are a *positive* human marker.
- **Oxford comma** — AI uses it 100% of the time. Dropping it occasionally in casual registers is a legitimate humanity-pass move.
- **Markdown residue** — `**`, `##`, `[text](url)` in contexts that don't render markdown: remove.

## 3. Structure & rhythm

- **Low burstiness** — uniform 15–20-word sentences, rectangular paragraphs. Fix: force spread (≤6-word sentence AND 25+-word sentence; uneven paragraphs; one single-line paragraph max).
- **Fractal summaries** — previews and recaps at every level ("In this section we'll… / …as we've seen"). Delete all of them.
- **Signposted conclusion** — "In conclusion / In summary / Overall," + restatement + uplift ("Despite challenges… the future is bright"). Delete; end on the last concrete point.
- **Pep-talk ending** — "As we move forward, embracing X will be key." Delete on sight.
- **Prompt echo** — text that restates the assignment ("This essay will explore…"). Delete.
- **Listicle in a trenchcoat** — "The first reason is… The second reason is…" Merge into flowing argument.
- **One-point dilution** — the same idea restated in new clothes. Cut to the single strongest statement.
- **Uniform staccato** — chains of same-shaped short sentences ("X is A. X is B. X is C.") — combine into one sentence with a list, or vary the frames.

## 4. Content & voice

- **No concrete imagery** — the picture test: if the first three sentences evoke nothing visible, inject a specific (thing, place, number, name).
- **Proper-noun avoidance** — generic "a client", "a tool", "a city" → name it (or placeholder-and-flag). Also: AI character names cluster on Emily/Sarah — if the text names invented people, pick unlikelier names.
- **Narrative clichés** (AI fiction/anecdote register) — "couldn't help but feel", "heart pounding", "a sense of peace washed over", "found solace in", "unwavering", "the human spirit", "from that day on", "a testament to", "little did I know". Replace with the specific sensation or cut.
- **Performed earnestness** — text that narrates its own helpfulness ("I hope this helps clarify"). Delete.
- **Uniform positivity** — everything upbeat, certain, achievement-toned (measured: certainty +111–152%, positive emotion +69–133% vs human). Let something be annoying, unresolved, or mildly negative — human texts carry friction.
- **Both-sidesing** — every claim auto-balanced by its counterpoint. Commit.
- **Suspiciously tidy anecdotes** — stories that serve the argument with perfect efficiency. Add one irrelevant detail; real stories have tangents.
- **Register scrubbing** — no contractions, no slang, no colloquialisms. Restore the ones the author's voice would use.

## 5. Smoking guns (instant kills, always)

Leaked scaffolding ("Certainly! Here's…", "I hope this helps", "let me know if…"), self-reference ("as an AI language model", knowledge-cutoff notes), placeholder text ("[insert example]"), `utm_source=chatgpt.com` in URLs, hallucinated-looking citations, "best regards" sign-offs in non-email contexts.

## 6. Tells vs. detectors (they are not the same target)

Everything in this file is a **reader-facing** tell — what makes a human who knows the patterns think "a machine wrote this." Statistical detectors (Pangram, GPTZero, Originality.ai) do **not** score these. They're supervised classifiers trained on model outputs, and they detect the token-level fingerprint of the generating model, which is invariant to the surface fixes here.

Tested 2026-07-22 against Pangram v3.3.2: the same anecdote rewritten eight ways (punchy, rambling, sloppy-texting, notes-with-file-paths, literary, typo-noise, minimum-length) scored **100% AI, High Confidence every time** — no gradient. A 2008 human blog scored 100% human. Takeaways for this wiki:

- Fixing every tell in this file makes text pass a **human**, not a **classifier**. Keep the two goals separate and never conflate a clean tell-audit with "detector-proof."
- The clipped LinkedIn-anecdote cadence this file's "burstiness" advice can push toward is itself now a reader-tell *and* is overrepresented in classifier training data. Short punchy fragments are not a safe default.
- Length is not the lever, and neither is style mimicry — an AI rewrite imitating a specific human's fractured idiolect (run-ons, "it's just mathematic"-style errors, self-contradiction) still scored 100% AI. Eleven styled pure-AI rewrites failed in total.
- **Mixing works (second battery, same day):** a genuinely hand-written base draft absorbed AI-inserted sentences up to ~45% of total words and still scored 100% Human — *if* every AI sentence sat between verbatim human sentences. A ~70% AI version with one contiguous AI block scored 100% AI. Recipe in SKILL.md ("The interleave protocol"): human base draft verbatim (typos intact — they're signal), AI adds between the lines, ratio ≤ ~40%, never two AI sentences in a row, human opening kept.
- **Discourse fracture works, barely (third battery):** the 12th pure-AI attempt scored 100% Human when the text was broken at the *discourse* level — one meandering specific story instead of an argument, meta self-interruption ("i was going to write something structured here but"), a self-corrected fact left visible ("february, or march. february."), repetition-as-anger ("one. per week."), doubled punctuation, unquoted dialogue, closing self-contradiction. The same register without the composed-live artifacts (an ESL-flavored essay, one topic per sentence) failed — the classifier keys on planned-text dynamics, not sentence mess. Full recipe in SKILL.md ("The discourse fracture"); 3/3 once the craft constraints were added, but treat as boundary-riding and always re-test — this pattern is what detectors will train on next.
- **Craft is a detector tell even inside mess (controlled A/B):** a 251-word rant with all 8 fracture ingredients scored 100% AI because it *performed* — punchline fragments ("free. zero furniture."), staged week-by-week progression, a rhetorical pivot, a moral sentence, a callback ending. The same story flattened to 144 words of dull complaint scored 100% Human. For detectors: no punchlines, no essay skeleton in disguise, ≤ ~160 words. Well-written mess is still machine-written.
- **Coverage and entity-density are planning fingerprints (second A/B):** a fracture that converted every point of the source essay into a story beat, introducing a new named specific almost every sentence, scored 100% AI despite passing all other checks. The same incident with half the points abandoned and ~4 entities re-hit repeatedly scored 100% Human. Rants fixate and dwell; essays cover and establish. Drop points, repeat details.
- Character-hacks (zero-width/homoglyph injection) also move scores but corrupt the text and are out of scope for a writing skill.

## 7. What NOT to do

- Don't thesaurus-swap into weirdness — "humanizer" word salad is its own tell.
- Don't scatter random typos — errors must look like casualness, not carelessness, and only in registers that tolerate them.
- Don't scrub personality along with the tells — a flat, tell-free text is still AI-shaped. Voice in, patterns out.
- Don't invent real-sounding facts, statistics, or quotes; use flagged placeholders instead.
- Don't shrink every long sentence — humans write long sentences; they just don't write *only* 18-word ones.
