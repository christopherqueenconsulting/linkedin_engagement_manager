"""Unit tests for the small LinkedIn analytics OAuth helpers."""

import pytest
import requests

from cqc_lem.api import main

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("profile_url, expected", [
    ("https://www.linkedin.com/in/Member-Name/", "member-name"),
    ("https://www.linkedin.com/company/acme", None),
    ("", None),
    (None, None),
])
def test_linkedin_vanity_name_parses_only_member_profile_urls(profile_url, expected):
    assert main._linkedin_vanity_name(profile_url) == expected


def test_linkedin_exception_status_code_reads_sdk_and_requests_shapes():
    sdk_error = RuntimeError("forbidden")
    sdk_error.status_code = 403
    response = requests.Response()
    response.status_code = 401
    request_error = requests.HTTPError(response=response)

    assert main._linkedin_exception_status_code(sdk_error) == 403
    assert main._linkedin_exception_status_code(request_error) == 401
    assert main._linkedin_exception_status_code(RuntimeError("unavailable")) is None


def test_linkedin_account_redirect_preserves_callback_port(monkeypatch):
    monkeypatch.setattr(main, "NGROK_CUSTOM_DOMAIN", "")

    response = main._linkedin_account_redirect(
        "http://localhost:8123/auth/linkedin/callback", {"li_error": "denied"})

    assert response.headers["location"] == "http://localhost:8123/account?li_error=denied"


def test_linkedin_account_redirect_uses_configured_ngrok_domain(monkeypatch):
    monkeypatch.setattr(main, "NGROK_CUSTOM_DOMAIN", "lem.ngrok-free.dev")

    response = main._linkedin_account_redirect(
        "http://localhost:8123/auth/linkedin/callback", {"li_error": "denied"})

    assert response.headers["location"] == "https://lem.ngrok-free.dev/account?li_error=denied"
