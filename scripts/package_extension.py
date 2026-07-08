#!/usr/bin/env python3
"""Build a Chrome Web Store-ready zip of the LEM LinkedIn Connect extension.

    poetry run python scripts/package_extension.py

Writes dist/lem-linkedin-connect-v<version>.zip (version read from manifest.json). This is
the artifact uploaded to the Chrome Web Store — the in-app download route
(GET /api/extension/linkedin-connect.zip) zips the same folder on the fly for side-loading.
"""
import json
import os
import zipfile

_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
_EXT_DIR = os.path.join(_ROOT, "src", "cqc_lem", "browser_extension")
_DIST_DIR = os.path.join(_ROOT, "dist")


def main() -> str:
    with open(os.path.join(_EXT_DIR, "manifest.json"), encoding="utf-8") as f:
        version = json.load(f)["version"]

    os.makedirs(_DIST_DIR, exist_ok=True)
    out = os.path.join(_DIST_DIR, f"lem-linkedin-connect-v{version}.zip")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, files in os.walk(_EXT_DIR):
            for name in sorted(files):
                fp = os.path.join(root, name)
                zf.write(fp, arcname=os.path.relpath(fp, _EXT_DIR))
    print(f"wrote {os.path.relpath(out, _ROOT)}")
    return out


if __name__ == "__main__":
    main()
