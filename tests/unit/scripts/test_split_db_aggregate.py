"""Pins the analysis in scripts/restructure/split_db_aggregate.py (issue #1154).

The script decides which functions leave `db.py` and which names travel with them. Both of its
analyses have already been wrong once -- the movability closure was not transitive, and lambda
parameters read as free globals -- and neither failure was visible in the output it printed. They
surfaced only because a LATER check refused to write.

That matters more than it looks: the `users` slice carries the `SECRET_FIELD_*` constants, which are
the AAD for every encrypted column. Renaming or orphaning one of those orphans every row already
written under it. A mis-scoped move there is not a test failure, it is unrecoverable data.

These tests go away with the script once the last aggregate has moved.
"""

import importlib.util
import os
import pathlib
import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit

_SCRIPT = pathlib.Path("scripts/restructure/split_db_aggregate.py")


def _load():
    spec = importlib.util.spec_from_file_location("split_db_aggregate", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def tool():
    return _load()


class TestFreeNames:
    def test_a_lambda_parameter_is_not_a_free_global(self, tool):
        """`tasks.sort(key=lambda t: t["scheduled_time"])` in db.py read as a global named `t`."""
        src = 'def f(tasks):\n    tasks.sort(key=lambda t: t["scheduled_time"])\n'
        assert "t" not in tool.free_names(src, {"f"})

    def test_a_name_only_in_a_string_annotation_still_counts(self, tool):
        """String annotations are never evaluated, so ast.Name never sees them -- but the import
        still has to travel, or the module lands with an F821 no test can trip.
        """
        src = 'def f(x) -> "datetime | None":\n    return None\n'
        assert "datetime" in tool.free_names(src, {"f"})

    def test_comprehension_and_except_bindings_are_not_free(self, tool):
        src = (
            "def f(rows):\n"
            "    try:\n"
            "        return [r for r in rows]\n"
            "    except ValueError as err:\n"
            "        return err\n"
        )
        assert tool.free_names(src, {"f"}) == set()


class TestTableGrounding:
    def test_prose_that_parses_like_sql_is_rejected(self, tool):
        """Docstrings say things like "read FROM the log". Only real table names may count."""
        import ast
        node = ast.parse('def f():\n    """rows FROM the cache, updated FROM another run"""\n')
        assert tool.tables_touched(node, {"posts", "users"}) == set()

    def test_a_real_table_is_found(self, tool):
        import ast
        node = ast.parse('def f():\n    return "SELECT id FROM posts WHERE user_id = %s"\n')
        assert tool.tables_touched(node, {"posts", "users"}) == {"posts"}


class TestModuleBindings:
    """`--verify` checked function bodies only; the constants travelling with them were unchecked.

    The `users` aggregate carries `SECRET_FIELD_COOKIE_VALUE = "cookies.value"` and three siblings,
    and those string VALUES are the AAD every encrypted column was sealed under. A byte changing in
    transit fails no test — it orphans every row already written, and with `ENCRYPTION_REQUIRED=true`
    in production those reads then return None instead of raising.
    """

    def test_constants_classes_and_annotated_assignments_are_all_captured(self, tool):
        src = (
            'SECRET_FIELD_COOKIE_VALUE = "cookies.value"\n'
            "STEPS: tuple = (1, 2)\n"
            "\n"
            "class Marker(Exception):\n"
            "    pass\n"
            "\n"
            "def fn():\n"
            "    return SECRET_FIELD_COOKIE_VALUE\n"
        )
        binds = tool.module_bindings(src)
        assert set(binds) == {"SECRET_FIELD_COOKIE_VALUE", "STEPS", "Marker"}
        assert binds["SECRET_FIELD_COOKIE_VALUE"] == 'SECRET_FIELD_COOKIE_VALUE = "cookies.value"'

    def test_a_changed_value_is_visible(self, tool):
        """The whole point: same name, different bytes, must not compare equal."""
        before = tool.module_bindings('SECRET_FIELD_PASSWORD = "users.password"\n')
        after = tool.module_bindings('SECRET_FIELD_PASSWORD = "users.passwd"\n')
        assert before["SECRET_FIELD_PASSWORD"] != after["SECRET_FIELD_PASSWORD"]


class TestDeterminism:
    @pytest.mark.parametrize("aggregate", ["posts", "outreach", "users"])
    def test_the_same_input_assigns_the_same_functions(self, aggregate):
        """Run the analysis under two different hash seeds and demand identical output.

        This is the failure it reproduces: `tables_touched` returns a set, Python randomises string
        hashing per process, and a function touching two tables owned by different aggregates got a
        TIED vote that `Counter.most_common` resolved by insertion order. Three identical runs
        assigned `posts` 79, 79 and 77 members. Nothing in the output looked wrong on any single run.

        A subprocess per seed is the point -- hash randomisation is fixed for the life of a process,
        so an in-process test cannot see this at all.
        """
        outputs = set()
        for seed in ("0", "1"):
            env = {**os.environ, "PYTHONHASHSEED": seed}
            result = subprocess.run(
                [sys.executable, str(_SCRIPT), aggregate, "--dry-run"],
                capture_output=True, text=True, env=env)
            # No check=True: a blocked aggregate exits 1 on purpose, and WHICH verdict it reaches
            # is part of what has to be stable. Compare the exit code alongside the report.
            outputs.add((result.returncode, result.stdout))
        assert len(outputs) == 1, (
            f"{aggregate} assigned differently under different hash seeds:\n"
            + "\n---\n".join(f"exit {rc}\n{out}" for rc, out in sorted(outputs)))


class TestEveryTableOwnerIsReal:
    def test_no_owner_entry_names_a_table_that_does_not_exist(self, tool):
        """A typo here silently assigns functions to the wrong module, or to none at all."""
        unknown = sorted(set(tool.TABLE_OWNER) - tool.real_tables())
        assert unknown == [], f"TABLE_OWNER names tables absent from the migrations: {unknown}"
