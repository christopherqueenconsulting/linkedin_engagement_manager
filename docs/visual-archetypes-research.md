# Visual archetypes — the research behind the image engine (issue #2241)

**What this drives.** This is the evidence base for the archetype round of the staged image engine
([`image-stack.md`](image-stack.md) § Visual archetypes). The owner's verdict on the photo-led
covers was "still generic with just random people"; the findings below are why the engine now:

- picks one of seven **archetypes** per image (§6.2) with the deterministic selection order and
  rotation penalty of §6.3 (`image_concept.select_archetype`);
- **draws** five of them — stat card, highlight chart, receipt, before/after, checklist — in code
  from the piece's own verified numbers and steps (`image_graphics`), because §6.1 says code
  draws anything with text or numbers and §5 says only real, sourced numbers;
- demotes the **people photo** to a human moment only, with the gaze turned toward the headline
  panel (§1.2, §4.1);
- builds the AI **editorial concept** with the Idea Miner (§3.3) and rotates text-free art
  styles (§4.2);
- adds the **"piques interest"** rubric (§6.4) to the vision judge, folded into `scroll_stop` and
  `specificity`.
- for POST images, rotates the **treatment** itself (anti-monotony round,
  `post_treatment.py`; `image-stack.md` § Post rhythm).

**How the post rotation implements this.** §6.1's "code draws anything with text or numbers"
becomes two $0 post cards: `data_card` is archetype A (the stat card) unchanged, and `quote_card`
is archetype G *without* the photo — a verbatim pull-quote from the post over the author's byline,
because §6.2 G forbids generated strangers and no approved founder photo exists yet. §2's
"consistency as a system signature" stays with the newsletter cover; posts vary within it — three
brand panel variants (charcoal, off-white, gold; each ≥4.5:1) and three photo grades — so the feed
does not look templated (§6.2 F, "rotate the medium per post"). §6.3's rotation penalty is
generalised into the sameness gate: no treatment, layout, panel, shot or grade more than twice in
a row. The text-free `photo_only` treatment is §6.1 principle 2 taken literally for the share of
posts that do not need a headline card.

Where the engine deliberately departs from a recommendation (the chart threshold, the rotation
penalty, the founder quote card) the departure and its reason are in `image-stack.md`. The report
is kept as written, citations included; `[weak]` marks evidence the author graded weak.

---

## Images That Pop: Research for LinkedIn Covers and Post Images

*Prepared 2026-10-07 for LEM's image engine (newsletter covers and post images for a founder-led AI/automation consultancy for small businesses).*

**Evidence key.** Peer-reviewed or large-sample sources are cited plainly. Vendor blogs, practitioner opinion, and convention-only data (no control group) are marked **[weak]**. Where a claim comes from my own observation of creators' feeds and not from a fetched source, it is labelled *(observation)*.

---

## 0. Diagnosis in one paragraph

The owner says the images are "generic with just random people," and the evidence agrees with him. Nielsen Norman Group's eye-tracking work found that users ignore generic stock photos of people as "filler," while they look closely at photos of real, relevant people and at images that carry information ([NN/g, Photos as Web Content](https://www.nngroup.com/articles/photos-as-web-content/); [UX Myths summary](https://uxmyths.com/post/705397950/myth-ornamental-graphics-improves-the-users-experience) [weak]). A 2025 analysis of 300k+ YouTube outliers found that faces made **no difference on average** and performed **worse in the Business niche** ([Search Engine Journal on 2025 dataset](https://www.searchenginejournal.com/do-faces-help-youtube-thumbnails-heres-what-the-data-says/563944/)). The audience also spots AI-made people and penalises them (Section 4). Taken together: **an AI photo of anonymous people is the weakest image we can make for this audience.** The fix is not better people. The fix is to make the image carry *the idea* or *the evidence* of the post.

---

## 1. Attention science

