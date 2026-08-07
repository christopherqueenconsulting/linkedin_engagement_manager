"""Everything LEM says to a model: the proxy client, the prompt-authoring helpers, and the quality gates.

`client.py` holds the ONE client (`AttributedOpenAI`) — every LLM call in the codebase goes through
it, which is what makes attribution, traces and cost tracking complete rather than best-effort.
Content is drawn from the shared core (`content_framework` / `content_research` /
`content_alignment`, plus `story_bank`, `slop_lint`, `image_brief`) by newsletters, posts AND
comments alike; a parallel per-content-type prompt helper is the drift this package exists to
prevent (`docs/content-core.md`).
"""
