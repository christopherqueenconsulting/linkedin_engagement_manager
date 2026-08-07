"""Unit tests for API_URL_FINAL construction in env_constants.py."""

import importlib
import sys

import pytest


def _reload_with_env(monkeypatch, env_vars: dict) -> object:
    """Reload env_constants with the given env vars and return the module."""
    for k, v in env_vars.items():
        monkeypatch.setenv(k, v)
    # Remove all conflicting env vars not in env_vars
    for key in ["NGROK_CUSTOM_DOMAIN", "NGROK_FREE_DOMAIN", "NGROK_API_PREFIX", "API_BASE_URL", "API_PORT"]:
        if key not in env_vars:
            monkeypatch.delenv(key, raising=False)

    # Force reimport
    if "cqc_lem.utilities.env_constants" in sys.modules:
        del sys.modules["cqc_lem.utilities.env_constants"]
    return importlib.import_module("cqc_lem.utilities.env_constants")


@pytest.mark.unit
class TestAPIUrlFinalConstruction:

    def test_custom_domain_takes_precedence_over_prefix(self, monkeypatch):
        """NGROK_CUSTOM_DOMAIN must be used as API_URL_FINAL even when PREFIX and FREE_DOMAIN are set."""
        ec = _reload_with_env(monkeypatch, {
            "NGROK_CUSTOM_DOMAIN": "relegable-preroyally-marti.ngrok-free.dev",
            "NGROK_FREE_DOMAIN": "ngrok-free.dev",
            "NGROK_API_PREFIX": "cqc-lem-api",
        })
        assert ec.API_URL_FINAL == "https://relegable-preroyally-marti.ngrok-free.dev"

    def test_prefix_form_used_when_no_custom_domain(self, monkeypatch):
        """Without NGROK_CUSTOM_DOMAIN, PREFIX.FREE_DOMAIN form is used."""
        ec = _reload_with_env(monkeypatch, {
            "NGROK_FREE_DOMAIN": "ngrok-free.dev",
            "NGROK_API_PREFIX": "myapp-api",
        })
        assert ec.API_URL_FINAL == "https://myapp-api.ngrok-free.dev"

    def test_localhost_fallback_when_no_ngrok_vars(self, monkeypatch):
        """When no ngrok vars are set, fallback to http://localhost:{API_PORT}."""
        ec = _reload_with_env(monkeypatch, {"API_PORT": "8000"})
        assert ec.API_URL_FINAL == "http://localhost:8000"

    def test_custom_api_base_url_fallback(self, monkeypatch):
        """API_BASE_URL env var is used as fallback when no ngrok vars present."""
        ec = _reload_with_env(monkeypatch, {
            "API_BASE_URL": "https://api.example.com",
            "API_PORT": "443",
        })
        assert ec.API_URL_FINAL == "https://api.example.com:443"

    def test_custom_domain_sets_api_base_url(self, monkeypatch):
        """API_BASE_URL should match API_URL_FINAL when custom domain is used."""
        ec = _reload_with_env(monkeypatch, {
            "NGROK_CUSTOM_DOMAIN": "my-custom.ngrok-free.dev",
        })
        assert ec.API_BASE_URL == ec.API_URL_FINAL
        assert ec.API_URL_FINAL == "https://my-custom.ngrok-free.dev"

    def test_li_redirect_url_uses_custom_domain(self, monkeypatch):
        """LI_REDIRECT_URL should use NGROK_CUSTOM_DOMAIN when set."""
        ec = _reload_with_env(monkeypatch, {
            "NGROK_CUSTOM_DOMAIN": "my-custom.ngrok-free.dev",
            "LI_REDIRECT_URL": "https://old-url.com/auth/linkedin/callback",
        })
        assert ec.LI_REDIRECT_URL == "https://my-custom.ngrok-free.dev/auth/linkedin/callback"
