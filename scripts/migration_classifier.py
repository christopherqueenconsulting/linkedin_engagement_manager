#!/usr/bin/env python3
"""Decide whether a Flyway migration is provably ADDITIVE — the deploy gate's narrowing rule.

`scripts/release_risk_check.py` holds any release that adds a migration, because applied DDL cannot
be rolled back with the image. Most migrations in this repo cannot hurt existing data at all — a new
table, a NULLable column, an ENUM that grows at the end — and holding those parked production on
v0.176.5 for ~24h behind two such migrations (V20260924022119, V20260924022245) with nobody told.
This module is the rule that lets those through while still holding everything else.

**Fail CLOSED.** A statement is additive only when it matches one of the shapes below EXACTLY;
anything unrecognised, unparseable, or ambiguous is non-additive and holds the release. The reason
string for every held statement is what the owner reads in the hold issue, so it names the clause.

Additive shapes (MySQL 8):

- `CREATE TABLE [IF NOT EXISTS] t (...)` — a new table, never `... AS SELECT`/`LIKE`.
- `CREATE INDEX` / `ALTER TABLE t ADD INDEX|KEY|FULLTEXT|SPATIAL ...` — a NON-unique index. A
  UNIQUE index, a PRIMARY KEY or a constraint can REJECT existing rows mid-migration, so it holds.
- `ALTER TABLE t ADD [COLUMN] c <type> ...` — only when NULLable (no `NOT NULL`) or carrying a
  `DEFAULT`; never `UNIQUE`/`PRIMARY KEY`/`AUTO_INCREMENT`, never the parenthesised multi-column form.
- `ALTER TABLE t MODIFY [COLUMN] c ENUM(...) ...` (or `CHANGE c c ENUM(...)` with the SAME name) —
  only when the prior definition of `t.c` (read from the EARLIER migrations) is an ENUM whose value
  list is an exact PREFIX of the new one, the new list still carries every value ANY earlier
  migration ever declared for that column (the #1566 out-of-order hazard, see
  `compose/local/database/migrations/README.md`), and the column modifiers after `ENUM(...)`
  (`NOT NULL`, `DEFAULT ...`) are unchanged. No prior definition found = hold.
- `INSERT [IGNORE] INTO ...` — seed rows, never `... ON DUPLICATE KEY UPDATE` (that rewrites rows).

Everything else holds: `DROP`, `RENAME`, `TRUNCATE`, `UPDATE`, `DELETE`, `REPLACE`, a `MODIFY` that
changes a type or nullability, an ENUM reorder or removal, `SET`, `DELIMITER`/procedures, and any
statement shape not listed above.

Stdlib-only, like the gate that imports it: it runs on a bare Actions runner with no venv.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

#: Keywords that open a non-column item inside a CREATE TABLE body.
_TABLE_ITEM_KEYWORDS = frozenset(
    {"PRIMARY", "KEY", "INDEX", "UNIQUE", "CONSTRAINT", "FOREIGN", "FULLTEXT", "SPATIAL", "CHECK"}
)

#: `ALTER TABLE` clauses that change nothing about the data and are safe to ignore.
_NEUTRAL_ALTER_CLAUSE_RE = re.compile(r"^(ALGORITHM|LOCK)\s*=\s*\w+$", re.IGNORECASE)

_IDENT = r"`?([A-Za-z0-9_$.]+)`?"


@dataclass(frozen=True)
class StatementVerdict:
    """One statement's classification.

    Attributes:
        statement: The statement text, whitespace-collapsed and truncated for display.
        additive: Whether it matched an additive shape.
        reason: Why — for a held statement, the text the owner reads in the hold issue.
    """

    statement: str
    additive: bool
    reason: str


@dataclass(frozen=True)
class MigrationVerdict:
    """A whole migration file's classification: additive only when EVERY statement is.

    Attributes:
        path: The migration's repo-relative path.
        statements: Per-statement verdicts, in file order.
        error: Set when the file could not be read or parsed at all — always non-additive.
    """

    path: str
    statements: tuple[StatementVerdict, ...] = field(default_factory=tuple)
    error: str | None = None

    @property
    def additive(self) -> bool:
        """True only when the file parsed and every statement in it is additive."""
        return self.error is None and bool(self.statements) and all(s.additive for s in self.statements)

    @property
    def reasons(self) -> list[str]:
        """The reasons this file holds a release — empty when it is additive."""
        if self.error is not None:
            return [self.error]
        if not self.statements:
            return ["no SQL statements found"]
        return [f"`{s.statement}` — {s.reason}" for s in self.statements if not s.additive]


# ────────────────────────────────────────────────────────────── lexical helpers


def strip_comments(sql: str) -> str:
    """Remove `--`, `#` and `/* */` comments, leaving quoted strings and identifiers intact.

    Raises:
        ValueError: On an unterminated quote or block comment — the caller turns that into a hold.
    """
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch in "'\"`":
            j = i + 1
            while j < n:
                if sql[j] == "\\" and ch != "`":
                    j += 2
                    continue
                if sql[j] == ch:
                    if j + 1 < n and sql[j + 1] == ch:
                        j += 2
                        continue
                    break
                j += 1
            if j >= n:
                raise ValueError("unterminated quoted string")
            out.append(sql[i : j + 1])
            i = j + 1
        elif sql.startswith("--", i) and (i + 2 >= n or sql[i + 2] in " \t\r\n"):
            nl = sql.find("\n", i)
            i = n if nl == -1 else nl
        elif ch == "#":
            nl = sql.find("\n", i)
            i = n if nl == -1 else nl
        elif sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            if end == -1:
                raise ValueError("unterminated block comment")
            out.append(" ")
            i = end + 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def split_top_level(text: str, sep: str) -> list[str]:
    """Split on `sep` only outside quotes and parentheses; blank pieces are dropped."""
    parts: list[str] = []
    depth, start, quote = 0, 0, ""
    i = 0
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\" and quote != "`":
                i += 2
                continue
            if ch == quote:
                quote = ""
        elif ch in "'\"`":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == sep and depth == 0:
            parts.append(text[start:i])
            start = i + 1
        i += 1
    parts.append(text[start:])
    return [p.strip() for p in parts if p.strip()]


def split_statements(sql: str) -> list[str]:
    """Comment-free statements of a migration, in order.

    Raises:
        ValueError: When the SQL cannot be tokenised, or uses `DELIMITER` (a stored routine body the
            `;` split cannot see into).
    """
    body = strip_comments(sql)
    if re.search(r"^\s*DELIMITER\b", body, re.IGNORECASE | re.MULTILINE):
        raise ValueError("uses DELIMITER (stored routine) — not classifiable")
    return split_top_level(body, ";")


def _matching_paren(text: str, open_idx: int) -> int:
    """Index of the `)` closing the `(` at `open_idx`, quote-aware; -1 when unbalanced."""
    depth, quote = 0, ""
    i = open_idx
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\" and quote != "`":
                i += 2
                continue
            if ch == quote:
                quote = ""
        elif ch in "'\"`":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _strip_strings(text: str) -> str:
    """Blank out quoted literals so a keyword inside a COMMENT '...' never reads as SQL."""
    return re.sub(r"'(?:[^'\\]|\\.|'')*'", "''", text)


def _norm_ident(name: str) -> str:
    return name.strip("`").split(".")[-1].lower()


def _collapse(text: str, limit: int = 160) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


@dataclass(frozen=True)
class EnumDef:
    """An ENUM column definition: the ordered values and the modifiers after the list."""

    values: tuple[str, ...]
    modifiers: str


def parse_enum_definition(definition: str) -> EnumDef | None:
    """Parse `ENUM('a','b') NOT NULL DEFAULT 'a'` — `None` when `definition` is not an ENUM."""
    match = re.match(r"\s*ENUM\s*\(", definition, re.IGNORECASE)
    if not match:
        return None
    open_idx = match.end() - 1
    close_idx = _matching_paren(definition, open_idx)
    if close_idx == -1:
        return None
    inner = definition[open_idx + 1 : close_idx]
    values: list[str] = []
    for raw in split_top_level(inner, ","):
        literal = re.fullmatch(r"'((?:[^'\\]|\\.|'')*)'", raw.strip())
        if literal is None:
            return None
        values.append(literal.group(1).replace("''", "'"))
    modifiers = " ".join(definition[close_idx + 1 :].split()).upper()
    # A column POSITION (`AFTER x` / `FIRST`) is where the column sits, not what it accepts — an
    # ADD that placed it and a later MODIFY that does not restate the position define the same column.
    modifiers = re.sub(r"\s*(\bAFTER\s+`?\w+`?|\bFIRST)\s*$", "", modifiers)
    return EnumDef(values=tuple(values), modifiers=modifiers)


# ────────────────────────────────────────────────────────────── prior-schema history


def migration_version(path: str | Path) -> tuple[int, ...]:
    """Flyway's numeric version ordering, so V35 sorts before V20260724110833."""
    stamp = Path(path).name.split("__", 1)[0].lstrip("Vv")
    return tuple(int(p) for p in re.split(r"[._]", stamp) if p.isdigit())


