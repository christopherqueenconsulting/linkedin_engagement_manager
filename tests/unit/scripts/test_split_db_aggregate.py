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

    def test_a_function_local_import_does_not_bind_the_whole_module(self, tool):
        """The subtlest bug in this tool, and the only one a test caught rather than a linter.

        `posts` was the first aggregate where SOME functions do a function-local
        `from ... import log_error` while others rely on db.py's module-level one. Treating the
        local import as a module binding made the generated import block omit `log_error` entirely,
        so the functions without a local import raised `NameError` — on the ERROR path only, which
        is why nothing but a test could reach it.
        """
        src = (
            "def a():\n"
            "    from cqc_lem.utilities.logger import log_error\n"
            "    log_error('boom')\n"
            "\n"
            "def b():\n"
            "    log_error('boom')\n"          # no local import — needs a module-level one
        )
        assert "log_error" in tool.free_names(src, {"a", "b"})

    def test_a_module_level_import_does_bind(self, tool):
        src = "from cqc_lem.utilities.logger import log_error\n\ndef a():\n    log_error('x')\n"
        assert "log_error" not in tool.free_names(src, {"a"})

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


class TestPrivateHelperAdoption:
    """A helper that runs no SQL of its own belongs to no aggregate, and strands its callers there.

    That is what the residue slices ran into: 8 table-less helpers were holding 21 members behind
    them, and every aggregate reported `0 movable` without naming the reason.
    """

    def test_a_private_helper_follows_its_only_reader(self, tool):
        adopted = tool.adopt_private_helpers(
            {"get_avatar_training": "avatar"}, {},
            {"_avatar_row_to_dict": {"get_avatar_training"}})
        assert adopted["_avatar_row_to_dict"] == "avatar"

    def test_adoption_is_transitive(self, tool):
        """A helper whose reader is itself an adopted helper still has one unambiguous owner."""
        adopted = tool.adopt_private_helpers(
            {"get_draft": "groups"}, {},
            {"_row": {"get_draft"}, "_column_list": {"_row"}})
        assert adopted["_column_list"] == "groups"

    def test_two_aggregates_reading_it_leaves_it_behind(self, tool):
        """Shared vocabulary belongs in platform/db/shared.py, which is a human's call."""
        adopted = tool.adopt_private_helpers(
            {"a": "users", "b": "posts"}, {}, {"_shared": {"a", "b"}})
        assert "_shared" not in adopted

    def test_a_public_name_is_never_adopted(self, tool):
        """The limit that keeps the rule from walking the facade's API into a repository.

        Applied to public names, adoption is transitive enough to drag `is_premium_subscriber`
        (billing) into `users` on the strength of one caller that was itself dragged there.
        """
        adopted = tool.adopt_private_helpers(
            {"update_engagement_preferences": "users"}, {},
            {"max_catchup_touches_allowed": {"update_engagement_preferences"}})
        assert "max_catchup_touches_allowed" not in adopted

    def test_a_helper_with_no_readers_in_db_py_is_left_alone(self, tool):
        assert tool.adopt_private_helpers({"a": "users"}, {}, {"_orphan": set()}) == {"a": "users"}

    def test_an_ambiguous_function_is_never_adopted(self, tool):
        """It spans aggregates by its own SQL; one caller does not settle that."""
        adopted = tool.adopt_private_helpers(
            {"reader": "posts"}, {"_spanning": ["outreach", "posts"]}, {"_spanning": {"reader"}})
        assert "_spanning" not in adopted


