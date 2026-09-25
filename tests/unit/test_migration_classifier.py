"""`scripts/migration_classifier.py`: which migrations may auto-deploy, and which HOLD.

The two real migrations that parked production on v0.176.5 for ~24h are graded as additive against
the repo's own history; a destructive set is graded as held, each for a named reason. Everything the
classifier cannot read or parse must HOLD — fail closed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "scripts"))

import migration_classifier as mc  # noqa: E402

MIGRATIONS = _ROOT / "compose" / "local" / "database" / "migrations"
ENUM_WIDENING = (
    "compose/local/database/migrations/V20260924022119__add_invite_follow_group_comment_to_logs_action_type.sql"
)
APPROVED_BY = "compose/local/database/migrations/V20260924022245__posts_approved_by.sql"

# A small, self-contained prior schema for the synthetic cases below.
PRIOR = {
    "V1__base.sql": """
        CREATE TABLE logs (
            id INT AUTO_INCREMENT PRIMARY KEY,
            action_type ENUM('comment','dm') NOT NULL,
            note VARCHAR(64) NULL,
            KEY idx_note (note)
        );
        CREATE TABLE users (
            id INT PRIMARY KEY,
            tier ENUM('free','pro') DEFAULT 'free',
            email VARCHAR(255) NULL
        );
    """,
    "V2__widen.sql": "ALTER TABLE logs MODIFY COLUMN action_type ENUM('comment','dm','reply') NOT NULL;",
    "V3__status.sql": "ALTER TABLE users ADD COLUMN status ENUM('a','b') NULL AFTER email;",
}


def _classify(sql: str) -> mc.MigrationVerdict:
    return mc.classify_migration("V9__new.sql", sql, mc.column_history(PRIOR))


# ---------------------------------------------------------------- the two real migrations


def test_the_two_real_migrations_that_parked_production_are_additive():
    verdicts = mc.classify_new_migrations([ENUM_WIDENING, APPROVED_BY], MIGRATIONS, _ROOT)
    assert [v.additive for v in verdicts] == [True, True], [v.reasons for v in verdicts]
    assert "only appends ['invite', 'follow', 'group_comment']" in verdicts[0].statements[0].reason
    assert "posts.approved_by" in verdicts[1].statements[0].reason
    assert "posts.approved_at" in verdicts[1].statements[0].reason


def test_the_real_1566_enum_regression_would_have_been_held():
    """V20260725001931 restated dm_followups.event_type WITHOUT values an earlier file declared."""
    path = "compose/local/database/migrations/V20260725001931__add_dm_nurture.sql"
    verdict = mc.classify_new_migrations([path], MIGRATIONS, _ROOT)[0]
    assert not verdict.additive
    assert any("dm_followups.event_type" in r for r in verdict.reasons)


def test_every_real_migration_classifies_without_raising():
    files = sorted(MIGRATIONS.glob("V*.sql"), key=mc.migration_version)
    for i, file in enumerate(files):
        prior = {p.name: p.read_text(encoding="utf-8") for p in files[:i]}
        verdict = mc.classify_migration(file.name, file.read_text(encoding="utf-8"), mc.column_history(prior))
        assert verdict.additive or verdict.reasons, file.name


def test_a_path_missing_from_the_checkout_holds(tmp_path):
    verdict = mc.classify_new_migrations(["compose/local/database/migrations/V9__gone.sql"], tmp_path, tmp_path)[0]
    assert not verdict.additive
    assert verdict.reasons == ["migration file could not be read"]


# ---------------------------------------------------------------- additive shapes


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TABLE widgets (id INT PRIMARY KEY, name VARCHAR(10) NOT NULL);",
        "CREATE TABLE IF NOT EXISTS `widgets` (id INT) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;",
        "CREATE INDEX idx_email ON users (email);",
        "ALTER TABLE users ADD INDEX idx_email (email), ALGORITHM=INPLACE, LOCK=NONE;",
        "ALTER TABLE users ADD KEY idx_email (email);",
        "ALTER TABLE users ADD COLUMN nickname VARCHAR(32) NULL;",
        "ALTER TABLE users ADD nickname VARCHAR(32);",
        "ALTER TABLE users ADD COLUMN enabled TINYINT(1) NOT NULL DEFAULT 0 AFTER email;",
        "INSERT INTO faq (q, a) VALUES ('x', 'y');",
        "INSERT IGNORE INTO faq (q, a) VALUES ('x', 'y');",
        "ALTER TABLE logs MODIFY COLUMN action_type ENUM('comment','dm','reply','invite') NOT NULL;",
        "ALTER TABLE logs MODIFY action_type ENUM('comment','dm','reply') NOT NULL;",
        "ALTER TABLE logs CHANGE action_type action_type ENUM('comment','dm','reply','x') NOT NULL;",
        # the prior declaration carried a position (AFTER email) — a MODIFY that omits it is the same column
        "ALTER TABLE users MODIFY COLUMN status ENUM('a','b','c') NULL;",
        # an ENUM whose only prior definition is the CREATE TABLE
        "ALTER TABLE users MODIFY tier ENUM('free','pro','team') DEFAULT 'free';",
        "-- DROP TABLE users; is only a comment\n/* DELETE FROM users; */\n# UPDATE users\n"
        "ALTER TABLE users ADD COLUMN bio TEXT NULL COMMENT 'never DROP this';",
    ],
)
def test_additive_shapes_auto_deploy(sql):
    verdict = _classify(sql)
    assert verdict.additive, verdict.reasons
    assert verdict.reasons == []


# ---------------------------------------------------------------- held shapes


@pytest.mark.parametrize(
    ("sql", "why"),
    [
        ("DROP TABLE logs;", "DROP statement"),
        ("ALTER TABLE logs DROP COLUMN note;", "DROP clause"),
        ("RENAME TABLE logs TO logs_old;", "RENAME statement"),
        ("ALTER TABLE logs RENAME TO logs_old;", "RENAME clause"),
        ("ALTER TABLE logs RENAME COLUMN note TO memo;", "RENAME clause"),
        ("ALTER TABLE logs CHANGE note memo VARCHAR(64) NULL;", "renames `logs.note`"),
        ("TRUNCATE TABLE logs;", "TRUNCATE statement"),
        ("UPDATE users SET tier = 'pro';", "UPDATE statement"),
        ("DELETE FROM users WHERE id = 1;", "DELETE statement"),
        ("REPLACE INTO users (id) VALUES (1);", "REPLACE statement"),
        ("INSERT INTO users (id) VALUES (1) ON DUPLICATE KEY UPDATE id = id;", "ON DUPLICATE KEY UPDATE"),
        ("ALTER TABLE logs MODIFY COLUMN note VARCHAR(255) NULL;", "changes its type"),
        ("ALTER TABLE logs MODIFY COLUMN note VARCHAR(64) NOT NULL;", "changes its type"),
        ("ALTER TABLE logs MODIFY COLUMN action_type ENUM('dm','comment','reply') NOT NULL;", "reorder/removal"),
        ("ALTER TABLE logs MODIFY COLUMN action_type ENUM('comment','reply') NOT NULL;", "reorder/removal"),
        ("ALTER TABLE logs MODIFY COLUMN action_type ENUM('comment','new','dm','reply') NOT NULL;", "reorder/removal"),
        ("ALTER TABLE logs MODIFY COLUMN action_type ENUM('comment','dm','reply','x') NULL;", "modifiers"),
        ("ALTER TABLE logs MODIFY COLUMN action_type ENUM('comment','dm','reply') NOT NULL DEFAULT 'dm';", "modifiers"),
        ("ALTER TABLE nowhere MODIFY COLUMN kind ENUM('a','b') NOT NULL;", "no prior definition"),
        ("ALTER TABLE users MODIFY COLUMN email ENUM('a') NULL;", "was not an ENUM before"),
        ("ALTER TABLE logs ADD COLUMN owner INT NOT NULL;", "NOT NULL with no DEFAULT"),
        ("ALTER TABLE logs ADD COLUMN token CHAR(36) NULL UNIQUE;", "UNIQUE"),
        ("ALTER TABLE logs ADD COLUMN uid INT AUTO_INCREMENT;", "AUTO_INCREMENT"),
        ("ALTER TABLE logs ADD UNIQUE INDEX ux_note (note);", "ADD UNIQUE"),
        ("ALTER TABLE logs ADD PRIMARY KEY (id);", "ADD PRIMARY"),
        ("ALTER TABLE logs ADD CONSTRAINT fk FOREIGN KEY (id) REFERENCES users(id);", "ADD CONSTRAINT"),
        ("ALTER TABLE logs ADD (a INT, b INT);", "parenthesised"),
        ("CREATE UNIQUE INDEX ux ON logs (note);", "CREATE UNIQUE INDEX"),
        ("CREATE TABLE copy AS SELECT * FROM logs;", "not an additive shape"),
        ("CREATE TABLE copy (id INT) AS SELECT id FROM logs;", "AS SELECT"),
        ("ALTER TABLE logs ALTER COLUMN note SET DEFAULT 'x';", "ALTER clause"),
        ("SET FOREIGN_KEY_CHECKS = 0;", "SET statement"),
        ("DELIMITER //\nCREATE PROCEDURE p() BEGIN SELECT 1; END //\nDELIMITER ;", "DELIMITER"),
        ("INSERT INTO faq (q) VALUES ('unterminated);", "could not be parsed"),
        ("/* never closed", "could not be parsed"),
        ("-- only a comment\n", "no SQL statements found"),
        # one additive statement does not launder a destructive one in the same file
        ("CREATE TABLE ok (id INT);\nDROP TABLE logs;", "DROP statement"),
    ],
)
def test_destructive_or_unknown_shapes_hold(sql, why):
    verdict = _classify(sql)
    assert not verdict.additive
    assert any(why in reason for reason in verdict.reasons), verdict.reasons


def test_an_unreadable_file_holds():
    verdict = mc.classify_migration("V9__x.sql", None, {})
    assert not verdict.additive
    assert verdict.reasons == ["migration file could not be read"]


def test_an_enum_value_an_older_migration_declared_cannot_be_dropped_even_if_the_newest_lacks_it():
    """The #1566 shape: prefix of the newest is not enough when an earlier file had another value."""
    history = mc.column_history(
        {
            "V1__a.sql": "CREATE TABLE t (k ENUM('a','b','nurture') NOT NULL);",
            "V2__b.sql": "ALTER TABLE t MODIFY k ENUM('a','b') NOT NULL;",
        }
    )
    verdict = mc.classify_migration("V3__c.sql", "ALTER TABLE t MODIFY k ENUM('a','b','c') NOT NULL;", history)
    assert not verdict.additive
    assert any("drops value(s)" in r and "nurture" in r for r in verdict.reasons)


