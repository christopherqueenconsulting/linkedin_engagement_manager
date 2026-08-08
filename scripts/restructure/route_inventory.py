#!/usr/bin/env python3
"""Dump the FastAPI route table so a router split can be proved route-for-route identical (#1154).

This is the `api/main.py` analogue of the `--verify` pass that guarded the `db.py` split: run it on
the merge base, run it on the branch, diff. An empty diff is the evidence that moving 175 route
declarations into `api/routers/*.py` changed nothing a client can see.

A green test suite is NOT that evidence. A route that silently stops being registered fails no test
that does not already name it, and the SPA finds it in production instead.

Deleted with the rest of the restructure tooling once the split is done.
"""

from __future__ import annotations

import argparse
import json
import sys


def inventory() -> list[dict]:
    # main's OWN flattener, not a second one written here. FastAPI >=0.139 keeps an included router
    # as one opaque `_IncludedRouter` node instead of copying its routes onto `app.routes`, so a
    # naive walk reports 14 routes out of 175 and every missing one reads as "removed by the split".
    # `_hide_admin_routes_from_schema` already depends on getting this right; sharing it means the
    # inventory cannot disagree with the app about what a route is.
    from cqc_lem.api.main import _walk_routes, app

    rows = []
    for route in _walk_routes(app.routes):
        path = getattr(route, "path", None)
        if path is None:
            continue
        endpoint = getattr(route, "endpoint", None)
        rows.append(
            {
                "methods": sorted(getattr(route, "methods", None) or []),
                "path": path,
                # The handler NAME, not its module: the module is exactly what the split changes,
                # so including it would make every moved route read as a difference.
                "endpoint": getattr(endpoint, "__name__", None),
                "name": getattr(route, "name", None),
                "include_in_schema": getattr(route, "include_in_schema", None),
                # Response model and status code are part of the contract a client sees.
                "status_code": getattr(route, "status_code", None),
                "response_model": getattr(
                    getattr(route, "response_model", None), "__name__", None
                ),
            }
        )
    rows.sort(key=lambda r: (r["path"], ",".join(r["methods"]), r["endpoint"] or ""))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="-", help="write JSON here (default stdout)")
    args = parser.parse_args()

    rows = inventory()
    text = json.dumps(rows, indent=2, sort_keys=True)
    if args.out == "-":
        print(text)
    else:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
    print(f"{len(rows)} route(s)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
