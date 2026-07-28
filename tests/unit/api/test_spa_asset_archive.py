"""Retention of previously-deployed SPA asset bundles (issue #743).

A tab open across a deploy asks for the PREVIOUS build's hashed lazy chunks. The archive is what
makes those keep resolving; the SPA reload is only the fallback for anything older than it.
"""

import json
import os
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.applications import Starlette

from cqc_lem.api.spa_assets import (
    ARCHIVE_DIR_ENV,
    ARCHIVE_KEEP_ENV,
    DEFAULT_KEEP,
    IMMUTABLE_CACHE_CONTROL,
    MANIFEST_NAME,
    NO_STORE_CACHE_CONTROL,
    ArchivedStaticFiles,
    archive_keep,
    archived_asset_path,
    spa_index_headers,
    sync_build_to_archive,
)

pytestmark = pytest.mark.unit

PROD_OVERLAY = (
    Path(__file__).resolve().parents[3] / "docker-compose.prod.yml"
).read_text()


def _service_block(compose: str, name: str) -> str:
    # Compose's own `!reset`/`!override` tags are not YAML a safe loader parses, so the overlay is
    # read as text (same approach as tests/unit/app/test_selenium_capacity.py).
    return re.split(r"\n  (?=\w)", compose.split(f"\n  {name}:")[1])[0]


def _write_build(dist: str, files: dict) -> str:
    """Write a `dist/assets` directory with the given {name: content} and return its path."""
    assets = os.path.join(dist, "assets")
    os.makedirs(assets, exist_ok=True)
    for name, body in files.items():
        with open(os.path.join(assets, name), "w") as fh:
            fh.write(body)
    return assets


def _archived_names(archive: str) -> set:
    return {n for n in os.listdir(archive) if n != MANIFEST_NAME and not n.startswith(".")}


@pytest.fixture
def archive(tmp_path, monkeypatch) -> str:
    path = str(tmp_path / "archive")
    monkeypatch.setenv(ARCHIVE_DIR_ENV, path)
    return path


