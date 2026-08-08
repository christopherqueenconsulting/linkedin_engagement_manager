"""A module-level name in a test file that NOTHING reads must not reach a PR.

This is the local pre-flight for the one CodeQL finding that has actually cost this repo merge
time. CodeQL files an unread private module-level variable as `py/unused-global-variable`, the
required `CodeQL PR Quality Gate` counts it as a new alert, and the merge queue evicts the PR —
so the cost lands at the very end of the pipeline, minutes into a merge_group run, on a ref that
has no PR to comment on. Both #1168 and #1171 were evicted repeatedly on 2026-08-08 for exactly
one orphaned line each (`_DB = "cqc_lem.utilities.db"`), and both were fixed by deleting it.

The alias is orphaned by a NORMAL edit, which is why a guard is worth having: a file binds
`_DB = "cqc_lem.utilities.db"` to spell patch targets as `f"{_DB}.helper"`, then a retarget
rewrites those strings to the module the helper moved to and the binding is left behind. Nothing
fails. The suite is green. The alert only exists once CodeQL has run in CI.

Scanning is cheap and local; the merge queue is neither.

Deliberately NOT limited to the `_`-prefixed spelling CodeQL flags. A public `DB = "..."` nothing
reads is the same dead line, and letting it sit invites the next author to copy it and rename it
private. The rule here is simply: if the module binds a name at import time, something must read
it — in this module, or through an import somewhere else in the suite.
"""

import ast
import pathlib

import pytest

pytestmark = pytest.mark.unit

_TESTS = pathlib.Path(__file__).resolve().parents[1]

# Names pytest itself consumes, so "nothing reads it" is wrong for them: pytest reaches into the
# module namespace rather than the module referencing them. `pytestmark` is the one that matters —
# it is on nearly every file in this suite and it is never read by the code that declares it.
_CONSUMED_BY_PYTEST = frozenset({
    "pytestmark",
    "pytest_plugins",
    "collect_ignore",
    "collect_ignore_glob",
})


def _module_bindings(tree: ast.Module) -> dict[str, int]:
    """Names this module binds at import time, mapped to the line that binds them.

    Only plain `NAME = ...` / `NAME: T = ...` at module level. Tuple unpacking, subscript and
    attribute targets are skipped: none of them is the orphaned-alias shape, and reading them
    correctly would cost false positives that get the whole assertion ignored.
    """
    bound: dict[str, int] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    bound.setdefault(target.id, node.lineno)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            bound.setdefault(node.target.id, node.lineno)
    return bound


def _names_read(tree: ast.Module) -> set[str]:
    """Every name loaded anywhere in the module, at any nesting depth.

    `ast.walk` is the point: the read is almost always inside a method, a comprehension or an
    f-string, never beside the binding. An f-string interpolation is a real `Name` load, so
    `f"{_DB}.helper"` counts and the alias it spells is correctly left alone.
    """
    return {
        node.id for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }


def _declared_exports(tree: ast.Module) -> set[str]:
    """String entries of a module-level `__all__`, which is a read by declaration."""
    exported: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets):
            continue
        exported |= {
            entry.value for entry in ast.walk(node)
            if isinstance(entry, ast.Constant) and isinstance(entry.value, str)
        }
    return exported


def _imported_across_suite(trees: dict[pathlib.Path, ast.Module]) -> set[tuple[str, str]]:
    """(module dotted path, name) pairs any file in the suite imports from another.

    Derived, not allowlisted, because the hazard is real and already present: `tests/unit/api/
    conftest.py` binds `SESSION_TOKEN` and never reads it — eight `test_*.py` files do, via
    `from tests.unit.api.conftest import SESSION_TOKEN`. Hardcoding that one name would leave
    the next shared constant to be reported as dead and deleted.
    """
    pairs: set[tuple[str, str]] = set()
    for tree in trees.values():
        for node in ast.walk(tree):
            # Relative imports carry no absolute module path to key on. There are none in this
            # suite today; if one appears it simply is not matched here, and the name it imports
            # would be reported — a visible failure to fix, never a silent miss.
            if isinstance(node, ast.ImportFrom) and node.module and not node.level:
                pairs |= {(node.module, alias.name) for alias in node.names}
    return pairs


