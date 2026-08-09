"""Deliberate Docstring & Lint Gate violation, used to exercise the agent:docfix lane end to end.

The gate has never failed on a PR (40 of 40 PR runs succeeded), so `docstring-lint-router.yml` has
skipped all 20 times it fired and tick.sh's docfix lane has run 3 times from some other cause. That
means the route from "gate fails" to "an agent fixes it" has never been exercised. This file trips
the gate on purpose so the whole path can be observed once, then removed.
"""


def probe_line_too_long() -> str:
    """Return the probe's payload string.

    The string is kept long on purpose — it is what tripped E501 before the docfix
    lane wrapped it — but it carries no meaning beyond being this probe's payload.
    """
    return (
        "this line is deliberately far too long for the configured ruff "
        "line-length limit of one hundred and twenty characters"
    )