class TestSync:
    def test_off_without_the_env_var(self, tmp_path, monkeypatch):
        monkeypatch.delenv(ARCHIVE_DIR_ENV, raising=False)
        assets = _write_build(str(tmp_path / "v1"), {"app-aaa.js": "one"})
        assert sync_build_to_archive(assets) is None

    def test_archives_the_current_build(self, tmp_path, archive):
        assets = _write_build(str(tmp_path / "v1"), {"app-aaa.js": "one", "app-aaa.css": "css"})
        manifest = sync_build_to_archive(assets)
        assert manifest is not None
        assert _archived_names(archive) == {"app-aaa.js", "app-aaa.css"}
        assert manifest["generations"][0]["files"] == ["app-aaa.css", "app-aaa.js"]

    def test_keeps_the_previous_builds_chunks(self, tmp_path, archive):
        sync_build_to_archive(_write_build(str(tmp_path / "v1"), {"jszip-OLD.js": "old"}))
        sync_build_to_archive(_write_build(str(tmp_path / "v2"), {"jszip-NEW.js": "new"}))
        # The pre-deploy tab's hash is still resolvable — that is the entire point.
        assert _archived_names(archive) == {"jszip-OLD.js", "jszip-NEW.js"}

    def test_prunes_beyond_the_retention_window(self, tmp_path, archive, monkeypatch):
        monkeypatch.setenv(ARCHIVE_KEEP_ENV, "2")
        for i in range(4):
            sync_build_to_archive(_write_build(str(tmp_path / f"v{i}"), {f"app-{i}.js": str(i)}))
        assert _archived_names(archive) == {"app-2.js", "app-3.js"}
        with open(os.path.join(archive, MANIFEST_NAME)) as fh:
            assert len(json.load(fh)["generations"]) == 2

    def test_a_shared_chunk_survives_pruning_its_first_build(self, tmp_path, archive, monkeypatch):
        # Most chunks do not change between releases; their hash is identical, so a retained
        # generation still references the file and pruning an older one must not delete it.
        monkeypatch.setenv(ARCHIVE_KEEP_ENV, "2")
        for i in range(3):
            sync_build_to_archive(
                _write_build(str(tmp_path / f"v{i}"), {"vendor-STABLE.js": "v", f"app-{i}.js": str(i)})
            )
        assert "vendor-STABLE.js" in _archived_names(archive)

    def test_a_restart_is_not_a_new_generation(self, tmp_path, archive):
        assets = _write_build(str(tmp_path / "v1"), {"app-aaa.js": "one"})
        sync_build_to_archive(assets)
        manifest = sync_build_to_archive(assets)
        assert len(manifest["generations"]) == 1

    def test_a_redeploy_of_an_older_build_is_not_duplicated(self, tmp_path, archive):
        # Rollback: deploy.sh restores PREV_TAG, whose file list the archive already knows.
        v1 = _write_build(str(tmp_path / "v1"), {"app-aaa.js": "one"})
        v2 = _write_build(str(tmp_path / "v2"), {"app-bbb.js": "two"})
        sync_build_to_archive(v1)
        sync_build_to_archive(v2)
        manifest = sync_build_to_archive(v1)
        ids = [g["id"] for g in manifest["generations"]]
        assert len(ids) == len(set(ids)) == 2
        assert _archived_names(archive) == {"app-aaa.js", "app-bbb.js"}

    def test_survives_a_corrupt_manifest(self, tmp_path, archive):
        os.makedirs(archive, exist_ok=True)
        with open(os.path.join(archive, MANIFEST_NAME), "w") as fh:
            fh.write("{not json")
        manifest = sync_build_to_archive(_write_build(str(tmp_path / "v1"), {"app-aaa.js": "one"}))
        assert manifest["generations"][0]["files"] == ["app-aaa.js"]

    def test_never_raises_when_the_archive_is_unusable(self, tmp_path, monkeypatch):
        # A read-only volume must degrade to the SPA reload path, not stop the API from starting.
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("")
        monkeypatch.setenv(ARCHIVE_DIR_ENV, str(blocker))
        assert sync_build_to_archive(_write_build(str(tmp_path / "v1"), {"a-1.js": "x"})) is None

    def test_no_dist_is_a_no_op(self, tmp_path, archive):
        assert sync_build_to_archive(str(tmp_path / "missing")) is None

    def test_keep_floor_is_two(self, monkeypatch):
        monkeypatch.setenv(ARCHIVE_KEEP_ENV, "1")
        assert archive_keep() == 2  # keeping only the live build archives nothing
        monkeypatch.setenv(ARCHIVE_KEEP_ENV, "nonsense")
        assert archive_keep() == DEFAULT_KEEP


class TestArchivedAssetPath:
    def test_resolves_an_archived_chunk(self, tmp_path, archive):
        sync_build_to_archive(_write_build(str(tmp_path / "v1"), {"app-aaa.js": "one"}))
        assert archived_asset_path("app-aaa.js") == os.path.realpath(
            os.path.join(archive, "app-aaa.js")
        )

    def test_unknown_name_is_none(self, tmp_path, archive):
        sync_build_to_archive(_write_build(str(tmp_path / "v1"), {"app-aaa.js": "one"}))
        assert archived_asset_path("app-zzz.js") is None

    @pytest.mark.parametrize("attempt", [
        "../../etc/passwd",
        "..%2f..%2fetc/passwd/..",
        "sub/../../outside.js",
        "/etc/passwd",
        "",
        MANIFEST_NAME,
    ])
    def test_rejects_traversal_and_the_manifest(self, tmp_path, archive, attempt):
        sync_build_to_archive(_write_build(str(tmp_path / "v1"), {"app-aaa.js": "one"}))
        (tmp_path / "outside.js").write_text("secret")
        assert archived_asset_path(attempt) is None

    def test_none_when_archiving_is_off(self, monkeypatch):
        monkeypatch.delenv(ARCHIVE_DIR_ENV, raising=False)
        assert archived_asset_path("app-aaa.js") is None