def test_a_dropped_column_has_no_prior_definition():
    history = mc.column_history(
        {
            "V1__a.sql": "CREATE TABLE t (k ENUM('a') NOT NULL);",
            "V2__b.sql": "ALTER TABLE t DROP COLUMN k;",
        }
    )
    verdict = mc.classify_migration("V3__c.sql", "ALTER TABLE t MODIFY k ENUM('a','b') NOT NULL;", history)
    assert not verdict.additive
    assert any("no prior definition" in r for r in verdict.reasons)


def test_an_unparseable_prior_migration_only_ever_holds_more():
    history = mc.column_history({"V1__bad.sql": "CREATE TABLE t (k ENUM('a) NOT NULL;"})
    verdict = mc.classify_migration("V2__c.sql", "ALTER TABLE t MODIFY k ENUM('a','b') NOT NULL;", history)
    assert not verdict.additive


def test_the_baseline_schema_is_prior_history(tmp_path):
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (tmp_path / "setup.sql").write_text("CREATE TABLE t (k ENUM('a') NOT NULL);", encoding="utf-8")
    (migrations / "V2__widen.sql").write_text("ALTER TABLE t MODIFY k ENUM('a','b') NOT NULL;", encoding="utf-8")
    verdict = mc.classify_new_migrations(["migrations/V2__widen.sql"], migrations, tmp_path)[0]
    assert verdict.additive, verdict.reasons


