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
import pathlib
import re

import pytest

pytestmark = pytest.mark.unit

_DB = pathlib.Path("src/cqc_lem/utilities/db.py")
_CANONICAL = "cqc_lem.platform.db.connection.get_db_connection"


class TestOneCanonicalTarget:
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
    )

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
        assert offenders == [], (
            "these rebind a moved symbol on the re-export, which the real module never reads — "
            "target cqc_lem.platform.db.connection instead:\n  " + "\n  ".join(sorted(set(offenders))))

    def test_the_facade_still_re_exports_it(self):
        """Compatibility half: ~2,400 imports still say `from cqc_lem.utilities.db import ...`."""
        from cqc_lem.platform.db.connection import get_db_connection as real
        from cqc_lem.utilities.db import get_db_connection as viaFacade
        assert viaFacade is real

    def test_the_facade_re_exports_every_enum(self):
        from cqc_lem.platform.db import enums
        from cqc_lem.utilities import db
        names = [n for n in dir(enums) if n[0].isupper() and not n.startswith("_")]
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
