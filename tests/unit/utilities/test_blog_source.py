"""Unit tests for blog-align source resolution (issue #967)."""

import random
from unittest.mock import patch

import pytest

from cqc_lem.utilities.blog_source import BLOG_SOURCE_MAX_CHARS, _plain_text, resolve_blog_source

pytestmark = pytest.mark.unit

_DB = "cqc_lem.utilities.db"
_RCP = "cqc_lem.app.run_content_plan"

_ON = {"align_with_blog": True}


class TestPlainText:
    def test_strips_markup_and_blank_lines(self):
        html = "<div><h1>Title</h1>\n\n<p>First para.</p><p>Second para.</p></div>"
        assert _plain_text(html) == "Title\nFirst para.\nSecond para."

    def test_accepts_bytes(self):
        assert _plain_text(b"<p>bytes body</p>") == "bytes body"

    def test_joins_paragraph_lists(self):
        assert _plain_text(["one", "two"]) == "one\ntwo"

    def test_none_is_empty(self):
        assert _plain_text(None) == ""


class TestResolveBlogSource:
    def test_toggle_off_never_touches_the_network(self):
        with patch(f"{_DB}.get_user_blog_url") as blog, \
             patch(f"{_RCP}.get_main_blog_url_content") as fetch:
            assert resolve_blog_source(1, {"align_with_blog": False}) is None
            assert resolve_blog_source(1, {}) is None
            assert resolve_blog_source(1, None) is None
        blog.assert_not_called()
        fetch.assert_not_called()

    def test_returns_blog_article_as_plain_text(self):
        with patch(f"{_DB}.get_user_blog_url", return_value="https://blog.x.com"), \
             patch(f"{_RCP}.get_main_blog_url_content",
                   return_value=("https://blog.x.com/p1", "<p>The real words.</p>")), \
             patch(f"{_DB}.get_user_sitemap_url") as sitemap:
            assert resolve_blog_source(1, _ON) == "The real words."
        sitemap.assert_not_called()  # blog is the primary source; no fallback needed

    def test_truncates_to_the_carry_budget(self):
        body = "<p>" + ("word " * 5000) + "</p>"
        with patch(f"{_DB}.get_user_blog_url", return_value="https://blog.x.com"), \
             patch(f"{_RCP}.get_main_blog_url_content", return_value=("u", body)), \
             patch(f"{_DB}.get_user_sitemap_url", return_value=None):
            out = resolve_blog_source(1, _ON)
        assert len(out) == BLOG_SOURCE_MAX_CHARS

    def test_blog_fetch_failure_falls_back_to_sitemap(self):
        with patch(f"{_DB}.get_user_blog_url", return_value="https://blog.x.com"), \
             patch(f"{_RCP}.get_main_blog_url_content", side_effect=RuntimeError("boom")), \
             patch(f"{_DB}.get_user_sitemap_url", return_value="https://x.com/sitemap.xml"), \
             patch(f"{_RCP}.fetch_sitemap_urls", return_value=["https://x.com/a"]), \
             patch(f"{_RCP}.filter_relevant_urls", side_effect=lambda urls, **kw: urls), \
             patch(f"{_RCP}.extract_page_content", return_value=("A Title", ["Page body."])):
            assert resolve_blog_source(1, _ON) == "A Title\nPage body."

    def test_empty_blog_falls_back_to_sitemap(self):
        with patch(f"{_DB}.get_user_blog_url", return_value="https://blog.x.com"), \
             patch(f"{_RCP}.get_main_blog_url_content", return_value=(None, None)), \
             patch(f"{_DB}.get_user_sitemap_url", return_value="https://x.com/sitemap.xml"), \
             patch(f"{_RCP}.fetch_sitemap_urls", return_value=["https://x.com/a"]), \
             patch(f"{_RCP}.filter_relevant_urls", side_effect=lambda urls, **kw: urls), \
             patch(f"{_RCP}.extract_page_content", return_value=(None, ["Only body."])):
            assert resolve_blog_source(1, _ON) == "Only body."

    def test_sitemap_skips_unreadable_pages(self):
        pages = {"https://x.com/a": (None, []), "https://x.com/b": ("B", ["Readable."])}
        with patch(f"{_DB}.get_user_blog_url", return_value=None), \
             patch(f"{_DB}.get_user_sitemap_url", return_value="https://x.com/sitemap.xml"), \
             patch(f"{_RCP}.fetch_sitemap_urls", return_value=list(pages)), \
             patch(f"{_RCP}.filter_relevant_urls", side_effect=lambda urls, **kw: urls), \
             patch(f"{_RCP}.extract_page_content", side_effect=lambda u: pages[u]):
            assert resolve_blog_source(1, _ON) == "B\nReadable."

    def test_sitemap_page_error_is_not_fatal(self):
        def _extract(url):
            if url.endswith("a"):
                raise RuntimeError("dead page")
            return ("B", ["Readable."])

        with patch(f"{_DB}.get_user_blog_url", return_value=None), \
             patch(f"{_DB}.get_user_sitemap_url", return_value="https://x.com/sitemap.xml"), \
             patch(f"{_RCP}.fetch_sitemap_urls", return_value=["https://x.com/a", "https://x.com/b"]), \
             patch(f"{_RCP}.filter_relevant_urls", side_effect=lambda urls, **kw: urls), \
             patch(f"{_RCP}.extract_page_content", side_effect=_extract):
            assert resolve_blog_source(1, _ON) == "B\nReadable."

    def test_sitemap_varies_the_page_across_editions(self):
        """Resolution is PER edition; a fixed 'first URL' would give every edition the same page."""
        urls = [f"https://x.com/{n}" for n in "abcde"]
        with patch(f"{_DB}.get_user_blog_url", return_value=None), \
             patch(f"{_DB}.get_user_sitemap_url", return_value="https://x.com/sitemap.xml"), \
             patch(f"{_RCP}.fetch_sitemap_urls", return_value=urls), \
             patch(f"{_RCP}.filter_relevant_urls", side_effect=lambda u, **kw: u), \
             patch(f"{_RCP}.extract_page_content", side_effect=lambda u: (None, [f"body {u[-1]}"])):
            random.seed(967)
            seen = {resolve_blog_source(1, _ON) for _ in range(12)}
        assert len(seen) > 1

    def test_sitemap_fetch_failure_warns_once(self):
        """One condition, ONE warning — the detect site owns it, the caller must not restate it."""
        with patch(f"{_DB}.get_user_blog_url", return_value=None), \
             patch(f"{_DB}.get_user_sitemap_url", return_value="https://x.com/sitemap.xml"), \
             patch(f"{_RCP}.fetch_sitemap_urls", side_effect=RuntimeError("boom")), \
             patch("cqc_lem.utilities.blog_source.log_warning") as warn:
            assert resolve_blog_source(1, _ON) is None
        warn.assert_called_once()

    def test_db_failure_is_never_fatal_to_the_edition(self):
        """The contract is 'never raises' — regenerate resolves OUTSIDE its own try/except."""
        with patch(f"{_DB}.get_user_blog_url", side_effect=RuntimeError("pool exhausted")), \
             patch("cqc_lem.utilities.blog_source.log_warning") as warn:
            assert resolve_blog_source(1, _ON) is None
        warn.assert_called_once()

    def test_nothing_configured_is_an_expected_no_op(self):
        """The toggle defaults ON, so a user with no blog set must not file a defect."""
        with patch(f"{_DB}.get_user_blog_url", return_value=None), \
             patch(f"{_DB}.get_user_sitemap_url", return_value=None), \
             patch("cqc_lem.utilities.blog_source.log_warning") as warn:
            assert resolve_blog_source(1, _ON) is None
        warn.assert_not_called()

    def test_configured_but_unreadable_warns(self):
        with patch(f"{_DB}.get_user_blog_url", return_value="https://blog.x.com"), \
             patch(f"{_RCP}.get_main_blog_url_content", return_value=("u", "")), \
             patch(f"{_DB}.get_user_sitemap_url", return_value=None), \
             patch("cqc_lem.utilities.blog_source.log_warning") as warn:
            assert resolve_blog_source(1, _ON) is None
        warn.assert_called_once()
