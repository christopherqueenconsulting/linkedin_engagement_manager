"""Retention of previously-deployed SPA asset bundles (issue #743).

A browser tab holds the hashed chunk filenames of the build it loaded. Releases batch 4x daily, so
a tab open across one asks for `jszip.min-BZWDyCXg.js` while the image on disk only has
`jszip.min-B8bT8jVo.js` — the lazy chunk 404s and the feature reads as broken. The SPA recovers with
one reload (`utils/chunkReload.ts`); this is the half that means most users never see even that.

Every deployed build syncs its `dist/assets` into a shared archive directory on a volume that
outlives the image, and a miss in the live bundle falls back to the archive. Serving an old hash is
always safe: the filenames are content-hashed, so a name resolves to exactly one file forever — the
`immutable` cache contract is preserved rather than weakened.

OFF unless ``SPA_ASSET_ARCHIVE_DIR`` names a directory: dev and CI serve the live bundle only.
"""

import hashlib
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.responses import FileResponse, Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

from cqc_lem.utilities.logger import log_debug, log_info, log_warning

ARCHIVE_DIR_ENV = "SPA_ASSET_ARCHIVE_DIR"
ARCHIVE_KEEP_ENV = "SPA_ASSET_ARCHIVE_KEEP"
DEFAULT_KEEP = 5
MANIFEST_NAME = "manifest.json"

IMMUTABLE_CACHE_CONTROL = "public, max-age=31536000, immutable"
# The HTML shell references hashed asset filenames, so it must NEVER be cached by browsers or the
# CDN — otherwise a stale shell points at an old bundle after every deploy, and the one reload the
# SPA falls back on would land on the same broken build. (Cloudflare must respect this; if a
# "Cache Everything" rule overrides it, the rule needs an HTML bypass.)
NO_STORE_CACHE_CONTROL = "no-store, no-cache, must-revalidate, max-age=0"

# Crawler-facing files (issue #1298). The SPA catch-all in api/main answers EVERY unclaimed path
# with index.html, so a file being present in dist/ does NOT mean it is served: without these
# routes /robots.txt, /sitemap.xml and the Open Graph image all come back as HTML, and a link
# preview whose og:image resolves to an HTML document renders blank — the exact defect #1298 exists
# to fix. Values are the content type a crawler must see.
PUBLIC_ROOT_FILES: Dict[str, str] = {
    "robots.txt": "text/plain; charset=utf-8",
    "sitemap.xml": "application/xml",
    # The brand package ships raster only (PR #1294) — PNG, PDF and ICO, no SVG — so since issue
    # #1300 the mark is an .ico rather than the purple template favicon.svg it replaced.
    "favicon.ico": "image/x-icon",
}

# Vite copies public/ VERBATIM — no env substitution reaches it — but a sitemap `<loc>` and a
# robots.txt `Sitemap:` line are only valid as ABSOLUTE URLs. So those two files carry this token
# and the server fills it in at serve time, where the canonical host is actually known.
BASE_URL_PLACEHOLDER = "%PUBLIC_BASE_URL%"
TEMPLATED_ROOT_FILES = frozenset({"robots.txt", "sitemap.xml"})

# The build-time twin, in index.html. Vite leaves an UNDEFINED %VITE_*% token untouched, and the
# image build deliberately unsets an empty VITE_PUBLIC_BASE_URL rather than let Vite bake "" into
# a relative og:image — so a build made without the repo variable still serves absolute Open Graph
# URLs, filled from PUBLIC_BASE_URL here.
VITE_BASE_URL_PLACEHOLDER = "%VITE_PUBLIC_BASE_URL%"


def public_base_url(fallback: str = "") -> str:
    """The absolute origin crawler-facing markup is written against, without a trailing slash.

    `PUBLIC_BASE_URL` is the configured answer — the same env the OAuth redirect and the WebAuthn
    relying party derive from, so there is one canonical host per deploy. The fallback is the
    request's own origin, which keeps dev (and any deploy that never set the variable) serving
    absolute URLs rather than the relative ones LinkedIn's and Twitter's crawlers ignore.
    """
    configured = (os.getenv("PUBLIC_BASE_URL", "") or "").strip()
    return (configured or fallback or "").rstrip("/")


def render_base_url(raw: str, base_url: str, placeholder: str = BASE_URL_PLACEHOLDER) -> str:
    """Substitute the canonical-host token in a text file about to be served."""
    return raw.replace(placeholder, base_url)