#: {(table, column): [(version, definition-or-None-when-dropped), ...]} — see `column_history`.
ColumnHistory = dict[tuple[str, str], list[tuple[tuple[int, ...], str | None]]]


def _column_declarations(statement: str) -> list[tuple[str, str, str | None]]:
    """(table, column, definition) for every column a statement declares; `None` marks a DROP."""
    found: list[tuple[str, str, str | None]] = []
    create = re.match(
        rf"\s*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?{_IDENT}\s*\(", statement, re.IGNORECASE
    )
    if create:
        table = _norm_ident(create.group(1))
        close_idx = _matching_paren(statement, create.end() - 1)
        if close_idx != -1:
            for item in split_top_level(statement[create.end() : close_idx], ","):
                col = re.match(rf"{_IDENT}\s+(.*)$", item, re.DOTALL)
                if col and col.group(1).upper() not in _TABLE_ITEM_KEYWORDS:
                    found.append((table, _norm_ident(col.group(1)), col.group(2)))
        return found
    alter = re.match(rf"\s*ALTER\s+TABLE\s+{_IDENT}\s+(.*)$", statement, re.IGNORECASE | re.DOTALL)
    if not alter:
        return found
    table = _norm_ident(alter.group(1))
    for clause in split_top_level(alter.group(2), ","):
        modify = re.match(rf"(?:MODIFY|ADD)\s+(?:COLUMN\s+)?{_IDENT}\s+(.*)$", clause, re.IGNORECASE | re.DOTALL)
        change = re.match(rf"CHANGE\s+(?:COLUMN\s+)?{_IDENT}\s+{_IDENT}\s+(.*)$", clause, re.IGNORECASE | re.DOTALL)
        drop = re.match(rf"DROP\s+(?:COLUMN\s+)?{_IDENT}\s*$", clause, re.IGNORECASE)
        if change:
            found.append((table, _norm_ident(change.group(1)), None))
            found.append((table, _norm_ident(change.group(2)), change.group(3)))
        elif modify and modify.group(1).upper() not in _TABLE_ITEM_KEYWORDS:
            found.append((table, _norm_ident(modify.group(1)), modify.group(2)))
        elif drop and drop.group(1).upper() not in _TABLE_ITEM_KEYWORDS:
            found.append((table, _norm_ident(drop.group(1)), None))
    return found