class TestArchivedStaticFiles:
    """The mount's contract: live bundle first, archive as the fallback, immutable either way."""

    def _client(self, assets_dir: str) -> TestClient:
        app = Starlette()
        app.mount("/assets", ArchivedStaticFiles(directory=assets_dir), name="spa-assets")
        return TestClient(app)

    def test_serves_the_live_bundle_immutably(self, tmp_path, archive):
        assets = _write_build(str(tmp_path / "v1"), {"app-aaa.js": "live"})
        sync_build_to_archive(assets)
        resp = self._client(assets).get("/assets/app-aaa.js")
        assert resp.status_code == 200
        assert resp.text == "live"
        assert resp.headers["cache-control"] == IMMUTABLE_CACHE_CONTROL

    def test_serves_a_pre_deploy_chunk_from_the_archive(self, tmp_path, archive):
        old = _write_build(str(tmp_path / "v1"), {"jszip.min-OLD.js": "old chunk"})
        sync_build_to_archive(old)
        new = _write_build(str(tmp_path / "v2"), {"jszip.min-NEW.js": "new chunk"})
        sync_build_to_archive(new)

        resp = self._client(new).get("/assets/jszip.min-OLD.js")
        assert resp.status_code == 200
        assert resp.text == "old chunk"
        # A content-hashed name resolves to one file forever, so the immutable contract holds.
        assert resp.headers["cache-control"] == IMMUTABLE_CACHE_CONTROL

    def test_a_genuinely_unknown_chunk_still_404s(self, tmp_path, archive):
        assets = _write_build(str(tmp_path / "v1"), {"app-aaa.js": "live"})
        sync_build_to_archive(assets)
        assert self._client(assets).get("/assets/app-never-shipped.js").status_code == 404

    def test_404s_with_archiving_off(self, tmp_path, monkeypatch):
        monkeypatch.delenv(ARCHIVE_DIR_ENV, raising=False)
        assets = _write_build(str(tmp_path / "v1"), {"app-aaa.js": "live"})
        assert self._client(assets).get("/assets/app-gone.js").status_code == 404


class TestIndexHeaders:
    """The HTML shell is the one thing that must never be cached — an archived chunk is only
    useful if the reload that fetches the new shell actually gets the new shell."""

    def test_index_is_never_cached(self):
        headers = spa_index_headers()
        assert headers["Cache-Control"] == NO_STORE_CACHE_CONTROL
        for directive in ("no-store", "no-cache", "must-revalidate", "max-age=0"):
            assert directive in headers["Cache-Control"]
        assert headers["Pragma"] == "no-cache"

    def test_the_shell_and_the_assets_have_opposite_contracts(self):
        assert "no-store" not in IMMUTABLE_CACHE_CONTROL
        assert "immutable" in IMMUTABLE_CACHE_CONTROL
        assert "immutable" not in NO_STORE_CACHE_CONTROL


class TestProdComposeWiring:
    """Nothing in the app fails when the volume is missing — retention just silently stops, and
    the only symptom is the failure this issue is about coming back. Only these assertions catch it.
    """

    MOUNT = "/app/spa_asset_archive"

    def test_the_active_color_mounts_and_configures_the_archive(self):
        blue = _service_block(PROD_OVERLAY, "web_api_blue")
        assert f"- spa_asset_archive:{self.MOUNT}" in blue
        assert f"- SPA_ASSET_ARCHIVE_DIR={self.MOUNT}" in blue

    def test_the_standby_color_inherits_the_same_archive(self):
        # Both colors serve traffic across a deploy, so a green that missed the volume would 404
        # exactly the chunks blue could still serve.
        green = _service_block(PROD_OVERLAY, "web_api_green")
        assert "<<: *web_api" in green or f"- spa_asset_archive:{self.MOUNT}" in green

    def test_the_volume_is_named_and_therefore_outlives_the_image(self):
        # A bind mount into the release checkout would be replaced along with the build.
        assert re.search(r"^volumes:\n(?:.*\n)*?  spa_asset_archive:", PROD_OVERLAY, re.M)
