"""There must be exactly ONE patchable definition of `get_db_connection`.

The split of `db.py` into `platform/db/` is only safe because of this. `utilities/db.py` re-exports
the name for the ~2,400 imports that reference it, but a re-export is a SECOND binding — and a test
that patches the re-export while the code reads the original gets a mock that is never consulted.
That failure is quiet: the function returns its documented fallback ([], None, False) and the test
asserts against it happily.

It bit for real during this split. 189 tests went red because `db.py`'s remaining 37 hand-written
functions called `get_db_connection()` through db.py's own binding, so patching the real definition
missed them. They call it through the MODULE now, which is what these tests pin.
"""

import ast
import importlib
import pathlib
import re

import pytest

pytestmark = pytest.mark.unit

_DB = pathlib.Path("src/cqc_lem/utilities/db.py")
_CANONICAL = "cqc_lem.platform.db.connection.get_db_connection"


def _facade_module_aliases(text: str) -> set[str]:
    """Local names this file binds to the facade MODULE itself, not to its path string.

    `from cqc_lem.utilities import db` and `import cqc_lem.utilities.db as db` both hand back an
    object whose attributes can be rebound with `patch.object`/`monkeypatch.setattr` — a facade
    patch that carries no path string for a regex to find.

    Read off the AST rather than matched line by line: a line-anchored regex cannot see the
    parenthesized form -- `from cqc_lem.utilities import (` with `db,` on the NEXT line -- which
    binds exactly the same object and would take the guard silently blind for that whole file. No
    test file spells it that way today, which is the reason to close it now rather than after one
    does.
    """
    names: set[str] = set()
    try:
        tree = ast.parse(text)
    except SyntaxError:  # pragma: no cover - every file this reads is importable Python
        return names
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {alias.asname for alias in node.names
                      if alias.name == "cqc_lem.utilities.db" and alias.asname}
        elif isinstance(node, ast.ImportFrom) and node.module == "cqc_lem.utilities":
            names |= {alias.asname or "db" for alias in node.names if alias.name == "db"}
    return names