def _dotted(path: pathlib.Path, root: pathlib.Path) -> str:
    """The import path `from X import ...` would name, relative to the import root."""
    return str(path.relative_to(root).with_suffix("")).replace("/", ".")


def _orphans(trees: dict[pathlib.Path, ast.Module], root: pathlib.Path) -> list[str]:
    """Module-level bindings nothing in the suite reads, as `path:line name` strings.

    `root` is the directory import paths are resolved against — the repo root for the real
    assertion, a `tmp_path` for the synthetic ones that prove this derivation fires.
    """
    imported = _imported_across_suite(trees)
    found: list[str] = []
    for path, tree in sorted(trees.items()):
        read = _names_read(tree)
        exported = _declared_exports(tree)
        dotted = _dotted(path, root)
        for name, line in sorted(_module_bindings(tree).items(), key=lambda kv: kv[1]):
            if name in _CONSUMED_BY_PYTEST or name in read or name in exported:
                continue
            if name.startswith("__") and name.endswith("__"):
                continue
            if (dotted, name) in imported:
                continue
            found.append(f"{path.relative_to(root)}:{line} {name}")
    return found


def _parse_tree(path: pathlib.Path) -> dict[pathlib.Path, ast.Module]:
    return {path: ast.parse(path.read_text(errors="ignore"))}


class TestNoOrphanedModuleGlobals:
    def test_the_derivation_actually_fires(self, tmp_path):
        """A guard nobody has watched fail is a guess — so make it fail, and pin what it spares.

        All four cases in one module because they interact: the orphan must be reported while
        `pytestmark` (pytest reads it), a name read only inside a method, and a name read only
        through an f-string are all left alone.
        """
        module = tmp_path / "test_sample.py"
        module.write_text(
            'import pytest\n'
            '\n'
            'pytestmark = pytest.mark.unit\n'
            '\n'
            '_ORPHAN = "cqc_lem.utilities.db"\n'
            '_USED_IN_METHOD = "cqc_lem.platform.db.connection"\n'
            '_USED_IN_FSTRING = "cqc_lem.utilities.logger"\n'
            '\n'
            'class TestThing:\n'
            '    def test_it(self):\n'
            '        assert _USED_IN_METHOD\n'
            '        assert f"{_USED_IN_FSTRING}.log_error"\n'
        )
        assert _orphans(_parse_tree(module), tmp_path) == ["test_sample.py:5 _ORPHAN"]

    def test_a_name_another_module_imports_is_not_an_orphan(self, tmp_path):
        """The `SESSION_TOKEN` shape: bound in one module, read only by importers of it."""
        shared = tmp_path / "conftest.py"
        shared.write_text('SESSION_TOKEN = "tok"\n')
        consumer = tmp_path / "test_consumer.py"
        consumer.write_text(
            f'from {_dotted(shared, tmp_path)} import SESSION_TOKEN\n'
            '\n'
            'def test_it():\n'
            '    assert SESSION_TOKEN\n'
        )
        trees = _parse_tree(shared) | _parse_tree(consumer)
        assert _orphans(trees, tmp_path) == []
        # The same binding with no importer IS an orphan — otherwise this test would pass on a
        # derivation that simply never reports anything.
        assert _orphans(_parse_tree(shared), tmp_path) == ["conftest.py:1 SESSION_TOKEN"]

    def test_no_test_module_binds_a_name_nothing_reads(self):
        """The real assertion, over the whole suite."""
        trees: dict[pathlib.Path, ast.Module] = {}
        for path in sorted(_TESTS.rglob("*.py")):
            if path.resolve() == pathlib.Path(__file__).resolve():
                continue
            try:
                trees[path] = ast.parse(path.read_text(errors="ignore"))
            except SyntaxError:  # a file this suite deliberately keeps unparseable is not ours
                continue
        orphans = _orphans(trees, _TESTS.parent)
        assert orphans == [], (
            "these bind a module-level name nothing reads — CodeQL files the `_`-prefixed ones as "
            "py/unused-global-variable, which fails the required CodeQL PR Quality Gate on the "
            "merge_group ref and evicts the PR from the merge queue. Delete the line:\n  "
            + "\n  ".join(orphans))
