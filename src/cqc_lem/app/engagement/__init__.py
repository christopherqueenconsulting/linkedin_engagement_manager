"""Engagement application services, split out of `app/run_automation.py` (#1154).

One module per bounded context. A module here owns its own Selenium/DB shell and imports nothing
from `run_automation`, so the dependency runs one way and the god module shrinks as contexts leave.

The import below is load-bearing for the same reason as the one in `cqc_lem/app/__init__.py`:
`autodiscover_tasks` never finds these, so importing the module IS the registration. Removing it as
an "unused import" would leave the worker rejecting every invite message as unknown.
"""
from . import invites  # noqa: F401
