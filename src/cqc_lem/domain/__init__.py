"""The types LEM's rules are written in terms of: pure data, no I/O.

Nothing here opens a database connection, a browser or a socket. A module in this package may be
imported from any layer, and imports nothing from `platform/` or the application lanes in return —
that one-way direction is what keeps the types cheap to construct in a test.

Issue #1154, Phase 3. The audit warranted exactly three new types (see `models.py`); leaf logic such
as `_score_feed_post` stays a pure function, because converting working tested functions to classes
buys nothing.
"""
