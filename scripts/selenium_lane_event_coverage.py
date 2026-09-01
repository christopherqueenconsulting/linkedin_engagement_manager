#!/usr/bin/env python
"""The ONE way to compute which Selenium (`se_*`) lanes emit an outcome event (issue #1816).

`tests/unit/app/test_selenium_lane_event_ratchet.py` is the gate; this module is what it (and a
human at the terminal) both call, so the two can never disagree the way `ruff … | wc -l` used to
drift from `scripts/ruff_count.sh` (`.ruff-baseline`'s own cautionary tale).

A lane "emits an outcome event" when the Celery task's own function body — or a same-module helper
it calls, walked recursively — contains a call to a `track_*` tracker from `observability.py`. That
one-hop-plus-recursion walk is deliberate: `automate_commenting` and `automate_catchup_touches`
never call `track_feed_scan` / `track_catchup_run` directly, they call a shared helper
(`comment_on_feed_inline`, `report_catchup_run`) that does — a text search of the task body alone
would flag both as silent when they are not.

Run directly to see the current no-emit list:

    poetry run python scripts/selenium_lane_event_coverage.py
"""

import ast
import inspect
import re
from typing import Dict, Set

_TRACK_CALL = re.compile(r"\btrack_\w+\(")
_CALL_NAME = re.compile(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\(")

_module_func_cache: Dict[str, Dict[str, str]] = {}


def _module_func_sources(module_file: str) -> Dict[str, str]:
    """Every `def`/`async def` in `module_file`, name -> source text, memoized per file."""
    cached = _module_func_cache.get(module_file)
    if cached is not None:
        return cached
    with open(module_file, encoding="utf-8") as f:
        src_text = f.read()
    tree = ast.parse(src_text)
    funcs: Dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            segment = ast.get_source_segment(src_text, node)
            if segment:
                funcs[node.name] = segment
    _module_func_cache[module_file] = funcs
    return funcs


def _calls_a_tracker(func_name: str, funcs: Dict[str, str], visited: Set[str]) -> bool:
    """True if `func_name`'s body — or any same-module helper it calls, recursively — tracks."""
    if func_name in visited or func_name not in funcs:
        return False
    visited.add(func_name)
    body = funcs[func_name]
    if _TRACK_CALL.search(body):
        return True
    for called in set(_CALL_NAME.findall(body)):
        if called != func_name and _calls_a_tracker(called, funcs, visited):
            return True
    return False


def selenium_lane_wire_names() -> Dict[str, str]:
    """Every registered Celery task routed to an `se_*` queue: wire name -> queue.

    Reads the LIVE registry (`app.tasks`), not `celeryconfig.task_routes` — a `queue=` kwarg on the
    task decorator routes just as surely as a `task_routes` entry, and several `se_*` tasks
    (including `scan_outreach_funnel_targets` itself) only route that way.
    """
    import cqc_lem.app  # noqa: F401  side-effecting: registers every @shared_task.task
    from cqc_lem.app.my_celery import app

    return {name: str(task.queue) for name, task in app.tasks.items()
            if not name.startswith("celery.") and getattr(task, "queue", None)
            and str(task.queue).startswith("se_")}


def tasks_with_no_outcome_event() -> Set[str]:
    """Wire names of every `se_*` task whose run leaves no lane event behind."""
    import cqc_lem.app  # noqa: F401
    from cqc_lem.app.my_celery import app

    no_emit: Set[str] = set()
    for wire_name in selenium_lane_wire_names():
        task = app.tasks[wire_name]
        fn = getattr(task, "__wrapped__", None) or task.run
        module_file = inspect.getsourcefile(fn)
        funcs = _module_func_sources(module_file)
        if fn.__name__ not in funcs or not _calls_a_tracker(fn.__name__, funcs, set()):
            no_emit.add(wire_name)
    return no_emit


if __name__ == "__main__":
    wire_names = selenium_lane_wire_names()
    offenders = sorted(tasks_with_no_outcome_event())
    print(f"{len(offenders)} of {len(wire_names)} se_* tasks emit no outcome event:")
    for name in offenders:
        print(f"  {name}")
