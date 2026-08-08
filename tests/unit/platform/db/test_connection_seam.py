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

    def test_db_py_routes_through_the_module(self):
        src = _DB.read_text()
        assert "_connection.get_db_connection()" in src
        assert "from cqc_lem.platform.db import connection as _connection" in src

    # Everything `connection.py` owns. Reading these through the facade is fine — it is the same
    # object. REBINDING one on the facade is not: the real module keeps its own copy.
    MOVED = (
        "get_db_connection", "db_cursor", "to_naive_utc", "reset_connection_pool",
        "MYSQL_POOL_ENABLED", "MYSQL_POOL_SIZE", "_get_pooled_connection",
        "_get_connection_pool", "_get_mysql_config", "_POOL_STATE",
        "MYSQL_HOST", "MYSQL_USER", "MYSQL_PASSWORD", "MYSQL_DATABASE", "MYSQL_PORT",
    )

    @staticmethod
    def _repository_hazards(repo_dir: pathlib.Path | None = None) -> dict[str, set[str]]:
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
        hazards: dict[str, set[str]] = {}
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
                        hazards.setdefault(sub.id, set()).add(mod.stem)
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
        return [
            "\n".join(lines[i:i + 12])
            for i, line in enumerate(lines)
            if any(re.search(pat, line) for pat in patterns)
        ]

    @staticmethod
    def _repository_functions(repo_dir: pathlib.Path | None = None) -> dict[str, set[str]]:
        """Which functions each repository module now owns."""
        owned: dict[str, set[str]] = {}
        repo_dir = repo_dir or _DB.parent.parent / "platform" / "db" / "repositories"
        for mod in sorted(repo_dir.glob("*.py")):
            if mod.name == "__init__.py":
                continue
            owned[mod.stem] = {
                n.name for n in ast.parse(mod.read_text()).body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
        return owned

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
        assert hazards == {"read_widget": {"widgets"}, "WIDGET_LIMIT": {"widgets"}}, (
            "an intra-module callee AND an intra-module constant read must both be flagged; a "
            "function nobody calls and a constant nobody reads must not be")

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
        owned = self._repository_functions()
        for p in pathlib.Path("tests").rglob("*.py"):
            if p.name == "test_connection_seam.py":
                continue
            t = p.read_text(errors="ignore")
            for sym, mods in hazards.items():
                blocks = self._facade_patch_blocks(t, sym)
                if not blocks:
                    continue
                for mod in sorted(mods):
                    # Scoped to the `with patch(...)` block, not the file. A test module routinely
                    # patches log_error for a function still IN db.py while mentioning a moved one
                    # somewhere else entirely — file-level matching called that an offence and
                    # would have had me "fix" a correct patch into a broken one.
                    exercised = sorted({
                        f for block in blocks for f in owned[mod]
                        if re.search(rf'\b{re.escape(f)}\b', block)
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