def column_history(migrations: dict[str, str]) -> ColumnHistory:
    """Every column declaration across `migrations`, grouped per column in Flyway version order.

    Args:
        migrations: `{path: sql}` of the migrations production already has — never the ones being
            classified. A file that cannot be tokenised is skipped: its columns then have less
            history, and a missing prior definition HOLDS, so a skip can only ever hold more.

    Returns:
        The history, each column's list sorted by migration version.
    """
    history: ColumnHistory = {}
    for path in sorted(migrations, key=migration_version):
        try:
            statements = split_statements(migrations[path])
        except ValueError:
            continue
        for statement in statements:
            for table, column, definition in _column_declarations(statement):
                history.setdefault((table, column), []).append((migration_version(path), definition))
    return history


# ────────────────────────────────────────────────────────────── statement classification


def _classify_enum_modify(
    table: str, column: str, definition: str, history: ColumnHistory
) -> tuple[bool, str]:
    new = parse_enum_definition(definition)
    if new is None:
        return False, f"MODIFY of `{table}.{column}` changes its type/definition (not an ENUM widening)"
    prior = history.get((table, column))
    if not prior or prior[-1][1] is None:
        return False, f"no prior definition of `{table}.{column}` found in earlier migrations"
    old = parse_enum_definition(prior[-1][1])
    if old is None:
        return False, f"`{table}.{column}` was not an ENUM before — this changes its type"
    if new.values[: len(old.values)] != old.values:
        return False, (
            f"ENUM `{table}.{column}` does not keep the existing values in place — "
            f"was {list(old.values)}, now {list(new.values)} (reorder/removal)"
        )
    ever = {v for _, d in prior if d is not None for v in (parse_enum_definition(d) or EnumDef((), "")).values}
    dropped = sorted(ever - set(new.values))
    if dropped:
        return False, f"ENUM `{table}.{column}` drops value(s) an earlier migration declared: {dropped}"
    if new.modifiers != old.modifiers:
        return False, (
            f"ENUM `{table}.{column}` changes its column modifiers "
            f"(`{old.modifiers or '<none>'}` → `{new.modifiers or '<none>'}`)"
        )
    added = list(new.values[len(old.values) :])
    return True, f"ENUM `{table}.{column}` only appends {added}"


