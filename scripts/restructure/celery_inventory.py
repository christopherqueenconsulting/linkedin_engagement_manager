#!/usr/bin/env python3
"""Dump every registered Celery task name, so splitting `run_automation.py` can be proved safe (#1154).

Celery derives a task's name from `<module>.<function>` unless the decorator pins one. Moving a task
to a new module therefore RENAMES it, and a rename is silent: `celeryconfig.task_routes` keys and the
beat schedule's `task` strings are plain strings that simply stop matching. The task keeps working
when called directly and never runs on the beat again.

`test_my_celery.py` already asserts every beat entry resolves against `app.tasks`. This covers the
other half — routes, and any task named by neither — by diffing the whole registry across the move:
run on the merge base, run on the branch, diff must be empty.

Deleted with the rest of the restructure tooling once the split is done.
"""

from __future__ import annotations

import argparse
import json
import sys


def inventory() -> dict:
    from cqc_lem.app.my_celery import app

    # Importing the app is not enough: a task only registers when its module is imported. Celery's
    # own autodiscovery runs lazily, so force it before reading the registry or the diff is between
    # two differently-populated snapshots rather than between two layouts.
    app.loader.import_default_modules()

    registered = sorted(name for name in app.tasks if not name.startswith("celery."))
    beat = {
        key: entry.get("task") if isinstance(entry, dict) else getattr(entry, "task", None)
        for key, entry in (app.conf.beat_schedule or {}).items()
    }
    routes = dict(app.conf.task_routes or {})

    named_by_config = {t for t in beat.values() if t} | set(routes)
    return {
        "registered": registered,
        "beat": dict(sorted(beat.items())),
        "task_routes": {k: str(v) for k, v in sorted(routes.items())},
        # The failure this exists to catch: a string in the config that no longer names a task.
        "unresolved_config_names": sorted(named_by_config - set(registered)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="-", help="write JSON here (default stdout)")
    args = parser.parse_args()

    data = inventory()
    text = json.dumps(data, indent=2, sort_keys=True)
    if args.out == "-":
        print(text)
    else:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")

    print(
        f"{len(data['registered'])} task(s), {len(data['beat'])} beat entries, "
        f"{len(data['task_routes'])} routes, "
        f"{len(data['unresolved_config_names'])} unresolved",
        file=sys.stderr,
    )
    return 1 if data["unresolved_config_names"] else 0


if __name__ == "__main__":
    sys.exit(main())
