"""Guard: an ENUM column may only ever grow, whatever order Flyway applies the migrations in.

Flyway runs with ``outOfOrder=true`` (``compose/local/database/flyway-entrypoint.sh``), so a
migration authored on Monday can be applied to production *after* one authored on Tuesday — the
apply order is the MERGE order, not the version order. Every ``ALTER TABLE ... MODIFY ... ENUM``
restates the whole value list, so whichever declaration lands last is the column's final shape.

That is how ``dm_followups.event_type`` lost ``'nurture'`` in production: ``V20260725001931``
(nurture) merged first, then ``V20260724211808`` (catch-up) — an older version number — restated
the list without ``'nurture'`` and silently dropped it, so every auto-nurture re-check insert died
with ``1265 Data truncated for column 'event_type'`` for three weeks (issue #1566).

The invariant that makes the apply order irrelevant: the newest declaration of a column must carry
every value any migration has ever declared for it. Then the version-ordered run and the
merge-ordered run converge on the same superset, and a PR that adds a value without restating the
others fails here instead of in production.
"""

import re
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "compose" / "local" / "database" / "migrations"

# `<table>` and `<column>` for a CREATE TABLE body or an ALTER TABLE clause, plus the ENUM list.
# The CREATE body ends at a `)` that closes the column list — `[^;]*` swallows any trailing table
# options (`) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;`).
_CREATE_RE = re.compile(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?`?(\w+)`?\s*\((.*?)\n\s*\)[^;]*;",
                        re.IGNORECASE | re.DOTALL)
_CREATE_COL_RE = re.compile(r"^\s*`?(\w+)`?\s+ENUM\s*\((.*?)\)", re.IGNORECASE | re.DOTALL | re.MULTILINE)
_ALTER_RE = re.compile(
    r"ALTER\s+TABLE\s+`?(\w+)`?(.*?);", re.IGNORECASE | re.DOTALL)
# ADD/CHANGE count too: a column FIRST declared by `ADD COLUMN ... ENUM(...)` is otherwise invisible
# here, so a later MODIFY that narrows it would be the column's only known declaration and the guard
# would skip it as a single-declaration column — exactly the case it exists to catch.
_ALTER_COL_RE = re.compile(
    r"(?:MODIFY|ADD)\s+(?:COLUMN\s+)?`?(\w+)`?\s+ENUM\s*\((.*?)\)"
    r"|CHANGE\s+(?:COLUMN\s+)?`?\w+`?\s+`?(\w+)`?\s+ENUM\s*\((.*?)\)",
    re.IGNORECASE | re.DOTALL)
_VALUE_RE = re.compile(r"'([^']*)'")


def _strip_comments(sql: str) -> str:
    return "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))


def _version(path: Path) -> tuple:
    """Flyway's version ordering: numeric parts, so V35 sorts before V20260724110833."""
    stamp = path.name.split("__", 1)[0].lstrip("Vv")
    return tuple(int(p) for p in re.split(r"[._]", stamp) if p.isdigit())


def _declarations() -> dict:
    """{(table, column): [(version, migration_name, frozenset(values)), ...]} in version order."""
    found: dict = {}
    for path in sorted(MIGRATIONS_DIR.glob("V*.sql"), key=_version):
        sql = _strip_comments(path.read_text(encoding="utf-8"))
        for table, body in _CREATE_RE.findall(sql):
            for column, values in _CREATE_COL_RE.findall(body):
                found.setdefault((table, column), []).append(
                    (_version(path), path.name, frozenset(_VALUE_RE.findall(values))))
        for table, body in _ALTER_RE.findall(sql):
            for modify_col, modify_values, change_col, change_values in _ALTER_COL_RE.findall(body):
                column, values = (modify_col, modify_values) if modify_col else (change_col, change_values)
                found.setdefault((table, column), []).append(
                    (_version(path), path.name, frozenset(_VALUE_RE.findall(values))))
    return found


def test_every_enum_declaration_in_the_schema_is_parsed():
    """A declaration the parser cannot see is a value the guard cannot protect.

    The guard is only as good as its reader: a column whose FIRST declaration is an
    ``ADD COLUMN ... ENUM(...)`` would otherwise have exactly one visible declaration once a later
    migration narrows it with ``MODIFY``, and the union check skips single-declaration columns. So
    pin the reader against the schema itself — every non-comment ``ENUM(`` in every migration must
    have produced a declaration.
    """
    parsed = {}
    for _, decls in _declarations().items():
        for _, name, _ in decls:
            parsed[name] = parsed.get(name, 0) + 1
    unparsed = []
    for path in sorted(MIGRATIONS_DIR.glob("V*.sql")):
        declared = len(re.findall(r"ENUM\s*\(", _strip_comments(path.read_text(encoding="utf-8")), re.IGNORECASE))
        if declared != parsed.get(path.name, 0):
            unparsed.append(f"{path.name}: {declared} ENUM declaration(s), {parsed.get(path.name, 0)} parsed")
    assert not unparsed, "the ENUM reader has blind spots:\n" + "\n".join(unparsed)


def test_the_newest_declaration_of_every_enum_column_carries_every_value_ever_declared():
    """No apply order can drop a value when the newest declaration is the union of all of them."""
    regressions = []
    for (table, column), decls in sorted(_declarations().items()):
        if len(decls) < 2:
            continue
        union = frozenset().union(*(values for _, _, values in decls))
        _, newest_name, newest = decls[-1]
        missing = sorted(union - newest)
        if missing:
            regressions.append(
                f"{table}.{column}: {newest_name} drops {missing} — restate every value ever "
                f"declared for this column, or an out-of-order apply loses them")
    assert not regressions, "ENUM columns narrowed:\n" + "\n".join(regressions)


def test_dm_followups_and_dm_templates_accept_every_event_type_the_code_writes():
    """The concrete #1566 regression: nurture and the catch-up milestones must coexist."""
    required = {"connection_accepted", "recommendation_received", "collaboration", "profile_viewer",
                "manual", "funnel", "nurture", "job_change", "promotion", "work_anniversary",
                "birthday", "education", "in_the_news"}
    declarations = _declarations()
    for table in ("dm_followups", "dm_templates"):
        newest = declarations[(table, "event_type")][-1][2]
        assert required <= newest, f"{table}.event_type is missing {sorted(required - newest)}"
