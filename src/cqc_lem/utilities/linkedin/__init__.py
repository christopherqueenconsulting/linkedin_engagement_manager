"""Everything that touches LinkedIn itself — Selenium surfaces, the REST/OAuth client, and the rails
that keep both inside safe limits.

Deliberately empty of re-exports: modules here are imported by their own path, often lazily, because
several pull in Selenium or the network at import time and the API process must not pay for that.

Two invariants hold across the whole package, and both exist because of incidents:

* **Every session is bounded by `rate_limit.py`**, which owns the 429 breaker and the shared Redis
  handle. Pacing (`utilities/human_pacing.py`) only slows us down; the breaker is the hard gate.
* **A click is not an outcome.** LinkedIn's SDUI gives no confirmation, so success is the OUTCOME
  being present afterwards, and a control whose label names a different person than the target is
  never clicked (issue #1012 sent ~20 invites to strangers). `docs/sdui-selenium-notes.md`.
"""
