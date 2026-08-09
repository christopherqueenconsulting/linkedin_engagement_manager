"""Engagement application services, split out of `app/run_automation.py` (#1154).

One module per bounded context. A module here owns its own Selenium/DB shell and imports nothing
from `run_automation`, so the dependency runs one way and the god module shrinks as contexts leave.

The import below is load-bearing for the same reason as the one in `cqc_lem/app/__init__.py`:
`autodiscover_tasks` never finds these, so importing the module IS the registration. Removing it as
an "unused import" would leave the worker rejecting those messages as unknown.

**All five are listed, not just the two that used to be.** Until step 5 the later clusters were
registered as a side effect of `run_automation` importing them — but `run_automation` is now a
re-export shim with no other reason to exist, so that was the whole registration of `feed`,
`posting` and `outreach` resting on a file someone will eventually want to delete. Listing them here
makes this line the registration, which is what the module docstring already claimed.
"""
from . import feed, invites, newsletter, outreach, posting  # noqa: F401