class TestAppendingToAnExistingModule:
    """After the ten first-pass slices every further move lands in a module that already exists."""

    HEADER = ('"""Docstring."""\n\nfrom typing import Optional\n\n'
              "from cqc_lem.platform.db.connection import db_cursor\n\n\n"
              "def already_here() -> Optional[dict]:\n    return None\n")

    def test_what_was_already_there_survives(self, tool):
        out = tool.append_to_module(self.HEADER, [], "def moved():\n    return 1\n")
        assert "def already_here()" in out and "def moved()" in out

    def test_only_the_imports_the_module_lacks_are_added(self, tool):
        out = tool.append_to_module(
            self.HEADER,
            ["from typing import (\n    Optional,\n    Union,\n)",
             "from cqc_lem.platform.db.connection import db_cursor"],
            "def moved():\n    return 1\n")
        assert out.count("from cqc_lem.platform.db.connection import db_cursor") == 1
        assert "Union" in out
        assert out.count("Optional") == self.HEADER.count("Optional")

    def test_a_missing_import_lands_above_the_appended_code(self, tool):
        out = tool.append_to_module(
            self.HEADER, ["import json"], "def moved():\n    return json.loads('{}')\n")
        assert out.index("import json") < out.index("def moved()")

    def test_a_single_name_from_import_the_module_lacks_is_still_added(self, tool):
        """The one-name form carries no name on a line of its own, so a text scan finds none.

        Dropping it is the worst outcome available here: the appended function reads a free name and
        the module raises NameError the first time that line runs, which no import-time check sees.
        """
        out = tool.append_to_module(
            self.HEADER,
            ["from cqc_lem.platform.db.enums import GroupPostDraftStatus"],
            "def moved():\n    return GroupPostDraftStatus.READY\n")
        assert "from cqc_lem.platform.db.enums import GroupPostDraftStatus" in out
        assert out.index("GroupPostDraftStatus") < out.index("def moved()")

    def test_an_aliased_import_keeps_its_alias(self, tool):
        """`connection as _connection` is how every module reaches the patchable connection seam."""
        out = tool.append_to_module(
            self.HEADER,
            ["from cqc_lem.platform.db import connection as _connection"],
            "def moved():\n    return _connection.get_db_connection()\n")
        assert "from cqc_lem.platform.db import connection as _connection" in out

    def test_a_single_name_from_import_already_bound_is_not_repeated(self, tool):
        out = tool.append_to_module(
            self.HEADER, ["from typing import Optional"], "def moved():\n    return None\n")
        assert out.count("from typing import Optional") == 1


class TestFacadeRewrites:
    def test_the_aggregates_existing_import_block_is_extended_not_duplicated(self, tool):
        src = ("from cqc_lem.platform.db.repositories.avatar import (\n"
               "    add_avatar_credits,\n"
               "    insert_avatar_training,\n"
               ")\n"
               "from cqc_lem.utilities.crypto import (\n    decrypt,\n)\n")
        out = tool.merge_facade_import(src, "avatar", ["get_active_avatar"])
        assert out.count("from cqc_lem.platform.db.repositories.avatar import") == 1
        assert "    get_active_avatar,\n" in out
        assert "    add_avatar_credits,\n" in out

    def test_a_first_pass_aggregate_still_gets_a_new_block(self, tool):
        src = "from cqc_lem.utilities.crypto import (\n    decrypt,\n)\n"
        out = tool.merge_facade_import(src, "groups", ["get_user_groups"])
        assert out.startswith("from cqc_lem.platform.db.repositories.groups import (\n")

    def test_dunder_all_gains_the_names_without_reordering_the_rest(self, tool):
        """The list is ten sorted runs, not one sorted list — re-sorting it is a 900-line diff."""
        src = '__all__ = [\n    "b_two",\n    "a_one",\n    "c_three",\n]\n'
        out = tool.merge_dunder_all(src, ["d_four", "a_zero"])
        assert out == ('__all__ = [\n    "b_two",\n    "a_one",\n    "c_three",\n'
                       '    "a_zero",\n    "d_four",\n]\n')

    def test_a_name_already_exported_is_not_added_twice(self, tool):
        src = '__all__ = [\n    "a_one",\n]\n'
        assert tool.merge_dunder_all(src, ["a_one"]).count('"a_one"') == 1

    def test_a_db_py_without_dunder_all_is_refused(self, tool):
        """No `__all__` means ruff F401 and CodeQL both read the re-exports as dead imports."""
        with pytest.raises(SystemExit):
            tool.merge_dunder_all("x = 1\n", ["get_user_groups"])


class TestEveryTableOwnerIsReal:
    def test_no_owner_entry_names_a_table_that_does_not_exist(self, tool):
        """A typo here silently assigns functions to the wrong module, or to none at all."""
        unknown = sorted(set(tool.TABLE_OWNER) - tool.real_tables())
        assert unknown == [], f"TABLE_OWNER names tables absent from the migrations: {unknown}"