def _make_public_file_route(dist_dir: str, file_name: str, media_type: str) -> Any:
    """Build the handler serving one crawler-facing file out of `dist_dir`."""

    async def _serve_public_file(request: Request) -> Response:
        """Serve the file, filling the canonical-host token when it carries one."""
        path = os.path.join(dist_dir, file_name)
        if not os.path.isfile(path):
            # An SPA build that shipped without the file: a 404 is the honest answer, and it is
            # what stops a crawler reading index.html as a robots.txt.
            raise StarletteHTTPException(status_code=404)
        if file_name not in TEMPLATED_ROOT_FILES:
            return FileResponse(path, media_type=media_type)
        with open(path, encoding="utf-8") as fh:
            body = render_base_url(fh.read(), public_base_url(str(request.base_url)))
        return Response(content=body, media_type=media_type)

    return _serve_public_file


def register_spa_public_routes(app: Any, dist_dir: str) -> None:
    """Serve the crawler-facing files of a built SPA, ahead of the HTML catch-all.

    Registration ORDER is the contract: routes match in the order they are added, so every one of
    these must exist before `/{full_path:path}` or index.html answers for all of them.
    """
    for file_name, media_type in PUBLIC_ROOT_FILES.items():
        app.get(f"/{file_name}", include_in_schema=False)(
            _make_public_file_route(dist_dir, file_name, media_type)
        )
    brand_dir = os.path.join(dist_dir, "brand")
    if os.path.isdir(brand_dir):
        # The Open Graph image lives here. Static, public, and referenced by absolute URL from the
        # shell, so it is mounted rather than routed file by file.
        app.mount("/brand", StaticFiles(directory=brand_dir), name="spa-brand")
    else:
        log_debug("No SPA brand directory to serve", dist_dir=dist_dir)


def spa_index_headers() -> Dict[str, str]:
    """The response headers every serving of the HTML shell must carry.

    One place, because a shell served from cache is the failure this whole module exists to
    contain — see the note on NO_STORE_CACHE_CONTROL for why. `Pragma` is there for the
    intermediaries that still only honour HTTP/1.0.
    """
    return {"Cache-Control": NO_STORE_CACHE_CONTROL, "Pragma": "no-cache"}


def archive_dir() -> Optional[str]:
    """The archive root, or None when retention is off.

    None is the whole feature's OFF switch (dev and CI), so every entry point checks it first
    rather than assuming a directory exists. A blank or whitespace-only value counts as unset.
    """
    return (os.getenv(ARCHIVE_DIR_ENV, "") or "").strip() or None


def archive_keep() -> int:
    """How many builds' assets to retain. One is meaningless (that is just the live bundle)."""
    raw = (os.getenv(ARCHIVE_KEEP_ENV, "") or "").strip()
    try:
        keep = int(raw) if raw else DEFAULT_KEEP
    except ValueError:
        keep = DEFAULT_KEEP
    return max(2, keep)


def _relative_files(root: str) -> List[str]:
    found: List[str] = []
    for dir_path, _dirs, file_names in os.walk(root):
        for name in file_names:
            rel = os.path.relpath(os.path.join(dir_path, name), root)
            found.append(rel.replace(os.sep, "/"))
    return sorted(found)


def _build_id(rel_files: List[str]) -> str:
    """Identify a build by its own file list — available with or without an IMAGE_TAG, and
    identical across a re-deploy of the same image, which is what makes the sync idempotent.
    """
    digest = hashlib.sha256()
    for rel in rel_files:
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()[:16]


def _manifest_path(archive: str) -> str:
    return os.path.join(archive, MANIFEST_NAME)


def _read_manifest(archive: str) -> Dict[str, Any]:
    try:
        with open(_manifest_path(archive)) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {"generations": []}
    generations = data.get("generations")
    if not isinstance(generations, list):
        return {"generations": []}
    return {"generations": [g for g in generations if isinstance(g, dict)]}


def _write_manifest(archive: str, manifest: Dict[str, Any]) -> None:
    """Atomic replace: a half-written manifest read by the other color would look like an empty
    archive and prune every retained build.
    """
    fd, tmp = tempfile.mkstemp(dir=archive, prefix=".manifest-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(manifest, fh)
        os.replace(tmp, _manifest_path(archive))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError as e:
            log_debug("Could not remove temporary manifest file", exc=e, archive=archive)
        raise


def _copy_asset(src: str, archive: str, rel: str) -> None:
    dest = os.path.join(archive, rel)
    if os.path.exists(dest):
        return
    os.makedirs(os.path.dirname(dest) or archive, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dest) or archive, prefix=".sync-")
    os.close(fd)
    try:
        # Copy to a temp name first: the other color may be serving this directory, and a partially
        # written chunk is worse than a missing one (the browser caches it `immutable`).
        shutil.copyfile(src, tmp)
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError as e:
            log_debug("Could not remove temporary asset file", exc=e, tmp=tmp)
        raise


