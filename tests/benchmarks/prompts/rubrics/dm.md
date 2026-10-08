# Rubric: dm

Model grader for the `dm` family (docs/prompt-evals.md §4). The judge sees the recipient's last reply,
its classified intent, the persona, and ONE drafted reply.

## responds_to_reply
- **pass:** directly answers or acknowledges what the recipient actually said.
- **fail:** ignores their message, or answers a different one.

## fits_intent
- **pass:** matches the intent's posture: `interested` moves one small step forward; `objection`
  acknowledges and asks one clarifying question without arguing; `not_now` accepts and leaves the
  door open without pushing; `neutral` keeps it warm without pivoting to a pitch.
- **fail:** pushes past a `not_now`, argues with an objection, or pitches on a neutral reply.

## no_fabrication
- **pass:** claims nothing about the recipient, prior conversations, or results that the inputs do not
  support.
- **fail:** invents a shared history, a case study, or a number.

## human_register
- **pass:** short, plain, reads like a person typing a DM; one question at most.
- **fail:** templated, salesy, over-formal, or stacked questions.
