"""The user's trained likeness end to end: policy, prompt attributes, LoRA training/inference, previews.

`guardrails` decides whether an avatar may be used at all, `attributes` turns declared traits into
the subject phrase, `replicate_avatar` trains and runs the LoRA, `samples` renders what the user
approves, and `likeness_probe` checks that a generated frame still carries the declared likeness.

Issue #744 (Phase 2 of #548). A synthetic likeness of a real person is the one image LEM can render
that a user cannot take back, so the whole package is arranged around two refusals: `guardrails`
fails CLOSED — anything unresolvable renders with base Flux instead — and `attributes` infers
nothing, writing only what the user declared. `image_gen.py` never reaches in here; the LoRA path
belongs to `ai_helper.generate_post_image` behind `guardrails.resolve_avatar_for`. The video
pipeline adds `likeness_probe` (issue #1279) — telemetry-only by default, hold-gated separately.
"""
