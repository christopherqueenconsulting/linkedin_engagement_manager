"""Unit tests for resilient profile parsing in scrapper.parse_profile_header."""

import pytest
from bs4 import BeautifulSoup

pytestmark = pytest.mark.unit


def _soup(html):
    return BeautifulSoup(html, "html.parser")


class TestIsLinkedinErrorPage:
    def test_429(self):
        from cqc_lem.utilities.linkedin.scrapper import _is_linkedin_error_page
        assert _is_linkedin_error_page("This page isn't working HTTP ERROR 429 Reload") is True

    def test_authwall_join(self):
        from cqc_lem.utilities.linkedin.scrapper import _is_linkedin_error_page
        assert _is_linkedin_error_page("Join LinkedIn to continue") is True

    def test_real_profile_text(self):
        from cqc_lem.utilities.linkedin.scrapper import _is_linkedin_error_page
        assert _is_linkedin_error_page("Jane Doe Senior Engineer at Acme") is False


class TestParseProfileHeader:
    def test_rate_limited_page_raises(self):
        from cqc_lem.utilities.linkedin.scrapper import ProfileUnavailableError, parse_profile_header
        page = _soup("<html><body>This page isn't working. HTTP ERROR 429</body></html>")
        with pytest.raises(ProfileUnavailableError):
            parse_profile_header(page, "https://www.linkedin.com/in/x/")

    def test_missing_name_raises(self):
        from cqc_lem.utilities.linkedin.scrapper import ProfileUnavailableError, parse_profile_header
        page = _soup("<html><body><div>no header here</div></body></html>")
        with pytest.raises(ProfileUnavailableError):
            parse_profile_header(page, "https://www.linkedin.com/in/x/")

    def test_none_source_raises(self):
        from cqc_lem.utilities.linkedin.scrapper import ProfileUnavailableError, parse_profile_header
        with pytest.raises(ProfileUnavailableError):
            parse_profile_header(None, "https://www.linkedin.com/in/x/")

    def test_name_from_title_when_no_h1(self):
        # LinkedIn dropped the profile <h1> and uses hashed CSS classes; the name is only
        # reliably in the <title>. Must extract it instead of raising.
        from cqc_lem.utilities.linkedin.scrapper import parse_profile_header
        page = _soup(
            "<html><head><title>Christopher Queen | LinkedIn</title></head>"
            "<body><div class='_9d763823 ca9510cb'>Christopher Queen</div>"
            "<div class='text-body-medium'>Applied AI Engineer</div></body></html>")
        prof = parse_profile_header(page, "https://www.linkedin.com/in/christopherqueen/")
        assert prof["full_name"] == "Christopher Queen"

    def test_name_from_title_strips_unread_count_prefix(self):
        from cqc_lem.utilities.linkedin.scrapper import _name_from_title
        page = _soup("<html><head><title>(7) Christopher Queen | LinkedIn</title></head><body></body></html>")
        assert _name_from_title(page) == "Christopher Queen"

    def test_generic_title_not_treated_as_name(self):
        from cqc_lem.utilities.linkedin.scrapper import ProfileUnavailableError, parse_profile_header
        page = _soup("<html><head><title>Feed | LinkedIn</title></head><body><div>x</div></body></html>")
        with pytest.raises(ProfileUnavailableError):
            parse_profile_header(page, "https://www.linkedin.com/in/x/")

    def test_extracts_name_and_title_with_fallback_selectors(self):
        # No 'mt2 relative' container — must still find the h1 + title via fallbacks.
        from cqc_lem.utilities.linkedin.scrapper import parse_profile_header
        page = _soup(
            "<html><body><main>"
            "<h1 class='inline t-24'>Jane Doe</h1>"
            "<div class='text-body-medium break-words'>Senior Engineer at Acme</div>"
            "</main></body></html>")
        prof = parse_profile_header(page, "https://www.linkedin.com/in/jane/", company_name="Acme")
        assert prof["full_name"] == "Jane Doe"
        assert prof["job_title"] == "Senior Engineer at Acme"
        assert prof["company_name"] == "Acme"
        assert prof["profile_url"] == "https://www.linkedin.com/in/jane/"

    def test_title_missing_is_empty_not_crash(self):
        from cqc_lem.utilities.linkedin.scrapper import parse_profile_header
        page = _soup("<html><body><h1>Jane Doe</h1></body></html>")
        prof = parse_profile_header(page, "https://www.linkedin.com/in/jane/")
        assert prof["full_name"] == "Jane Doe"
        assert prof["job_title"] == ""

    def test_old_container_still_works(self):
        from cqc_lem.utilities.linkedin.scrapper import parse_profile_header
        page = _soup(
            "<html><body><div class='mt2 relative'>"
            "<h1 class='break-words'>Bob Smith</h1>"
            "<div class='text-body-medium break-words'>CTO at Beta</div>"
            "<span class='dist-value'>1st</span>"
            "</div></body></html>")
        prof = parse_profile_header(page, "https://www.linkedin.com/in/bob/")
        assert prof["full_name"] == "Bob Smith"
        assert prof["job_title"] == "CTO at Beta"
        assert prof["connection"] == "1st"
