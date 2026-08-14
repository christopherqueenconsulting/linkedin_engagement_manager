#!/usr/bin/env python3
"""Write the published OpenAPI document to the checked-in snapshot the SPA generates types from.

The SPA's TypeScript types are generated from `/api/openapi.json` (issue #1446), and a generator
that has to `docker compose up` first is a generator nobody runs. So the schema is dumped straight
off the app object — no server, no database, no network — into
`src/cqc_lem/ui/openapi.json`, which is committed. `npm run gen:api-types` reads that file.

Two properties make the snapshot safe to commit:

* **It is what `/api/openapi.json` actually serves.** `app.openapi()` is the same call the route
  makes, taken after `_hide_admin_routes_from_schema()` has run at import, so every `/api/admin/*`
  operation is already out of it. Dumping from the route table instead would re-publish all 18.
* **It does not depend on the environment.** Nothing in the schema is read from `os.environ`;
  the placeholders below exist only so importing `api.main` succeeds on a checkout with no `.env`
  (the OpenAI client raises at import without a key). They are not credentials and nothing in this
  process makes a network call.

Usage::

    poetry run python scripts/dump_openapi.py           # rewrite the snapshot
    poetry run python scripts/dump_openapi.py --check   # exit 1 if it is stale

`--check` is for a developer who wants the answer without a dirty tree; CI asks the same question
from `tests/unit/api/test_openapi_snapshot.py`, which runs in the required Unit Tests lane.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_PATH = REPO_ROOT / "src" / "cqc_lem" / "ui" / "openapi.json"

# Import-time placeholders only — see the module docstring. `setdefault` never overwrites a real
# value, so running this with a populated `.env` loaded changes nothing.
_IMPORT_PLACEHOLDERS = {
    "OPENAI_API_KEY": "openapi-dump-placeholder",
    "DB_HOST": "localhost",
    "DB_USER": "openapi-dump",
    "DB_PASSWORD": "openapi-dump",
    "DB_NAME": "openapi-dump",
}


def render_schema() -> str:
    """Return the published OpenAPI document as the exact text the snapshot file holds.

    Returns:
        The JSON document, sorted, two-space indented and newline-terminated.
    """
    for key, value in _IMPORT_PLACEHOLDERS.items():
        os.environ.setdefault(key, value)
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from cqc_lem.api.main import app

    return json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n"


def _label() -> str:
    """The snapshot path as a reader should see it — repo-relative when it is inside the repo."""
    try:
        return str(SNAPSHOT_PATH.relative_to(REPO_ROOT))
    except ValueError:
        return str(SNAPSHOT_PATH)


def main() -> int:
    """Rewrite the snapshot, or report whether it is stale.

    Returns:
        0 when the snapshot is up to date (or was rewritten), 1 when `--check` found it stale.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="do not write; exit 1 if the committed snapshot is stale")
    args = parser.parse_args()

    rendered = render_schema()
    current = SNAPSHOT_PATH.read_text() if SNAPSHOT_PATH.exists() else None

    if args.check:
        if current == rendered:
            print(f"up to date: {_label()}")
            return 0
        print(f"STALE: {_label()} — run "
              "`poetry run python scripts/dump_openapi.py` and `npm run gen:api-types`",
              file=sys.stderr)
        return 1

    if current == rendered:
        print(f"unchanged: {_label()}")
        return 0
    SNAPSHOT_PATH.write_text(rendered)
    print(f"wrote {_label()} — now run `npm run gen:api-types` in "
          "src/cqc_lem/ui")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
