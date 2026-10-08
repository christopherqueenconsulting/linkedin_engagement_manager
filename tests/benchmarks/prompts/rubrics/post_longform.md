# Rubric: post_longform

Model grader for the `post_longform` family (docs/prompt-evals.md §4). The judge sees the persona, the
buyer stage, the content-mix class, the supplied trend/source text, and ONE post.

## grounded_in_inputs
- **pass:** the substance comes from the supplied source/analysis and the persona's stated expertise.
- **fail:** the post's central claim, statistic, or story is not supported by any input.

## stage_fit
- **pass:** awareness names a problem the reader may not know they have; consideration compares
  approaches; decision makes a concrete case or next step.
- **fail:** wrong job for the stage (a hard sell at awareness, vague musing at decision).

## specific_not_generic
- **pass:** at least one concrete, persona-specific detail a different author could not have written.
- **fail:** could be published unchanged by anyone in the industry.

## cta_contract
- **pass:** `value`/`authority` close with a genuine, post-specific question or takeaway; `promo` offers
  the supplied artifact (lead magnet / newsletter) and never asks for a meeting or call.
- **fail:** generic engagement bait ("Thoughts?"), or a meeting ask on any class.
