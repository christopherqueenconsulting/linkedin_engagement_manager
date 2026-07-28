"""Unit tests for apply_proxy (Selenium egress proxy wiring)."""

import base64
import io
import json
import zipfile

import pytest
from unittest.mock import MagicMock

from cqc_lem.utilities.selenium_util import apply_proxy

pytestmark = pytest.mark.unit


def _args(options):
    return [c.args[0] for c in options.add_argument.call_args_list]


def _extension_files(b64):
    z = zipfile.ZipFile(io.BytesIO(base64.b64decode(b64)))
    return {n: z.read(n).decode() for n in z.namelist()}


def test_plain_host_port():
    opts = MagicMock()
    apply_proxy(opts, "http://10.0.0.5:8080")
    assert "--proxy-server=http://10.0.0.5:8080" in _args(opts)


def test_socks_scheme_preserved():
    opts = MagicMock()
    apply_proxy(opts, "socks5://exit.local:1080")
    assert "--proxy-server=socks5://exit.local:1080" in _args(opts)


def test_inline_credentials_stripped_from_proxy_server():
    opts = MagicMock()
    apply_proxy(opts, "http://user:secret@host.example:3128")
    # creds must NOT appear in the --proxy-server arg
    proxy_args = [a for a in _args(opts) if a.startswith("--proxy-server")]
    assert proxy_args == ["--proxy-server=http://host.example:3128"]
    assert all("secret" not in a for a in _args(opts))


def test_invalid_url_no_host_is_ignored():
    opts = MagicMock()
    apply_proxy(opts, "not-a-url")
    assert not any(a.startswith("--proxy-server") for a in _args(opts))


def test_default_scheme_when_missing():
    opts = MagicMock()
    apply_proxy(opts, "//bare.host:9000")
    assert "--proxy-server=http://bare.host:9000" in _args(opts)


def test_credentialed_proxy_loads_auth_extension():
    opts = MagicMock()
    apply_proxy(opts, "http://user:secret@host.example:3128")
    # host:port still passed bare (no creds); auth handled by the extension
    assert "--proxy-server=http://host.example:3128" in _args(opts)
    opts.add_encoded_extension.assert_called_once()
    b64 = opts.add_encoded_extension.call_args.args[0]
    files = _extension_files(b64)
    assert "manifest.json" in files and "background.js" in files
    manifest = json.loads(files["manifest.json"])
    assert manifest["manifest_version"] == 3
    assert "webRequestAuthProvider" in manifest["permissions"]
    assert "user" in files["background.js"] and "secret" in files["background.js"]


def test_username_only_no_extension():
    opts = MagicMock()
    apply_proxy(opts, "http://user@host.example:3128")
    assert "--proxy-server=http://host.example:3128" in _args(opts)
    opts.add_encoded_extension.assert_not_called()


def test_no_extension_for_authless_proxy():
    opts = MagicMock()
    apply_proxy(opts, "http://10.0.0.5:8080")
    opts.add_encoded_extension.assert_not_called()


def test_sticky_username_with_special_chars_embedded_safely():
    # DataImpulse sticky username carries ';' and '.' targeting params
    user = "acct__cr.us;state.florida;city.jacksonville;sessid.lem001"
    opts = MagicMock()
    apply_proxy(opts, f"http://{user}:pw123@gw.dataimpulse.com:10000")
    b64 = opts.add_encoded_extension.call_args.args[0]
    bg = _extension_files(b64)["background.js"]
    # embedded via json.dumps → the exact username string round-trips inside a JS literal
    assert json.dumps(user) in bg
