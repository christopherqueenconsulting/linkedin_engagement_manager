"""LEM's HTTP surface: the FastAPI app (`main.py`) and the SPA asset-retention fallback (`spa_assets.py`).

Nothing else in the codebase serves HTTP, so the identity rule has exactly one place to hold:
every `/api` route resolves its caller through `main.get_session_user_id()`, and an `email`,
`user_id` or `post_id` in a request is a TARGET to authorise, never the actor (issue #914).
"""