class TestOneCanonicalTarget:
    def test_the_canonical_target_actually_resolves(self):
        """The path this suite tells everyone to patch must be a real, importable target.

        Without this, a later rename leaves the advice below pointing at nothing: every offending
        test gets told to patch a path that would raise `ModuleNotFoundError` if it obeyed.
        """
        module_path, _, attr = _CANONICAL.rpartition(".")
        module = importlib.import_module(module_path)
        assert callable(getattr(module, attr))

    def test_db_py_never_calls_the_re_export_directly(self):
        """A bare `get_db_connection()` in db.py reads the facade's copy, which patches miss."""
        src = _DB.read_text()
        bare = re.findall(r'(?<![.\w])get_db_connection\(\)', src)
        assert bare == [], (
            f"{len(bare)} bare call(s) in db.py — route them through `_connection.` so the one "
            "patchable definition is reached")

    def test_db_py_is_imports_and_dunder_all_only(self):
        """The end state of the split (issue #1614), and the strongest form of the seam rule.

        While db.py still ran SQL, the guard above was the best available: route its calls through
        `_connection.` so the one patchable definition is reached. Now that no statement is left
        here, there is nothing to route — so the rule becomes structural. A module that binds only
        imports and `__all__` cannot re-introduce a second call path, and a future function added
        back here would fail this before it could quietly acquire one.
        """
        tree = ast.parse(_DB.read_text())
        offenders = []
        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
                continue  # the module docstring
            if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets):
                continue
            offenders.append(f"{type(node).__name__} at line {node.lineno}")
        assert offenders == [], (
            "db.py is the facade: imports and `__all__`, nothing else. SQL belongs in "
            "platform/db/repositories/<aggregate>.py, shared vocabulary in platform/db/shared.py. "
            + ", ".join(offenders))

    def test_db_py_holds_no_sql(self):
        """Same claim from the other side — the text, not the shape.

        The AST check above would pass a module that imported a helper and called it in a
        comprehension inside `__all__`. This one is what the acceptance criterion on #1614 actually
        says: no cursor, no connection handle, no statement.
        """
        src = _DB.read_text()
        # `db_cursor` appears as a re-exported NAME and must keep doing so; what may not appear is
        # a CALL to it, a `_connection.` attribute read, or a statement in a string.
        found = [t for t in (r"\bdb_cursor\s*\(", r"_connection\.", r"cursor\.execute")
                 if re.search(t, src)]
        sql = [n.value for n in ast.walk(ast.parse(src))
               if isinstance(n, ast.Constant) and isinstance(n.value, str)
               and re.search(r"\b(SELECT|INSERT INTO|UPDATE|DELETE FROM)\b", n.value)]
        assert found == [] and sql == [], f"db.py still reaches the database: {found or sql}"

    # Everything `connection.py` owns. Reading these through the facade is fine — it is the same
    # object. REBINDING one on the facade is not: the real module keeps its own copy.
    MOVED = (
        "get_db_connection", "db_cursor", "to_naive_utc", "reset_connection_pool",
        "MYSQL_POOL_ENABLED", "MYSQL_POOL_SIZE", "_get_pooled_connection",
        "_get_connection_pool", "_get_mysql_config", "_POOL_STATE",
        "MYSQL_HOST", "MYSQL_USER", "MYSQL_PASSWORD", "MYSQL_DATABASE", "MYSQL_PORT",
    )

    @staticmethod
    def _repository_hazards(
            repo_dir: pathlib.Path | None = None) -> dict[str, dict[str, set[str]]]:
        """Moved symbols a facade patch would silently fail to intercept, derived not listed.

        Patching `cqc_lem.utilities.db.X` is normally FINE even after X moves: nearly every caller
        reaches X through the facade, so the re-export is the binding it reads. The exception is a
        caller that moved INTO the same repository module — it resolves X from its own module
        globals and never consults the facade, so the patch binds an object nobody calls and the
        test passes while asserting against real SQL.

        Deriving this per module means each new aggregate slice inherits the guard instead of
        depending on someone remembering to extend a tuple.
        """
        # sym -> EVERY module that reads it, not just one. Keyed one-to-one, `log_error` (read by
        # four repositories) collapsed to whichever sorted last, so a groups test went unflagged
        # because the check then looked for newsletter functions in it.
        hazards: dict[str, dict[str, set[str]]] = {}
        repo_dir = repo_dir or _DB.parent.parent / "platform" / "db" / "repositories"
        for mod in sorted(repo_dir.glob("*.py")):
            if mod.name == "__init__.py":
                continue
            tree = ast.parse(mod.read_text())
            defined = {
                n.name for n in tree.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            # Module-level constants are the same hazard wearing different clothes: a moved
            # function reads `CLAIM_STALE_MINUTES` out of its OWN globals, so rebinding the
            # facade's copy changes nothing the function will ever look at.
            #
            # IMPORTED names count for exactly the same reason, and this half was missed at first.
            # `groups.py` does `from cqc_lem.utilities.logger import log_error`; a test patching
            # `cqc_lem.utilities.db.log_error` then asserted against a mock the moved function
            # never consults. Anything in the module's global namespace is a hazard, however it
            # got there — defined here or imported here.
            for node in tree.body:
                if isinstance(node, ast.Assign):
                    defined |= {t.id for t in node.targets if isinstance(t, ast.Name)}
                elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                    defined.add(node.target.id)
                elif isinstance(node, (ast.Import, ast.ImportFrom)):
                    defined |= {(a.asname or a.name).split(".")[0] for a in node.names}
            for node in tree.body:
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for sub in ast.walk(node):
                    read = isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load)
                    if read and sub.id in defined and sub.id != node.name:
                        # Record WHICH function reads it, not just that the module does. The hazard
                        # is not "a test patches `get_user_id`" -- half this suite does that
                        # correctly, because `helper.py` and the db.py functions that stayed both
                        # read the facade. It is "a test exercises a function INSIDE this
                        # repository that reads the patched name from this module's globals".
                        # Flagging the looser condition retargeted 159 correct patches and broke
                        # 52 tests.
                        hazards.setdefault(sub.id, {}).setdefault(mod.stem, set()).add(node.name)
            # Reaching the reader INDIRECTLY is the same hazard (issue #1614). A test calls
            # `_admin_user_filters(...)`, which calls `_effective_admin_sql`, which is what actually
            # reads `admin_email_allowlist` off this module's globals — so the facade patch is just
            # as dead, and the "which function does the block exercise" half of the check below
            # never matched the name the test actually calls. Two of these shipped red.
            #
            # Closed over the intra-module call graph only. Following calls OUT of the module would
            # be the loose condition this file already warns about.
            calls: dict[str, set[str]] = {}
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    calls[node.name] = {
                        sub.func.id for sub in ast.walk(node)
                        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
                    }
            for sym, mods in hazards.items():
                readers = mods.get(mod.stem)
                if not readers:
                    continue
                while True:
                    grown = {f for f, called in calls.items() if called & readers} - readers
                    if not grown:
                        break
                    readers |= grown
        return hazards

    @staticmethod
    def _patches_facade(text: str, sym: str) -> bool:
        """Does this file patch `<facade>.sym`, spelled literally OR through a module alias?

        34 test files bind `_DB = "cqc_lem.utilities.db"` and then write `f"{_DB}.log_error"`. A
        regex for the literal path sees none of them — which is why the widened hazard set still
        missed the real `test_groups.py` failure, and why an earlier literal grep in this same
        split undercounted 844 patch targets across 83 files.
        """
        if re.search(rf'["\']cqc_lem\.utilities\.db\.{re.escape(sym)}["\']', text):
            return True
        aliases = re.findall(r'^(\w+)\s*=\s*["\']cqc_lem\.utilities\.db["\']', text, re.MULTILINE)
        return any(
            re.search(rf'\{{{re.escape(a)}\}}\.{re.escape(sym)}\b', text) for a in aliases
        )

    @staticmethod
    def _facade_patch_blocks(text: str, sym: str) -> list[str]:
        """The `with` block around each facade patch of `sym` — approximated by the next 12 lines.

        Every one of these follows the same shape in this suite: patch, import the function inside
        the block, call it, assert. Twelve lines covers that comfortably without spilling into the
        next test.
        """
        lines = text.splitlines()
        aliases = re.findall(r'^(\w+)\s*=\s*["\']cqc_lem\.utilities\.db["\']', text, re.M)
        patterns = [rf'["\']cqc_lem\.utilities\.db\.{re.escape(sym)}["\']']
        patterns += [rf'\{{{re.escape(a)}\}}\.{re.escape(sym)}\b' for a in aliases]
        # `patch.object(db, "sym")` is the same hazard with no path string in it, so neither pattern
        # above sees it (issue #1614). It shipped red: `test_flag_migrations.py` patched
        # `_select_engagement_row` on the facade while calling `get_engagement_preferences`, which
        # had moved into users.py and reads it from there. Match it through whatever local name
        # this file binds the facade MODULE to.
        for mod_alias in _facade_module_aliases(text):
            patterns.append(
                rf'(?:patch\.object|monkeypatch\.setattr)\(\s*{re.escape(mod_alias)}\s*,\s*'
                rf'["\']{re.escape(sym)}["\']')
        return [
            "\n".join(lines[i:i + 12])
            for i, line in enumerate(lines)
            if any(re.search(pat, line) for pat in patterns)
        ]

    def test_the_hazard_derivation_actually_fires(self, tmp_path):
        """The guard above is currently green because no repository has an intra-module call yet.

        A guard nobody has watched fail is a guess. This pins the derivation itself against a
        synthetic module, so the assertion stays meaningful for the aggregates still to be split.
        """
        (tmp_path / "widgets.py").write_text(
            "WIDGET_LIMIT = 5\n"
            "UNREAD_LIMIT = 9\n"
            "\n"
            "def read_widget(x):\n"
            "    return x\n"
            "\n"
            "def list_widgets():\n"
            "    return [read_widget(1)][:WIDGET_LIMIT]\n"
            "\n"
            "def untouched():\n"
            "    return None\n")
        hazards = self._repository_hazards(tmp_path)
        assert hazards == {"read_widget": {"widgets": {"list_widgets"}},
                           "WIDGET_LIMIT": {"widgets": {"list_widgets"}}}, (
            "an intra-module callee AND an intra-module constant read must both be flagged; a "
            "function nobody calls and a constant nobody reads must not be")

    def test_the_hazard_derivation_follows_intra_module_calls(self, tmp_path):
        """A test rarely calls the function that does the reading — it calls the one above it.

        `_admin_user_filters` -> `_effective_admin_sql` -> `admin_email_allowlist` is the real
        shape, and while the derivation stopped at the direct reader the block check looked for a
        name no test mentions. Two of these shipped red on #1614.
        """
        (tmp_path / "widgets.py").write_text(
            "WIDGET_LIMIT = 5\n"
            "\n"
            "def _clause():\n"
            "    return WIDGET_LIMIT\n"
            "\n"
            "def list_widgets():\n"
            "    return _clause()\n")
        hazards = self._repository_hazards(tmp_path)
        assert hazards["WIDGET_LIMIT"]["widgets"] == {"_clause", "list_widgets"}, (
            "the public entry point a test actually calls must be flagged too, not only the "
            "private helper that does the read")

    def test_patch_object_on_a_facade_alias_counts_as_a_facade_patch(self):
        """The spelling with no path string in it — `patch.object(db, "sym")`.

        `_facade_patch_blocks` matched only quoted `cqc_lem.utilities.db.<sym>` paths, so this one
        was invisible to the guard and shipped red in `test_flag_migrations.py`.
        """
        text = ('from cqc_lem.utilities import db\n'
                'with patch.object(db, "_select_engagement_row", return_value=None):\n'
                '    db.get_engagement_preferences(1)\n')
        assert _facade_module_aliases(text) == {"db"}
        assert _facade_module_aliases("import cqc_lem.utilities.db as thedb\n") == {"thedb"}
        assert _facade_module_aliases(
            "from cqc_lem.utilities import db as facade, logger\n") == {"facade"}
        # The parenthesized spelling binds the same object, and a line-anchored regex cannot see
        # past the opening paren — the guard would go quiet for the whole file, not just that line.
        assert _facade_module_aliases(
            "from cqc_lem.utilities import (\n    db,\n    logger,\n)\n") == {"db"}
        assert _facade_module_aliases("from cqc_lem.utilities import logger\n") == set()
        assert len(self._facade_patch_blocks(text, "_select_engagement_row")) == 1
        assert self._facade_patch_blocks(text, "get_engagement_preferences") == []

    def test_no_test_patches_an_intra_repository_call_on_the_facade(self):
        """Needs BOTH halves to fire, or it is noise.

        Patching `cqc_lem.utilities.db.datetime` is perfectly correct for a function still IN
        db.py, and half the repository modules import `datetime` too — so flagging every facade
        patch of a name some repository happens to import produces false positives that would get
        the whole assertion ignored. The hazard is a facade patch AND a target that moved, so the
        file must also exercise a function that module now owns.
        """
        offenders = []
        hazards = self._repository_hazards()
        for p in pathlib.Path("tests").rglob("*.py"):
            if p.name == "test_connection_seam.py":
                continue
            t = p.read_text(errors="ignore")
            for sym, mods in hazards.items():
                blocks = self._facade_patch_blocks(t, sym)
                if not blocks:
                    continue
                for mod, readers in sorted(mods.items()):
                    # The block must CALL a function that READS `sym` from this module's globals.
                    # Both halves were learned the hard way. Matching the whole file flagged a
                    # correct patch. Matching any moved function in the block still flagged 13
                    # files when only 5 were wrong — because `helper.py` and the db.py functions
                    # that stayed BOTH read `get_user_id` off the facade, so patching it there is
                    # right. Acting on the loose version retargeted 159 patches and broke 52 tests.
                    #
                    # `get_user_id` has exactly one reader inside users.py — `add_user_by_email` —
                    # and that is exactly the one test that genuinely failed.
                    exercised = sorted({
                        f for block in blocks for f in readers
                        if re.search(rf'\b{re.escape(f)}\s*\(', block)
                    })
                    if not exercised:
                        continue
                    offenders.append(
                        f"{p} -> patches {sym} on the facade while exercising "
                        f"{', '.join(exercised[:3])} from repositories/{mod}.py, which reads "
                        f"{sym} from its OWN globals; target "
                        f"cqc_lem.platform.db.repositories.{mod}.{sym}")
        assert offenders == [], "\n  ".join([""] + sorted(set(offenders)))

    def test_no_test_rebinds_a_moved_symbol_on_the_facade(self):
        """Covers BOTH lanes and all three rebinding styles.

        The unit lane caught `patch(...)` and `patch.object(...)`. It could NOT catch
        `monkeypatch.setattr(db, "MYSQL_POOL_ENABLED", True)` — that one shipped and turned up as a
        red INTEGRATION test, where the checkout handed back a direct CMySQLConnection because
        pooling was never actually enabled. This is the assertion that would have caught it.
        """
        offenders = []
        for p in pathlib.Path("tests").rglob("*.py"):
            if p.name == "test_connection_seam.py":
                continue
            t = p.read_text(errors="ignore")
            # A file may bind the local name `db` to the REAL module — those are correct, and the
            # tests that own the pool do exactly that. Only the facade binding is the hazard.
            if re.search(r'from cqc_lem\.platform\.db import connection as db\b', t):
                continue
            for sym in self.MOVED:
                if re.search(rf'["\']cqc_lem\.utilities\.db\.{re.escape(sym)}["\']', t):
                    offenders.append(f"{p} -> patch string for {sym}")
                if re.search(rf'patch\.object\(\s*db\s*,\s*["\']{re.escape(sym)}["\']', t):
                    offenders.append(f"{p} -> patch.object for {sym}")
                if re.search(rf'monkeypatch\.setattr\(\s*db\s*,\s*["\']{re.escape(sym)}["\']', t):
                    offenders.append(f"{p} -> monkeypatch.setattr for {sym}")
                # Plain assignment: the fourth style, and the one that actually shipped. The e2e
                # workflow test did `_db_mod.MYSQL_HOST = ...` against the facade and then called a
                # connect that reads connection.MYSQL_HOST, so its host override pointed nowhere.
                # No mock, no patch call, nothing for the three checks above to match on.
                if re.search(rf'^\s*\w*db\w*\.{re.escape(sym)}\s*=[^=]', t, re.MULTILINE):
                    offenders.append(f"{p} -> direct assignment to {sym}")
        assert offenders == [], (
            "these rebind a moved symbol on the re-export, which the real module never reads — "
            f"target {_CANONICAL.rpartition('.')[0]} instead:\n  "
            + "\n  ".join(sorted(set(offenders))))

    def test_the_facade_still_re_exports_it(self):
        """Compatibility half: ~2,400 imports still say `from cqc_lem.utilities.db import ...`."""
        from cqc_lem.platform.db.connection import get_db_connection as real
        from cqc_lem.utilities.db import get_db_connection as viaFacade
        assert viaFacade is real

    def test_the_facade_re_exports_every_enum(self):
        from cqc_lem.platform.db import enums
        from cqc_lem.utilities import db
        # Only the enums this module DEFINES. `dir()` also sees what it imports — `StrEnum` itself
        # is in there, and asserting the facade re-exports a stdlib base class held db.py to
        # keeping an import it no longer uses.
        names = [
            n for n in dir(enums)
            if isinstance(getattr(enums, n), type) and getattr(enums, n).__module__ == enums.__name__
        ]
        missing = [n for n in names if getattr(db, n, None) is not getattr(enums, n)]
        assert missing == [], f"facade does not re-export: {missing}"

    def test_the_pool_flag_is_read_where_the_conftest_patches_it(self):
        """tests/conftest.py disables pooling on `connection`; it must be read there."""
        conn_src = pathlib.Path("src/cqc_lem/platform/db/connection.py").read_text()
        tree = ast.parse(conn_src)
        fn = next(n for n in tree.body
                  if isinstance(n, ast.FunctionDef) and n.name == "get_db_connection")
        names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
        assert "MYSQL_POOL_ENABLED" in names
