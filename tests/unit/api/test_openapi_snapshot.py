"""The committed schema snapshot is what the app actually publishes (issue #1446).

The SPA's TypeScript types are generated from `src/cqc_lem/ui/openapi.json`, which is dumped off
the app by `scripts/dump_openapi.py` and committed. A committed generator INPUT is only worth
anything if something fails when it goes stale — otherwise the SPA keeps compiling against the
shape the API had months ago, which is exactly the hand-maintained-types failure this replaced.

Both links of the chain are checked from here, in the REQUIRED Unit Tests lane:

* **app → snapshot** is exact — the bytes are re-rendered and compared.
* **snapshot → `src/api/schema.ts`** is checked through the stamp the generator writes
  (`schema.stamp.json`), because this lane has no node and cannot re-run the generator. That
  catches the mistake that actually happens — the snapshot regenerated, `npm run gen:api-types`
  forgotten — plus the two hand-edit signals below. `npm run check:api-types` re-runs the
  generator for an exact answer wherever node is available.
"""

import hashlib
import importlib.util
import json
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[3]
_UI = _REPO_ROOT / "src" / "cqc_lem" / "ui"
_SNAPSHOT = _UI / "openapi.json"
_GENERATED_TS = _UI / "src" / "api" / "schema.ts"
_STAMP = _UI / "src" / "api" / "schema.stamp.json"
_PACKAGE_JSON = _UI / "package.json"

_REGEN = ("run `poetry run python scripts/dump_openapi.py` and then "
          "`npm run gen:api-types` in src/cqc_lem/ui")


def _published() -> dict:
    from cqc_lem.api.main import app

    return app.openapi()


@pytest.fixture(scope="module")
def dump_tool():
    """`scripts/dump_openapi.py`, loaded by path — `scripts/` is not a package."""
    script = _REPO_ROOT / "scripts" / "dump_openapi.py"
    spec = importlib.util.spec_from_file_location("dump_openapi", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestTheDumpToolWritesWhatIsCommitted:
    """The script and this test have to agree on the rendering, or `--check` lies to a developer."""

    def test_render_matches_the_committed_snapshot(self, dump_tool):
        assert dump_tool.render_schema() == _SNAPSHOT.read_text()

    def test_it_writes_where_the_generator_reads(self, dump_tool):
        assert dump_tool.SNAPSHOT_PATH == _SNAPSHOT

    def test_check_mode_passes_on_a_current_snapshot(self, dump_tool, monkeypatch):
        monkeypatch.setattr("sys.argv", ["dump_openapi.py", "--check"])
        assert dump_tool.main() == 0

    def test_check_mode_fails_on_a_stale_one(self, dump_tool, monkeypatch, tmp_path):
        """Anti-vacuity: `--check` returning 0 unconditionally would pass the test above."""
        stale = tmp_path / "openapi.json"
        stale.write_text('{"openapi": "3.1.0"}\n')
        monkeypatch.setattr(dump_tool, "SNAPSHOT_PATH", stale)
        monkeypatch.setattr("sys.argv", ["dump_openapi.py", "--check"])
        assert dump_tool.main() == 1


class TestTheSnapshotMatchesTheApp:
    def test_the_snapshot_is_byte_for_byte_what_the_dump_writes(self):
        """Text, not just parsed equality.

        `openapi-typescript` reads this file, so a formatting change is a diff in the generated
        TypeScript too. Comparing the bytes keeps one canonical rendering instead of two that
        happen to parse the same.
        """
        expected = json.dumps(_published(), indent=2, sort_keys=True) + "\n"
        assert _SNAPSHOT.read_text() == expected, f"the committed schema is stale — {_REGEN}"

    def test_the_snapshot_is_not_empty(self):
        """Anti-vacuity: an empty document would satisfy any comparison against itself."""
        schema = json.loads(_SNAPSHOT.read_text())
        assert len(schema["paths"]) > 100
        assert schema["components"]["schemas"]

    def test_no_admin_operation_reached_the_snapshot(self):
        """#1219's watch item, re-derived here.

        `_hide_admin_routes_from_schema()` keeps every `/api/admin/*` operation out of the
        published document. A dump taken by a different route — importing `app` before the hiding
        runs, or rebuilding from the route table — would re-publish all 18 into a file that is now
        committed and shipped to the browser as types.
        """
        from cqc_lem.api.main import _ADMIN_ROUTES_HIDDEN

        schema = json.loads(_SNAPSHOT.read_text())
        assert not [p for p in schema["paths"] if p.startswith("/api/admin")]
        assert _ADMIN_ROUTES_HIDDEN >= 18


class TestTheGeneratedTypesTrackTheSnapshot:
    def test_the_types_were_generated_from_the_committed_snapshot(self):
        """The stamp is the only thing this lane can compare — it has no node to regenerate with.

        It answers the question that matters: was `schema.ts` produced from THIS `openapi.json`?
        Regenerating one without the other leaves the SPA compiling against a shape the API no
        longer serves, and nothing else in CI would say so.
        """
        stamp = json.loads(_STAMP.read_text())
        assert stamp["source_sha256"] == hashlib.sha256(_SNAPSHOT.read_bytes()).hexdigest(), (
            f"src/api/schema.ts was generated from a different schema — {_REGEN}")

    def test_the_stamp_names_the_generator_package_json_pins(self):
        """A stamp written by some other generator would make the check above meaningless."""
        scripts = json.loads(_PACKAGE_JSON.read_text())["scripts"]
        pinned = re.search(r"openapi-typescript@[\d.]+", scripts["gen:api-types"])
        assert pinned, "gen:api-types no longer pins a generator version"
        assert json.loads(_STAMP.read_text())["generator"] == pinned.group(0)

    def test_every_component_name_appears_in_the_generated_types(self):
        generated = _GENERATED_TS.read_text()
        schema = json.loads(_SNAPSHOT.read_text())
        missing = [name for name in schema["components"]["schemas"] if name not in generated]
        assert not missing, (
            f"src/api/schema.ts does not mention {missing[:5]} — it is stale against the "
            f"committed snapshot; {_REGEN}"
        )

    def test_the_generated_file_says_it_is_generated(self):
        """Anti-vacuity for the check above, and the one line that keeps someone from editing it."""
        assert "auto-generated by openapi-typescript" in _GENERATED_TS.read_text()
