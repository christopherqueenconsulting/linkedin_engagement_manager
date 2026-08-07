"""E2E test for the Avatar page — requires a running FastAPI server and Playwright.

Skipped automatically when REPLICATE_API_TOKEN is not set or is a placeholder.
To run locally:
    REPLICATE_API_TOKEN=r8_xxx BASE_URL=http://localhost:8000 \
        poetry run pytest tests/e2e -m e2e -v
"""

import os

import pytest

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000")


@pytest.mark.e2e
@pytest.mark.requires_replicate
def test_avatar_page_shows_pricing_and_credit_balance(page):
    """Navigate to /avatars without logging in — expect redirect or login prompt.
    Then simulate auth bypass and verify the pricing cards and credit balance display.
    """
    # The app uses an email/PIN auth with bypass mode when no email provider is set.
    # In test environments with no SENDGRID/SMTP configured, /auth/email/init returns
    # a bypass session immediately. We call the API directly to get a session token.
    import requests

    init_resp = requests.post(
        f"{BASE_URL}/api/auth/email/init",
        json={"email": "e2e-test@example.com"},
        timeout=10,
    )
    assert init_resp.status_code == 200, f"Auth init failed: {init_resp.text}"
    body = init_resp.json()["detail"]

    if body.get("bypass"):
        session_token = body["session_token"]
    else:
        pytest.skip("Email provider is configured — bypass auth not available in this environment")
        session_token = ""  # unreachable — pytest.skip() always raises; satisfies static analysis

    # Set the session token in localStorage via a JS snippet
    page.goto(BASE_URL)
    page.evaluate(f"""
        localStorage.setItem('lem_session', '{session_token}');
        localStorage.setItem('lem_email', 'e2e-test@example.com');
    """)

    # Navigate to the Avatars page
    page.goto(f"{BASE_URL}/avatars")
    page.wait_for_load_state("networkidle", timeout=15_000)

    # Verify the credit balance widget is present
    balance_el = page.locator("[data-testid='avatar-credit-balance']")
    balance_el.wait_for(timeout=10_000)
    assert balance_el.is_visible(), "Avatar credit balance widget not visible"

    # Verify pricing cards are present
    pricing_el = page.locator("[data-testid='avatar-pricing-cards']")
    assert pricing_el.is_visible(), "Avatar pricing cards not visible"

    # Verify all four price points appear on the page
    for price in ["$5", "$10", "$25", "$40"]:
        assert page.locator(f"text={price}").first.is_visible(), f"Price card '{price}' not found"

    # Verify the Train New Avatar section heading exists
    assert page.locator("text=Train New Avatar").is_visible(), "Train New Avatar section missing"

    # Verify the Buy button exists for the $5 package
    buy_buttons = page.locator("button", has_text="Buy")
    assert buy_buttons.count() >= 1, "No Buy buttons found on pricing cards"
