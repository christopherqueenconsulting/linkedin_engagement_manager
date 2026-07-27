# Deck reference-value gate — worked example (issue #728)

The acceptance evidence for the save-worthiness gate: the same document post before and after the
gate rejected it, rendered through the unchanged `create_carousel_slide_images` pipeline.

Both decks were graded with `content_framework.deck_reference_report(deck, caption,
save_targeted=True)` against the caption **“Here is the exact stack behind it, end to end.”**

## Before — `before/` (5 slides, REJECTED)

The deck that actually shipped on 2026-07-27. Every body slide restates a claim; the caption
promises a stack the slides never show.

| Slide | Text | Artifacts found |
|---|---|---|
| Cover | The 160-Release Build Receipt | `metric` |
| 2 | Release count is not the goal — shipping often is a side effect of small reversible changes | — |
| 3 | You do not need Kubernetes — a single box carried the whole thing | — |
| 4 | Automation compounds — every manual step removed paid for itself | — |
| CTA | Save this | — |

```
passes=False
empty_slides=['Release count is not the goal', 'You do not need Kubernetes', 'Automation compounds']
unfulfilled=['the exact stack, commands or config']
```

## After — `after/` (6 slides, ACCEPTED)

Every body slide carries something a reader can act from with the post text deleted, and the
caption's promise is fulfilled by the slides.

| Slide | Text | Artifacts found |
|---|---|---|
| Cover | The 5 checks I run before every release | `metric` |
| 2 | 1. Pin the image tag — set IMAGE_TAG to the release tag, never latest | `step`, `config` |
| 3 | 2. Migrate first — run flyway migrate before the app flips; if a migration fails, roll back | `step`, `command`, `decision_rule` |
| 4 | 3. Health check under 2 seconds — or the deploy aborts | `step`, `threshold`, `metric` |
| 5 | What changed — deploys went from 14 minutes of downtime to 0 | `comparison`, `metric` |
| CTA | Save this | — |

```
passes=True
empty_slides=[]
unfulfilled=[]
```

The slide images have no photo band here because they were rendered offline (Pexels unreachable), so
they fall back to the text-only layout. The rendering path itself is untouched by this change — what
this issue changes is the TEXT that lands on the slides.