def _classify_add_column(table: str, column: str, definition: str) -> tuple[bool, str]:
    bare = _strip_strings(definition).upper()
    for keyword in ("PRIMARY KEY", "UNIQUE", "AUTO_INCREMENT", "REFERENCES"):
        if re.search(rf"\b{keyword}\b", bare):
            return False, f"ADD COLUMN `{table}.{column}` is {keyword} — can reject or rewrite existing rows"
    if re.search(r"\bNOT\s+NULL\b", bare) and not re.search(r"\bDEFAULT\b", bare):
        return False, f"ADD COLUMN `{table}.{column}` is NOT NULL with no DEFAULT"
    return True, f"ADD COLUMN `{table}.{column}` (NULLable or DEFAULTed)"


def _classify_alter_clause(table: str, clause: str, history: ColumnHistory) -> tuple[bool, str]:
    head = clause.split(None, 1)[0].upper() if clause.split() else ""
    if _NEUTRAL_ALTER_CLAUSE_RE.match(clause):
        return True, "ALTER option"
    if head == "ADD":
        rest = clause[3:].strip()
        kind = rest.split(None, 1)[0].upper() if rest else ""
        if kind in {"INDEX", "KEY", "FULLTEXT", "SPATIAL"}:
            return True, "ADD INDEX"
        if kind in {"UNIQUE", "PRIMARY", "CONSTRAINT", "FOREIGN", "CHECK"}:
            return False, f"ADD {kind} — a constraint can reject existing rows mid-migration"
        if rest.startswith("("):
            return False, "parenthesised multi-column ADD — not classifiable"
        col = re.match(rf"(?:COLUMN\s+)?{_IDENT}\s+(.*)$", rest, re.IGNORECASE | re.DOTALL)
        if col is None:
            return False, "unrecognised ADD clause"
        return _classify_add_column(table, _norm_ident(col.group(1)), col.group(2))
    if head == "MODIFY":
        col = re.match(rf"MODIFY\s+(?:COLUMN\s+)?{_IDENT}\s+(.*)$", clause, re.IGNORECASE | re.DOTALL)
        if col is None:
            return False, "unrecognised MODIFY clause"
        return _classify_enum_modify(table, _norm_ident(col.group(1)), col.group(2), history)
    if head == "CHANGE":
        col = re.match(rf"CHANGE\s+(?:COLUMN\s+)?{_IDENT}\s+{_IDENT}\s+(.*)$", clause, re.IGNORECASE | re.DOTALL)
        if col is None:
            return False, "unrecognised CHANGE clause"
        old_name, new_name = _norm_ident(col.group(1)), _norm_ident(col.group(2))
        if old_name != new_name:
            return False, f"CHANGE renames `{table}.{old_name}` to `{new_name}`"
        return _classify_enum_modify(table, new_name, col.group(3), history)
    return False, f"{head or 'empty'} clause is not an additive shape"