### 1.1 What wins the first glance (salience)
- **The decision to stop happens in about half a second.** The orienting response fires 100–200 ms after a visual change ([adlibrary, Meta creative guide](https://adlibrary.com/posts/meta-ads-creative-best-practices) [weak]). Meta's own guidance stresses the opening frames ([same](https://adlibrary.com/posts/meta-ad-creative-best-practices) [weak]). In practice, the image must be legible as a **thumbnail at feed size** before anyone reads the headline.
- **One focal point.** MrBeast's leaked production handbook says "the simpler the better" ([handbook PDF](https://cdn.prod.website-files.com/6623bf84e83241ec49b548e4/66edaa19db6e9359bb92931f_How-To-Succeed-At-MrBeast-Production%20(2).pdf); [memo summary](https://www.alexanderjarvis.com/memo-how-to-succeed-in-mrbeast-production/)). Thumbnail analysts describe his frames as having exactly one place the eye lands, high contrast, and minimal large text ([Artiphik](https://artiphik.com/blog/mrbeast-thumbnail-analysis) [weak]; [yougenie](https://blog.yougenie.co/posts/mrbeast-thumbnail-strategy-analysis/) [weak]).
- **High contrast and faces dominate successful thumbnails as a convention.** vidIQ's 2026 study of 500 breakout videos found 69% used a face and 89% used a face *or* high contrast. vidIQ notes there was no control group of flops, so this shows convention, not cause ([vidIQ thumbnail study 2026](https://vidiq.com/research/youtube-thumbnail-study/) [weak, convention only]).
- **Social-feed eye-tracking:** in a 2024 study, 201 participants scrolled a dynamic Facebook-style feed, and the picture and headline were the main attention anchors among post elements ([Mayer, Ohme, Maslowska & Segijn 2024, *Social Media + Society*](https://journals.sagepub.com/doi/10.1177/20563051241245666)). In Instagram-style posts, gaze concentrated on the central visual, branding, and human faces ([MDPI 2025 food-post eye-tracking](https://www.mdpi.com/1995-8692/18/6/69)).

### 1.2 Faces and gaze direction
- Faces attract gaze, but **where the face looks matters more than whether a face is present.** In eye-tracking of magazine ads, a model who looked *at the product* produced longer looks at the product, the brand, and the rest of the ad than a model looking at the viewer ([Sajjacholapunt & Ball 2014, *Frontiers in Psychology*](https://www.frontiersin.org/journals/psychology/articles/10.3389/fpsyg.2014.00210/full); [ResearchGate](https://www.researchgate.net/publication/264702675_The_effect_of_gaze_cues_on_attention_to_print_advertisements)). Gaze cueing in banner ads also shifted product judgments ([Palcu et al., PMC](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC5454066/)).
- **Implication:** if a person appears, they should be *looking at the thing that matters* (the headline panel, the number, the broken object). A person smiling at the camera only attracts attention to themselves.
- Emotional faces (surprise, curiosity) are widely claimed to add 20–30% CTR on YouTube ([thumbnailtest](https://thumbnailtest.com/guides/face-in-youtube-thumbnail/) [weak]; [bananathumbnail](https://blog.bananathumbnail.com/youtube-thumbnail-psychology/) [weak]). Those figures are unsourced vendor claims, and the 300k-video dataset above does not support a universal face effect. **Use faces only when it is the founder's own face (real, consistent, known) or when the face's expression IS the story.**

### 1.3 Curiosity gap (Loewenstein)
- Information-gap theory: curiosity is a feeling of deprivation that arises when attention is drawn to a specific gap in one's knowledge ([Loewenstein 1994 via ResearchGate](https://www.researchgate.net/publication/232440476_The_Psychology_of_Curiosity_A_Review_and_Reinterpretation); [2024 review, *Journal of Documentation*](https://www.diva-portal.org/smash/get/diva2:1845351/FULLTEXT01.pdf)).
- **It is an inverted U.** Curiosity falls when the gap is too large (no foothold) or too small (nothing to learn) ([same review](https://www.diva-portal.org/smash/get/diva2:1845351/FULLTEXT01.pdf)). A 2026 preregistered experiment (n≈500 ×2) showed that *visualising* a moderate knowledge gap (33% known) raised reading, while showing complete knowledge (100%) lowered it ([*Journal of Cognition* 2026, "Knowledge Gap Illustrations Spark Curiosity"](https://journalofcognition.org/articles/10.5334/joc.501)). This is the most directly usable finding: **the image should show part of the answer and visibly withhold the rest** (a chart with one bar hidden, a receipt with the total circled but a line item blurred, a 5-step checklist with step 4 ticked "?").
- Gaps that are also *frustrating* backfire ([Organizational Behavior and Human Decision Processes 2023](https://www.sciencedirect.com/science/article/abs/pii/S0749597823000523)). The payoff must arrive in the post. This is the line between curiosity and clickbait.
- A 2025 integrative account adds that a gap sparks curiosity when it is *appraised* as relevant and resolvable ([Erdemli et al. 2025, *Affective Science*](https://www.unige.ch/fapse/e3lab/files/9217/6375/5818/erdemli_2025_affsci.pdf)). For B2B, relevance means the gap must be about the reader's money, time, or risk.

### 1.4 Novelty, incongruity, visual puns
- **Schema incongruity earns attention.** Incongruent ads drew more visual attention than congruent ones, mainly on *second-pass* viewing. People notice first, then return to resolve the oddity ([Frontiers 2014 / PMC on incongruity](https://www.frontiersin.org/journals/psychology/articles/10.3389/fpsyg.2014.00210/full); [conflict detection in surrealistic images, PMC](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC7272058/)). Perceptual-plus-conceptual incongruity produced more fixations than perceptual alone ([incongruous contextual cues, PMC](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC9807612/)).
- **The cost is fluency.** Incongruity can lower liking unless it resolves quickly into a "got it" ([visual-style eye-tracking, PMC 2025](https://pmc.ncbi.nlm.nih.gov/articles/PMC12452646/)). Design rule: **one oddity, resolvable in under 2 seconds with the headline.** Two oddities are noise.
- Ambient/guerrilla ad research finds the same thing: moderate incongruity drives effectiveness ([ResearchGate](https://www.researchgate.net/publication/270648950_Ambient_advertising_characteristics_and_schema_incongruity_as_drivers_of_advertising_effectiveness)).

---

## 2. What top creators and B2B publishers actually use

### 2.1 LinkedIn platform evidence
- Images make up 57% of LinkedIn posts and earn a 1.33× engagement multiplier. **Portrait 1080×1350 earned 2.83% median engagement vs 2.54% square vs 2.14% landscape (+32%)** across 368k single-image posts ([AuthoredUp, 3M+ posts, Mar 2025–Feb 2026](https://authoredup.com/blog/best-performing-content-on-linkedin)). **Our covers should render a 4:5 variant for feed posts.** Newsletter covers have their own fixed aspect ratio.
- The image types that work, per the same analysis: **screenshots of real results** (dashboards, DMs, before/after metrics), **original data visualisations**, **authentic photos over stock**, and high-contrast designs readable at small size ([AuthoredUp](https://authoredup.com/blog/best-performing-content-on-linkedin) [moderate: practitioner analysis of a large dataset]).
- Image posts drew the most comments (35 avg vs 28 for video) in a 57,809-post dataset ([Linkhub, July 2026](https://linkhub.gg/en/blog/format-post-linkedin-engagement) [weak: no impressions data]). Documents/carousels lead engagement rate at ~7% ([AuthoredUp](https://authoredup.com/blog/best-performing-content-on-linkedin)).

### 2.2 Creators
- **Lara Acosta:** about 46% of her posts are simple image posts, often of herself, with consistent warm palette, fonts, and layouts. She says a text screenshot "doubles your chances of capturing attention" ([magicpost](https://magicpost.in/blog/how-to-write-like-lara-acosta) [weak]; [contentin](https://contentin.io/blog/the-best-linkedin-posts-35-high-performing-posts-templates-from-7-content-masters/) [weak]; [Creator Science podcast](https://podcast.creatorscience.com/lara-acosta/)).
- **Justin Welsh:** minimal, text-forward, with no emojis or hashtags. His images are usually typographic or simple diagram cards repurposed from X ([contentin](https://contentin.io/blog/the-best-linkedin-posts-35-high-performing-posts-templates-from-7-content-masters/) [weak]).
- **Sahil Bloom:** hand-drawn framework visuals, co-created with the illustrator @drex_jpg ([X thread](https://x.com/SahilBloom/status/1456969290364620809?lang=en); [Framework Handbook](https://sahilbloom.substack.com/p/the-framework-handbook)). The hand-drawn look signals "a human thought about this," which works against both the stock look and the AI look ([Medium on sketch style](https://medium.com/oceanize-geeks/sketchy-looks-hand-drawing-style-in-modern-web-ui-design-e090b6d560fa) [weak]).
- **Chris Donnelly:** grew to 1M+ followers on infographics and carousels in a recognisable house template ([Nathan Barry interview](https://nathanbarry.com/personal-branding-masterclass-how-i-made-10m-on-linkedin-chris-donnelly-088/)).
- **Jasmin Alić, Matt Gray, Dan Koe:** *(observation, [weak])* Alić mostly runs text plus a real photo of himself. Gray uses bold numbered "system" cards. Koe uses a dark minimal aesthetic with a single striking image or quote. I fetched no image-level engagement data for these three.
- **Common thread:** none of them uses anonymous-people photography. The image is (a) the creator's real face, (b) the idea drawn out, or (c) proof.

### 2.3 Publications
- **The Economist:** the cover comes out of the Friday editorial meeting. It must distil the lead editorial's argument into one image, using visual metaphor, allegory, or pun, and colour carries meaning (red = danger) ([Trung Phan](https://www.readtrung.com/p/the-economist-cover-curse-explained) [weak]; [Istanbul Chronicle](https://www.theistanbulchronicle.com/post/story-behind-the-economist-s-covers) [weak]; [magCulture interview with Stephen Petch](https://magculture.com/blogs/journal/stephen-petch-the-economist); [Luca D'Urbino covers](https://www.behance.net/gallery/80117861/The-Economist-Covers)).
- **Lenny's Newsletter:** loose lines and warm watercolour, chosen deliberately "to break away from the incredibly neat vector illustrations that dominate tech" ([Natalie Harney case study](https://natalieharney.com/portfolio/lennysbrand)). Here the style itself is the differentiator.
- **Stripe Press:** one crafted object on near-black, small serif type, and a quiet system ([siiimple](https://siiimple.com/stripe-press/) [weak]; [Behind the cover: Scaling People](https://stripepress.substack.com/p/behind-the-cover-scaling-people-by)). This is a model of *restraint as premium*.
- **Morning Brew:** plain two-colour masthead, with personality in copy and not in art ([Audiencers](https://theaudiencers.com/deep-dive-into-the-morning-brew-newsletter-andy-griffiths/) [weak]).

### 2.4 Archetype catalogue

| # | Archetype | When it works | Example | Evidence |
|---|---|---|---|---|
| 1 | **Typographic poster** | Thesis is a contrarian one-liner | Welsh-style card | Convention only [weak]. Text-as-image "doubles attention" per Acosta [weak] |
| 2 | **Big-number stat card** | Article has one surprising number | "$11,400/yr lost to re-typing invoices" | Data-viz screenshots + original data rank high ([AuthoredUp](https://authoredup.com/blog/best-performing-content-on-linkedin)) |
| 3 | **Before/after split** | Transformation, process change | Manual workflow vs automated | "Before/after metrics" named as top performer ([AuthoredUp](https://authoredup.com/blog/best-performing-content-on-linkedin)) |
| 4 | **Annotated screenshot / chat mockup** | Story of a tool, client message, or AI prompt | ChatGPT exchange with one line circled | Screenshots of DMs/results are top performers ([AuthoredUp](https://authoredup.com/blog/best-performing-content-on-linkedin)). **Must be real or clearly illustrative** (Section 6 ethics) |
| 5 | **Hand-drawn framework sketch** | Article proposes a model, 2×2, or loop | Bloom's sketches | Creator convention [weak]. Embellishment aids recall ([Bateman 2010](https://www.semanticscholar.org/paper/Useful-junk:-the-effects-of-visual-embellishment-on-Bateman-Mandryk/5c1008ce672d709d41cbcce36d1e49ec08eb8c03)) |
| 6 | **Meme / reaction** | Shared frustration, light tone | Two-panel "what clients ask / what they need" | No B2B data [weak]. Brand-risk; low priority |
| 7 | **Editorial conceptual (one surreal juxtaposition)** | Abstract thesis (trust, risk, waste) | Economist covers | Incongruity research (§1.4) |
| 8 | **Chart with one highlighted point** | Trend or comparison with one outlier | Gray bars, one gold bar + label | Annotation preferred ([Stokes & Hearst 2022](https://arxiv.org/abs/2209.10789)). Titles drive recall ([Kong et al. CHI'18](https://experts.illinois.edu/en/publications/frames-and-slants-in-titles-of-visualizations-on-controversial-to/)) |
| 9 | **Receipt / invoice / spreadsheet mockup** | Cost, waste, ROI arguments | Receipt "Hidden cost of manual follow-up" | Receipts are a recognisable schema plus a gap (§1.3) [weak, no direct data] |
| 10 | **Quote card with face** | Founder's own strong opinion, or a real client quote | Founder photo + pull-quote | Real faces > stock ([NN/g](https://www.nngroup.com/articles/photos-as-web-content/)) |
| 11 | **Comparison table** | Options/tools/vendors | "Zapier vs Make vs custom" | Save-worthy reference format [weak] |
| 12 | **Checklist** | How-to, audit, readiness | "7 signs you're ready to automate" with 2 unchecked | Partial completion = visible gap ([J. Cognition 2026](https://journalofcognition.org/articles/10.5334/joc.501)) |

---

## 3. Editorial illustration craft: how an idea becomes a cover

### 3.1 What art directors do
1. **Read for the argument, not the topic.** An Economist cover illustrates the *leader's claim*, not its subject ([Trung Phan](https://www.readtrung.com/p/the-economist-cover-curse-explained) [weak]).
2. **Word list → mind map.** Write down the key nouns and verbs. Branch from each into associations: idioms, objects, places, tools, body parts ([Art Prof brainstorming lesson](https://artprof.org/learn/fundamentals/brainstorming/brainstorming-track-lesson-2/)).
3. **Many thumbnails, fast.** Illustrators fill a page of boxes to force quantity, then send the art director 2–3 directions ([Kat J. Weiss process](https://www.katjweiss.com/blog/2023/1/10/developing-concepts-for-a-political-editorial-illustration-my-process); [New Yorker illustration in 7 days, Milanote](https://milanote.com/the-work/creating-an-illustration-for-the-new-yorker); [Delaney Gibbons](https://delaneygibbons.substack.com/p/evolution-of-an-editorial-illustration)).
4. **Apply a transformation operator** to pairs of associations ([Linearity on editorial illustration](https://www.linearity.io/blog/editorial-illustration/) [weak]):
   - **Juxtaposition:** two things that do not belong together (nature + machinery).
   - **Object substitution / fusion:** one object takes another's form (a filing cabinet shaped like a cage).
   - **Scale shift:** an oversized object over tiny figures means pressure, control, imbalance.
   - **Transformation in progress:** a lightbulb melting like an ice cube means fading innovation.
   - **Visual oxymoron:** an object that contradicts its own function (a padlock made of paper, an hourglass with sand flowing up).
   - **Literalised idiom (visual pun):** "drowning in paperwork" shown as a desk at the bottom of a pool. Recent LLM→image→VLM loops generate these iteratively ([Visual Puns from Idioms, arXiv 2511.22943](https://arxiv.org/pdf/2511.22943)).
5. **Check clarity.** "A metaphor should be clever but not confusing" ([Linearity](https://www.linearity.io/blog/editorial-illustration/) [weak]).

### 3.2 Fresh metaphor vs cliché
Clichés are the *first-order association* that every competitor reaches: gears (process), lightbulb (idea), puzzle pieces (fit), robot hand + human hand (AI), handshake (partnership), rocket (growth), chess (strategy), glowing brain (AI). Test:
- **The first-ten test:** if a metaphor shows up in the first ten results of a stock search for the topic, discard it.
- **The specificity test:** a fresh metaphor uses an object *from the article's own world*: the invoice, the voicemail, the sticky note, the clipboard, the delivery van, the dental chair. A cliché uses an object from the *category* (AI → robot).
- **The thesis test:** could the same image illustrate the *opposite* argument? If yes, it is decoration and not an idea.
- Academic work confirms that LLMs can generate visual metaphors when the process is explicitly decomposed: an LLM elaborates the metaphor into a concrete scene, a diffusion model renders it, and humans or a VLM check it. Explicit elaboration produced much better images than raw metaphor prompts ([I Spy a Metaphor, ACL Findings 2023](https://arxiv.org/abs/2305.14724)). A 2025 benchmark finds T2I models still struggle to *blend* two concepts into one object ([Blending Concepts, arXiv](https://arxiv.org/pdf/2609.02502)). **Prefer juxtaposition and scale shift (two objects side by side) over hybrid fusion, which models garble.** A multi-reward framework scores generated metaphors for source/target alignment ([Mind's Eye, arXiv 2508.18569](https://arxiv.org/pdf/2508.18569)). This supports a VLM grader step.

### 3.3 LLM procedure: "Idea Miner" (concrete steps)
1. **Thesis in ≤12 words** ("Most small firms lose a day a week to copy-paste between apps").
2. **Extract 20 concrete nouns** from the article and the reader's daily world. No abstractions. Include tools, documents, rooms, and body parts.
3. **Extract 5 verbs/tensions** (leak, trap, pile up, wait, duplicate).
4. **Generate 12 candidate pairings** by applying operators to noun × tension: juxtaposition, scale shift, oxymoron, literal idiom, before/after, and transformation-in-progress.
5. **Kill clichés** against a banned list (gears, lightbulb, puzzle, robot, brain, rocket, chess, handshake, target/dartboard, magnifying glass, cloud icons, binary code, glowing network).
6. **Score each candidate** with the rubric (§6.3). Keep the top 3.
7. **Elaborate the winner** into a literal scene: subject, setting, *one* oddity, camera, lighting, palette.
8. **Render, then have a VLM ask:** "In one sentence, what is this image arguing?" If the answer does not match the thesis, fall back to the #2 idea or to a code-drawn archetype.

---

## 4. AI image specifics (2025–2026)

### 4.1 Audiences penalise the AI look
- **NielsenIQ neuroscience (2025):** consumers readily identify AI-made ads, respond with negative sentiment, and show *lower memory activation and action intent* ([ANA / NielsenIQ](https://www.ana.net/miccontent/show/id/er-2025-02-ai-feb25-nielseniq)).
- **TBWA × Ideally (2026):** ads perceived as AI-made took trust and intent penalties, and **disclosure made it worse**, especially for banks, airlines, and other high-involvement categories ([Mi3](https://www.mi-3.com.au/03-02-2026/machine-made-ads-for-trigger-big-trust-penalties)).
- **Klaviyo/Datalily (Dec 2025):** 7% say visible AI marketing raises trust and 31% say it lowers it ([eMarketer](https://www.emarketer.com/content/shoppers-aren-t-impressed-by-ai-generated-marketing); [Kate O'Neill](https://www.koinsights.com/the-authenticity-premium-why-consumers-are-rejecting-ai-generated-content/) [weak]). Academic replication: labelled-AI ads lowered trust and purchase intent ([ResearchGate 2025](https://www.researchgate.net/publication/399748854_AI-generated_versus_human-created_advertising_Effects_on_consumer_trust_and_purchase_intent)).
- **Implication for an AI consultant:** the irony cuts both ways. Readers expect the AI expert to use AI, but *generic* AI imagery reads as low effort. The tells are AI people (waxy skin, perfect teeth, gibberish text), glossy blue holograms, and symmetrical "corporate" lighting. **Lean on code-drawn graphics and stylised illustration, where "AI-ness" is invisible or irrelevant, and drop photoreal crowds.** Do not hide AI use. Make the image's *idea* human-authored.

### 4.2 Prompting away from stock
- OpenAI's official gpt-image guide: camera and composition language (lens, aperture, lighting) steers realism better than "8K/ultra-detailed". It recommends naming materials, textures, and the medium, and asking for real texture and an unposed feel ([OpenAI cookbook, image-gen prompting](https://developers.openai.com/cookbook/examples/multimodal/image-gen-models-prompting-guide); [gpt-image-1.5 guide](https://developers.openai.com/cookbook/examples/multimodal/image-gen-1.5-prompting_guide); [image prompting docs](https://developers.openai.com/api/docs/guides/image-prompting)).
- Film and analogue anchors (Portra 400, medium format, grain, light leak) and explicit skin texture reduce the plastic look ([Luma](https://lumalabs.ai/news/realistic-ai-image-prompts) [weak]; [promptaa](https://www.promptaa.com/blog/prompt-key-words-to-make-images-less-fake-looking) [weak]).
- For illustration, name the **movement, medium, and era** ("1970s risograph print", "Bauhaus poster"). The more specific the anchor, the better the model grips it ([Envato](https://elements.envato.com/learn/illustration-prompts) [weak]).
- **Styles that read as designed, not stock** (craft consensus, [weak]):
  - *Risograph:* 2–3 spot inks, misregistration, grain.
  - *Paper collage:* cut-paper edges, halftone photo fragments, visible shadows.
  - *Editorial flat vector:* limited palette, oversized objects, no gradients.
  - *Isometric diorama:* good for workflows.
  - *Claymation / felt miniature:* a tactile scale shift that is hard to read as AI stock.
  - *Single-object still life on seamless colour:* Stripe Press restraint.
  - *Watercolour line:* Lenny's.
- **Photoreal done right:** one object, not people. Hard flash or a single strong light. An unusual angle (overhead flat-lay, worm's-eye, macro). Colour-blocked seamless background in the brand accent.

### 4.3 Negative constraints to always send
No text, letters, logos, or UI glyphs. No people unless specified. No holograms, glowing circuits, or blue-purple tech gradients. No robots. No handshakes. No smiling-at-camera groups. (The existing engine already bans text in render prompts. Keep it.)

---

## 5. Data graphics on social

- **Titles steer the takeaway.** Readers recall the message stated in a chart's title more than other data in the chart ([Kong, Liu, Karahalios, CHI'18](https://experts.illinois.edu/en/publications/frames-and-slants-in-titles-of-visualizations-on-controversial-to/); [follow-up CHI'19](https://dl.acm.org/doi/abs/10.1145/3290605.3300576)). **Write the headline as the finding** ("Invoices take 3× longer by hand"), not as a label ("Invoice processing time").
- **Annotate.** Readers preferred heavily annotated charts over minimal ones when the text adds context. The guideline: "Rather than aiming for maximally minimalist design, annotate charts with relevant text" ([Stokes & Hearst 2022](https://arxiv.org/abs/2209.10789); [MIT Vis "Striking a Balance"](https://vis.mit.edu/pubs/vis-text-balance)).
- **Embellishment aids recall without hurting accuracy.** Nigel Holmes-style charts were recalled better 2–3 weeks later ([Bateman et al., CHI 2010](https://www.semanticscholar.org/paper/Useful-junk:-the-effects-of-visual-embellishment-on-Bateman-Mandryk/5c1008ce672d709d41cbcce36d1e49ec08eb8c03); [eagereyes discussion](https://eagereyes.org/criticism/chart-junk-considered-useful-after-all)). Color and a *recognisable object* raise memorability, and common bar charts are the least memorable ([Borkin et al. 2013](http://olivalab.mit.edu/Papers/Borkin_etal_MemorableVisualization_TVCG2013.pdf)).
- **On LinkedIn**, original data visuals and before/after metrics are among the top image types ([AuthoredUp](https://authoredup.com/blog/best-performing-content-on-linkedin)).
- **Recipe for a single-stat or mini-chart card:**
  1. Headline = the finding.
  2. One hero number at 30–40% of canvas height.
  3. At most 5 bars or one line, all gray except **one gold highlight**.
  4. One direct annotation with an arrow ("← this is you").
  5. Source line in small type (credibility, and it guards against "made-up number" suspicion).
  6. An optional pictogram object (an invoice icon on the bar) for the Borkin/Bateman memorability gain.
  7. 4:5 portrait.
  8. **Only real numbers from the article or a cited source.** Never synthesise a stat. The engine's fact-anchor gate (#2231) must apply here too.

---

## 6. Recommended system for LEM

### 6.1 Principles
1. **The image carries the idea or the evidence, never just decoration.** No anonymous AI people by default.
2. **Code draws anything with text or numbers.** AI renders only text-free concepts.
3. **One focal point, one oddity, one accent colour** (brand gold on charcoal stays as the system signature, as Acosta and Donnelly do with consistency).
4. **Show part, withhold part** (the curiosity gap), and always pay it off in the post.
5. Default to **4:5 portrait** for feed posts.

### 6.2 The seven rotating archetypes

**A. Stat Card (code-drawn).**
- *Trigger:* the article contains a verified number with a unit ($, %, hours, ×) that is surprising relative to a stated baseline.
- *Pop:* a giant number, scale contrast, and a gold accent against neutral.
- *Recipe:* charcoal ground, the number in Montserrat ExtraBold at about 35% height in gold, a one-line finding headline above, a comparison line below ("vs 4 hrs for teams using X"), a source line at 11px, and an optional single line-icon. Gap variant: show the number and phrase the "why" as the open question in the kicker.

**B. Highlight Chart (code-drawn).**
- *Trigger:* ≥3 comparable values or a time series in the article or source.
- *Pop:* one gold bar among gray, plus a direct annotation.
- *Recipe:* matplotlib/SVG, ≤5 bars, no gridlines, headline = finding, arrow + 6-word annotation on the highlighted bar, source line. Optional pictogram on the highlight bar.

**C. Receipt / Spreadsheet (code-drawn).**
- *Trigger:* cost, waste, hidden-time, or ROI theme. Line items can be stated or derived from the article's own figures.
- *Pop:* a familiar schema used for an unfamiliar purpose (an incongruity that resolves at once). The circled total is the hook.
- *Recipe:* thermal-receipt texture (off-white, monospace), the title "THE REAL COST OF {X}", 4–6 line items, a TOTAL circled in hand-drawn gold marker, and a slight rotation plus shadow on charcoal. Every number must trace to the article. Otherwise use qualitative items with no figures.

**D. Before/After Split (code layout + optional AI halves).**
- *Trigger:* the article describes a transformation, a workflow change, or "old way / new way".
- *Pop:* symmetry with one contrast. The eye ping-pongs between halves (a second-pass fixation).
- *Recipe:* a vertical split with code-set labels "BEFORE" / "AFTER" and 3 bullet facts per side. Optional AI halves show the *same object* in two states (cluttered desk with paper towers vs the same desk cleared, overhead flat-lay, identical framing, risograph style). The same framing is essential, and so is no people.

**E. Checklist / Comparison Table (code-drawn).**
- *Trigger:* listicle, how-to, readiness audit (→ checklist), or ≥2 options compared on ≥3 criteria (→ table).
- *Pop:* save-worthy reference value plus a visible gap.
- *Recipe:* checklist of 5–7 items with 2 checked, 1 marked with a gold "?" and the rest blank, headline "Score yourself: …". The table is ≤3 columns × ≤5 rows, with the winning cell highlighted in gold and the rest gray. Hand-drawn-style checkmarks (SVG path jitter) for a Bloom-like human touch.

**F. Editorial Concept (AI-rendered + code-set headline panel; the current layout, re-pointed).**
- *Trigger:* an abstract thesis with no usable numbers (trust, risk, mindset, a contrarian claim). This is the default fallback for opinion pieces.
- *Pop:* a single juxtaposition, scale shift, or oxymoron from the Idea Miner (§3.3), using an object from the reader's world.
- *Recipe (prompt skeleton):* `"{medium: 1970s risograph print in two spot inks, charcoal and mustard gold, visible grain and slight misregistration | cut-paper collage with halftone fragments | tactile claymation miniature}. A single {concrete object} {operator: towering over / made of / sinking into / leaking} {second object}, centred on a flat {colour} background, generous negative space on the {left/right} for a headline panel. Overhead / low-angle. No people, no text, no letters, no logos, no robots, no gears, no lightbulbs, no glowing circuits."` Rotate the medium per post so the feed does not look templated. Example: "Your CRM is a leaky bucket" becomes a galvanised bucket shaped like a Rolodex, with business cards spilling out of holes, overhead, risograph.

**G. Founder Quote Card (real photo + code type).**
- *Trigger:* the post is first-person opinion or story, *and* an approved real founder photo exists (avatar/LoRA path behind the existing guardrails).
- *Pop:* a real, familiar face (NN/g) with gaze directed *toward the quote* (gaze cueing), plus a strong pull-quote.
- *Recipe:* founder cut-out on one side, looking toward the text. A 1–2 line quote ≤18 words in the gold display face, with name/byline. Never use generated strangers. If no approved likeness exists, fall back to A or F.

*Deliberately excluded:* meme/reaction (brand risk, and no B2B evidence), and fabricated chat or screenshot mockups (they read as fake receipts; use one only when the user supplies the real screenshot).

### 6.3 Selection procedure (LLM, deterministic order)
1. Extract features: `has_verified_number`, `n_comparable_values`, `is_cost_theme`, `is_transformation`, `is_list_or_howto`, `n_options_compared`, `is_first_person_opinion`, `founder_photo_available`, `abstract_thesis`.
2. Candidate set: A if number; B if ≥3 values; C if cost theme; D if transformation; E if list/compare; G if opinion + photo; F always.
3. Generate a 1-line concept for each candidate, then score with the rubric.
4. **Rotation penalty:** −2 if the archetype was used in either of the last 2 posts, −1 if the medium in F repeats. This keeps the feed varied without randomness.
5. Pick the highest score. Ties go to code-drawn (cheaper, no AI-look risk, no garbled text).

### 6.4 "Piques interest" rubric (0–2 each, max 14; ship at ≥9)
| Criterion | 0 | 2 |
|---|---|---|
| **Thumbnail read** (legible at 25% size) | Muddy | One focal point instantly clear |
| **Thesis fit** (VLM states the argument in one sentence) | Generic or could argue the opposite | Matches the thesis |
| **Gap** | Shows nothing or everything | Shows part, withholds part, pays off in the post |
| **Freshness** | Banned-list cliché or stock-like | Object from the reader's world, unexpected pairing |
| **Resolvability** | Oddity unexplained after 2 s | "Got it" within 2 s with the headline |
| **Credibility** | Invented number, AI-person tells | Sourced number, real face, or stylised art |
| **Relevance to SMB owner** | Abstract tech | Their money, time, or risk is visible |

Score with the existing `lem-vision` gate. Use the VLM for thumbnail read, thesis fit, and freshness, and code checks for credibility (number provenance). Feed post-publish engagement back per archetype to tune the rotation weights.

---

## Caveats
- Most "faces boost CTR 20–30%" figures are uncited vendor claims. The larger 2025 dataset contradicts them for Business ([SEJ](https://www.searchenginejournal.com/do-faces-help-youtube-thumbnails-heres-what-the-data-says/563944/)).
- I found no controlled study of *LinkedIn image archetypes*. The archetype ranking here is triangulated from LinkedIn format data, creator convention, and lab attention research. Treat it as a prior and A/B test it via the existing experiments framework.
- Several pages (Sage, Mi3, ScienceDirect, vidIQ, magCulture) were paywalled or blocked to fetch. Their claims are cited from search-result abstracts.