def _prune(archive: str, manifest: Dict[str, Any]) -> int:
    retained = {rel for gen in manifest["generations"] for rel in gen.get("files", [])}
    removed = 0
    for rel in _relative_files(archive):
        if rel == MANIFEST_NAME or rel in retained or os.path.basename(rel).startswith("."):
            continue
        try:
            os.unlink(os.path.join(archive, rel))
            removed += 1
        except OSError as e:
            log_debug("Could not prune archived asset", exc=e, rel=rel)
    return removed


def sync_build_to_archive(dist_assets_dir: str) -> Optional[Dict[str, Any]]:
    """Add this build's assets to the archive and prune past the retention window.

    Returns the manifest, or None when archiving is off / unusable. Never raises: an archive
    problem must degrade to the reload path, never keep the API from starting.
    """
    archive = archive_dir()
    if not archive or not os.path.isdir(dist_assets_dir):
        return None
    try:
        os.makedirs(archive, exist_ok=True)
        rel_files = _relative_files(dist_assets_dir)
        if not rel_files:
            return None
        build_id = _build_id(rel_files)

        manifest = _read_manifest(archive)
        generations = manifest["generations"]
        if generations and generations[0].get("id") == build_id:
            return manifest  # already the newest generation — a restart, not a deploy

        for rel in rel_files:
            _copy_asset(os.path.join(dist_assets_dir, rel), archive, rel)

        generations = [g for g in generations if g.get("id") != build_id]
        generations.insert(0, {
            "id": build_id,
            "image_tag": os.getenv("IMAGE_TAG", "") or None,
            "synced_at": datetime.now(timezone.utc).isoformat(),
            "files": rel_files,
        })
        manifest = {"generations": generations[:archive_keep()]}
        # Manifest first: a crash between the two leaves unreferenced files behind (harmless, and
        # the next deploy prunes them) rather than a manifest promising files already deleted.
        _write_manifest(archive, manifest)
        removed = _prune(archive, manifest)
        log_info(
            f"Archived SPA asset bundle {build_id}: files={len(rel_files)} "
            f"generations={len(manifest['generations'])} pruned={removed}"
        )
        return manifest
    except Exception as e:  # noqa: BLE001 — archiving is best-effort by design
        log_warning("Could not archive the SPA asset bundle", exc=e)
        return None


def archived_asset_path(rel_path: str) -> Optional[str]:
    """Resolve an asset name against the archive, or None. Path-traversal safe (CWE-22): the
    resolved path must stay inside the archive root and be a regular file.
    """
    archive = archive_dir()
    if not archive or not rel_path:
        return None
    candidate = rel_path.lstrip("/")
    if not candidate or candidate == MANIFEST_NAME or "\\" in candidate:
        return None
    if os.path.isabs(candidate) or any(part in ("..", "") for part in candidate.split("/")):
        return None
    root = os.path.realpath(archive)
    resolved = os.path.realpath(os.path.join(root, candidate))
    if resolved != root and not resolved.startswith(root + os.sep):
        return None
    return resolved if os.path.isfile(resolved) else None


class ArchivedStaticFiles(StaticFiles):
    """The SPA asset mount: content-hashed files cached forever, with a miss falling back to a
    previously-deployed build so a tab that predates the current release still resolves its chunks.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        """Serve from the live bundle, falling back to the archive only on a 404.

        Any other HTTPException (405, a permission problem) is re-raised untouched: the archive
        answers "this hash is from an older build", not "this request was wrong". The immutable
        cache header is stamped on BOTH paths, so an archived chunk is as cacheable as a live one —
        content-hashed names make that safe.
        """
        try:
            response = await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404:
                raise
            archived = archived_asset_path(path)
            if archived is None:
                raise
            log_info(f"Served SPA asset {path} from the archive (pre-deploy tab)")
            response = FileResponse(archived)
        response.headers["Cache-Control"] = IMMUTABLE_CACHE_CONTROL
        return response