def classify_statement(statement: str, history: ColumnHistory) -> StatementVerdict:
    """Classify ONE comment-free statement. Anything not matched below is non-additive.

    Args:
        statement: A single statement, as `split_statements` yields it.
        history: `column_history` of the migrations production already has.

    Returns:
        The statement's verdict and reason.
    """
    shown = _collapse(statement)
    bare = _strip_strings(statement)
    upper = " ".join(bare.upper().split())

    create_table = re.match(rf"\s*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?{_IDENT}\s*\(", statement, re.IGNORECASE)
    if create_table:
        close_idx = _matching_paren(statement, create_table.end() - 1)
        tail = _strip_strings(statement[close_idx + 1 :]).upper() if close_idx != -1 else ""
        if close_idx == -1 or re.search(r"\b(SELECT|LIKE)\b", tail):
            return StatementVerdict(shown, False, "CREATE TABLE form not classifiable (AS SELECT / LIKE / unbalanced)")
        return StatementVerdict(shown, True, "CREATE TABLE")

    if re.match(r"CREATE\s+(FULLTEXT\s+|SPATIAL\s+)?INDEX\b", upper):
        return StatementVerdict(shown, True, "CREATE INDEX")
    if upper.startswith("CREATE UNIQUE INDEX"):
        return StatementVerdict(shown, False, "CREATE UNIQUE INDEX — can reject existing rows mid-migration")

    alter = re.match(rf"\s*ALTER\s+TABLE\s+{_IDENT}\s+(.*)$", statement, re.IGNORECASE | re.DOTALL)
    if alter:
        table = _norm_ident(alter.group(1))
        clauses = split_top_level(alter.group(2), ",")
        if not clauses:
            return StatementVerdict(shown, False, "ALTER TABLE with no clauses")
        notes: list[str] = []
        for clause in clauses:
            ok, why = _classify_alter_clause(table, clause, history)
            if not ok:
                return StatementVerdict(shown, False, why)
            notes.append(why)
        return StatementVerdict(shown, True, "; ".join(notes))

    if re.match(r"INSERT\s+(IGNORE\s+)?INTO\b", upper):
        if "ON DUPLICATE KEY UPDATE" in upper:
            return StatementVerdict(shown, False, "INSERT … ON DUPLICATE KEY UPDATE rewrites existing rows")
        return StatementVerdict(shown, True, "INSERT seed rows")

    first = upper.split(None, 1)[0] if upper else "empty"
    return StatementVerdict(shown, False, f"{first} statement is not an additive shape")


def classify_migration(path: str, sql: str | None, history: ColumnHistory) -> MigrationVerdict:
    """Classify a whole migration file. `sql=None` (unreadable) is a hold, never a pass.

    Args:
        path: The migration's repo-relative path, for display.
        sql: The file's contents, or `None` when it could not be read.
        history: `column_history` of the migrations production already has.

    Returns:
        The file's verdict — additive only when it parsed and every statement is additive.
    """
    if sql is None:
        return MigrationVerdict(path=path, error="migration file could not be read")
    try:
        statements = split_statements(sql)
    except ValueError as exc:
        return MigrationVerdict(path=path, error=f"SQL could not be parsed: {exc}")
    return MigrationVerdict(path=path, statements=tuple(classify_statement(s, history) for s in statements))


#: The MySQL init schema (`docker-entrypoint-initdb.d`) every Flyway migration builds on — the
#: original definition of `logs.action_type`, `posts.post_type` and friends lives here, not in a `V*`.
BASELINE_SCHEMA = "setup.sql"


def classify_new_migrations(new_paths: list[str], migrations_dir: Path, repo_root: Path) -> list[MigrationVerdict]:
    """Classify each newly added migration against every OTHER migration on disk as prior history.

    The prior history is every `V*.sql` in `migrations_dir` that is not being classified, plus the
    `BASELINE_SCHEMA` beside it (version `()`, so it sorts before every `V*`).

    Args:
        new_paths: Repo-relative paths of the migrations this release would apply.
        migrations_dir: The checked-out migrations directory (at the release's own tag).
        repo_root: The checkout root the `new_paths` are relative to.

    Returns:
        One verdict per new path, in the order given. A path missing from the checkout is a hold.
    """
    new_names = {Path(p).name for p in new_paths}
    prior: dict[str, str] = {}
    for file in sorted(migrations_dir.glob("V*.sql")):
        if file.name in new_names:
            continue
        try:
            prior[file.name] = file.read_text(encoding="utf-8")
        except OSError:
            continue
    try:
        prior[BASELINE_SCHEMA] = (migrations_dir.parent / BASELINE_SCHEMA).read_text(encoding="utf-8")
    except OSError:
        pass
    history = column_history(prior)
    verdicts: list[MigrationVerdict] = []
    for rel in new_paths:
        try:
            sql: str | None = (repo_root / rel).read_text(encoding="utf-8")
        except OSError:
            sql = None
        verdicts.append(classify_migration(rel, sql, history))
    return verdicts