def test_the_file_being_classified_is_never_its_own_prior_history(tmp_path):
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "V2__widen.sql").write_text("ALTER TABLE t MODIFY k ENUM('a','b') NOT NULL;", encoding="utf-8")
    verdict = mc.classify_new_migrations(["migrations/V2__widen.sql"], migrations, tmp_path)[0]
    assert not verdict.additive
    assert any("no prior definition" in r for r in verdict.reasons)


def test_parse_enum_definition_rejects_a_non_literal_value():
    assert mc.parse_enum_definition("ENUM(a, 'b') NOT NULL") is None
    assert mc.parse_enum_definition("ENUM('a'") is None
    assert mc.parse_enum_definition("VARCHAR(3)") is None
    assert mc.parse_enum_definition("ENUM('it''s','b') NOT NULL AFTER x") == mc.EnumDef(("it's", "b"), "NOT NULL")


def test_split_top_level_ignores_separators_in_quotes_and_parens():
    assert mc.split_top_level("a(1,2), 'x,y', \"p,q\", `c,d`", ",") == ["a(1,2)", "'x,y'", '"p,q"', "`c,d`"]
    assert mc.split_top_level("'it\\'s,', b", ",") == ["'it\\'s,'", "b"]


def test_strip_comments_keeps_quoted_markers():
    assert mc.strip_comments("SELECT '--x', \"#y\", `/*z*/`; -- gone") == "SELECT '--x', \"#y\", `/*z*/`; "
    assert mc.strip_comments("a--b") == "a--b"
    assert mc.strip_comments("'it''s' -- c") == "'it''s' "


def test_migration_version_orders_legacy_before_timestamps():
    names = ["V20260724110833__x.sql", "V35__y.sql", "setup.sql", "V4__z.sql"]
    assert sorted(names, key=mc.migration_version) == ["setup.sql", "V4__z.sql", "V35__y.sql", "V20260724110833__x.sql"]


def test_a_long_statement_is_truncated_for_display():
    verdict = _classify("UPDATE users SET email = '" + "x" * 400 + "';")
    assert len(verdict.statements[0].statement) <= 160
    assert verdict.statements[0].statement.endswith("…")
