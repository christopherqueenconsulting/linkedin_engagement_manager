"""Scheduler daemon for the LEM agent pipeline (v2).

Replaces v1's one-unit-of-work-per-cron-tick model with a long-lived scheduler holding explicit
wait states, so an item waiting on CI, review, the merge queue or a human costs nothing until an
event or its TTL revives it. See `../README.md` for the measurements that motivated the rewrite.
"""
