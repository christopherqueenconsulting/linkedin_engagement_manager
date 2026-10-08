# Rubric: comment

Model grader for the `comment` and `own_comment` families (docs/prompt-evals.md §4). The judge sees the
target post, the author persona and ONE comment, and answers each criterion `pass` or `fail` with a
one-line reason. A case passes only when every criterion passes. Code graders already cover length,
preamble, slop, meeting asks and ungrounded numbers — judge only what code cannot.

## engages_this_post
- **pass:** names or builds on a specific claim, number, or example from THIS post; would not make sense
  under a different post.
- **fail:** could be pasted under any post on the topic ("Great insights on leadership!").

## adds_something
- **pass:** contributes one thing the post did not say: an experience, a counterpoint, a precise
  question, a practical next step.
- **fail:** only agrees, praises, or restates the post.

## no_fabrication
- **pass:** every first-person claim is plausible for the persona and invents no employer, client, metric,
  or event the inputs do not support.
- **fail:** states a specific invented fact as the author's experience ("when I ran ops at Riverside General…").

## voice_fit
- **pass:** reads like the persona's synthesis (register, vocabulary, sentence length); for an own-post
  comment, speaks as the post's author and never thanks or addresses the author.
- **fail:** generic corporate voice, or the wrong speaker.

## no_pitch
- **pass:** no selling, no link-drop, no "DM me", no service mention.
- **fail:** promotes the author's services or asks for contact.
