"""The crawler-facing half of the SPA shell (issue #1298).

The SPA catch-all answers every unclaimed path with index.html, so "the file is in dist/" is not
the same thing as "the file is served". These tests pin both halves: the routes that claim those
paths ahead of the catch-all, and the absolute-URL substitution that makes a sitemap and a robots
`Sitemap:` line valid at all.
"""

import os
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

from cqc_lem.api.spa_assets import (
    BASE_URL_PLACEHOLDER,
    PUBLIC_ROOT_FILES,
    VITE_BASE_URL_PLACEHOLDER,
    public_base_url,
    register_spa_public_routes,
    render_base_url,
)

pytestmark = pytest.mark.unit

UI_PUBLIC = Path(__file__).resolve().parents[3] / "src" / "cqc_lem" / "ui" / "public"
UI_INDEX = Path(__file__).resolve().parents[3] / "src" / "cqc_lem" / "ui" / "index.html"


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    """A minimal built SPA: the public files the routes serve, plus the OG image."""
    (tmp_path / "robots.txt").write_text(
        f"User-agent: *\nAllow: /\n\nSitemap: {BASE_URL_PLACEHOLDER}/sitemap.xml\n"
    )
    (tmp_path / "sitemap.xml").write_text(
        f'<?xml version="1.0" encoding="UTF-8"?>\n<urlset><url>'
        f"<loc>{BASE_URL_PLACEHOLDER}/</loc></url></urlset>\n"
    )
    (tmp_path / "favicon.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
    (tmp_path / "brand").mkdir()
    (tmp_path / "brand" / "og.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    return tmp_path


def _client(dist_dir: Path) -> TestClient:
    """An app wired exactly like api/main: public routes first, catch-all last."""
    app = FastAPI()
    register_spa_public_routes(app, str(dist_dir))

    @app.get("/{full_path:path}", response_class=HTMLResponse)
    def _catch_all(full_path: str):
        return HTMLResponse(content="<html>spa</html>")

    return TestClient(app)


class TestPublicBaseUrl:
    def test_prefers_the_configured_host_and_drops_the_trailing_slash(self, monkeypatch):
        monkeypatch.setenv("PUBLIC_BASE_URL", "https://lem.example.com/")
        assert public_base_url("http://ignored.example") == "https://lem.example.com"

    def test_falls_back_to_the_request_origin_when_unset(self, monkeypatch):
        monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
        assert public_base_url("http://testserver/") == "http://testserver"

    def test_blank_configuration_counts_as_unset(self, monkeypatch):
        monkeypatch.setenv("PUBLIC_BASE_URL", "   ")
        assert public_base_url("http://testserver/") == "http://testserver"

    def test_no_host_at_all_is_empty_not_a_crash(self, monkeypatch):
        monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
        assert public_base_url() == ""


class TestRenderBaseUrl:
    def test_substitutes_every_occurrence(self):
        raw = f"{BASE_URL_PLACEHOLDER}/a {BASE_URL_PLACEHOLDER}/b"
        assert render_base_url(raw, "https://x.example") == "https://x.example/a https://x.example/b"

    def test_the_build_time_token_is_a_separate_placeholder(self):
        raw = f'content="{VITE_BASE_URL_PLACEHOLDER}/brand/og.png"'
        assert render_base_url(raw, "https://x.example", VITE_BASE_URL_PLACEHOLDER) == (
            'content="https://x.example/brand/og.png"'
        )
        # The server-side token must not touch the build-time one, or a shell Vite already filled
        # in would be rewritten on every request.
        assert render_base_url(raw, "https://x.example") == raw


class TestServedFiles:
    def test_robots_and_sitemap_beat_the_spa_catch_all(self, dist, monkeypatch):
        monkeypatch.setenv("PUBLIC_BASE_URL", "https://lem.example.com")
        client = _client(dist)

        robots = client.get("/robots.txt")
        assert robots.status_code == 200
        assert robots.headers["content-type"].startswith("text/plain")
        assert "Sitemap: https://lem.example.com/sitemap.xml" in robots.text

        sitemap = client.get("/sitemap.xml")
        assert sitemap.status_code == 200
        assert sitemap.headers["content-type"].startswith("application/xml")
        assert "<loc>https://lem.example.com/</loc>" in sitemap.text

    def test_no_placeholder_survives_into_a_response(self, dist, monkeypatch):
        monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
        client = _client(dist)
        for path in ("/robots.txt", "/sitemap.xml"):
            body = client.get(path).text
            assert BASE_URL_PLACEHOLDER not in body
            # No configured host: the request's own origin keeps the URLs absolute.
            assert "http://testserver/" in body

    def test_the_open_graph_image_is_served_as_an_image(self, dist):
        response = _client(dist).get("/brand/og.png")
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"

    def test_favicon_is_served_as_svg_not_html(self, dist):
        response = _client(dist).get("/favicon.svg")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("image/svg+xml")

    def test_a_missing_file_404s_instead_of_returning_the_shell(self, tmp_path):
        response = _client(tmp_path).get("/robots.txt")
        assert response.status_code == 404
        assert "spa" not in response.text

    def test_an_unclaimed_path_still_reaches_the_spa(self, dist):
        response = _client(dist).get("/dashboard")
        assert response.status_code == 200
        assert "spa" in response.text

    def test_a_brandless_build_does_not_break_registration(self, tmp_path):
        (tmp_path / "robots.txt").write_text("User-agent: *\n")
        response = _client(tmp_path).get("/robots.txt")
        assert response.status_code == 200


class TestShippedSourceFiles:
    """The two halves must not drift: what public/ ships is what the routes know how to serve."""

    def test_every_routed_root_file_exists_in_public(self):
        for file_name in PUBLIC_ROOT_FILES:
            assert (UI_PUBLIC / file_name).is_file(), file_name

    def test_the_open_graph_image_exists(self):
        assert (UI_PUBLIC / "brand" / "og.png").is_file()

    def test_sitemap_and_robots_carry_the_absolute_url_token(self):
        # A relative <loc> or Sitemap: line is INVALID per the sitemaps.org spec and is dropped by
        # Google, so the placeholder — not a bare "/" — is what may ship.
        sitemap = (UI_PUBLIC / "sitemap.xml").read_text()
        robots = (UI_PUBLIC / "robots.txt").read_text()
        assert f"<loc>{BASE_URL_PLACEHOLDER}/</loc>" in sitemap
        assert f"Sitemap: {BASE_URL_PLACEHOLDER}/sitemap.xml" in robots

    def test_the_shell_references_the_build_time_token(self):
        shell = UI_INDEX.read_text()
        for attribute in ("og:image", "og:url", "twitter:image"):
            assert attribute in shell
        assert f"{VITE_BASE_URL_PLACEHOLDER}/brand/og.png" in shell

    def test_the_image_the_shell_points_at_resolves_under_public(self):
        shell = UI_INDEX.read_text()
        marker = f'content="{VITE_BASE_URL_PLACEHOLDER}/'
        start = shell.index(marker) + len(marker)
        relative = shell[start:shell.index('"', start)]
        assert (UI_PUBLIC / relative).is_file(), relative
        assert not os.path.isabs(relative)
