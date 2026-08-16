"""The DM event vocabulary the app writes must fit the enum the migrations declare.

Issue #1576: `V20260725001931__add_dm_nurture.sql` restated `dm_templates.event_type` /
`dm_followups.event_type` WITHOUT the six catch-up values a lower-versioned migration had
added, so on the repo's declared schema the app could write event types the column no longer
accepted (MySQL strict mode, error 1265). These tests read the migrations themselves, so the
next MODIFY that narrows either column fails here instead of at send time.
"""
import re
from pathlib import Path

import pytest

MIGRATIONS_DIR = Path("compose/local/database/migrations")

DM_ENUM_TABLES = ("dm_templates", "dm_followups")

# The union both columns must always carry: every value either has ever declared.
EXPECTED_DM_EVENT_TYPES = {
    "connection_accepted", "recommendation_received", "collaboration", "profile_viewer",
    "manual", "funnel", "nurture", "job_change", "promotion", "work_anniversary",
    "birthday", "education", "in_the_news",
}


def _migration_version(path: Path) -> int:
    """Sort key for a migration file — `V35__x.sql` and `V20260725001931__x.sql` are both ints."""
    return int(path.name.split("__", 1)[0].lstrip("V"))


def _event_type_enum_for(table: str) -> set:
    """The final declared `<table>.event_type` values, in Flyway version order.

    Takes the HIGHEST-versioned statement that declares the column (a `CREATE TABLE` or an
    `ALTER TABLE … MODIFY`), which is the end state of the repo's schema.
    """
    declared = None
    for path in sorted(MIGRATIONS_DIR.glob("V*.sql"), key=_migration_version):
        for statement in path.read_text().split(";"):
            named = re.search(r"(?:ALTER|CREATE)\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?`?(\w+)`?",
                              statement, re.IGNORECASE)
            if not named or named.group(1) != table:
                continue
            values = re.search(r"event_type\s+ENUM\(([^)]*)\)", statement, re.IGNORECASE)
            if values:
                declared = set(re.findall(r"'([^']+)'", values.group(1)))
    assert declared is not None, f"no migration declares {table}.event_type"
    return declared


@pytest.mark.parametrize("table", DM_ENUM_TABLES)
def test_the_final_enum_is_the_full_union(table):
    assert _event_type_enum_for(table) == EXPECTED_DM_EVENT_TYPES


@pytest.mark.parametrize("table", DM_ENUM_TABLES)
def test_the_default_templates_all_fit_the_enum(table):
    # _DM_DEFAULT_TEMPLATES is the vocabulary the app writes — every key reaches both columns
    # (a template row at step 0, a dm_followups row when the sequence is enqueued).
    from cqc_lem.utilities.db import _DM_DEFAULT_TEMPLATES

    assert set(_DM_DEFAULT_TEMPLATES) <= _event_type_enum_for(table)


@pytest.mark.parametrize("table", DM_ENUM_TABLES)
def test_the_spa_s_template_editor_events_all_fit_the_enum(table):
    # Settings > DM Templates saves the whole set in one transaction (#1575), so ONE event the
    # column rejects 500s the entire save.
    types_ts = Path("src/cqc_lem/ui/src/pages/account/types.ts").read_text()
    block = re.search(r"export const DM_EVENTS[^=]*=\s*\[(.*?)\n\]", types_ts, re.DOTALL)
    assert block, "DM_EVENTS is no longer declared as a literal array in types.ts"
    keys = set(re.findall(r"key:\s*'([^']+)'", block.group(1)))
    assert keys, "read no DM_EVENTS keys"
    assert keys <= _event_type_enum_for(table)
