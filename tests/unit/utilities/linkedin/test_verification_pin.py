"""Unit tests for the email-reply verification-PIN exchange."""

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_MOD = "cqc_lem.utilities.linkedin.verification_pin"


@pytest.fixture
def fake_redis():
    client = MagicMock()
    with patch(f"{_MOD}._redis_client", return_value=client):
        yield client


class TestExtractPin:
    def test_plain_code(self):
        from cqc_lem.utilities.linkedin.verification_pin import extract_pin_from_text
        assert extract_pin_from_text("Here you go: 483920") == "483920"

    def test_code_alone(self):
        from cqc_lem.utilities.linkedin.verification_pin import extract_pin_from_text
        assert extract_pin_from_text("123456") == "123456"

    def test_ignores_quoted_history(self):
        from cqc_lem.utilities.linkedin.verification_pin import extract_pin_from_text
        body = "654321\n\nOn Tue wrote:\n> your code was 999999 last time"
        assert extract_pin_from_text(body) == "654321"

    def test_ignores_gt_quoted_block(self):
        from cqc_lem.utilities.linkedin.verification_pin import extract_pin_from_text
        body = "111222\n> previously 333444"
        assert extract_pin_from_text(body) == "111222"

    def test_not_seven_or_more_digits(self):
        from cqc_lem.utilities.linkedin.verification_pin import extract_pin_from_text
        assert extract_pin_from_text("order 1234567 shipped") is None

    def test_none_and_empty(self):
        from cqc_lem.utilities.linkedin.verification_pin import extract_pin_from_text
        assert extract_pin_from_text("") is None
        assert extract_pin_from_text("no digits here") is None


class TestPinReplyAddress:
    def test_explicit_env_wins(self, monkeypatch):
        monkeypatch.setenv("LINKEDIN_PARSE_DOMAIN", "parse.custom.io")
        from cqc_lem.utilities.linkedin.verification_pin import pin_reply_address
        assert pin_reply_address("tok9") == "pin+tok9@parse.custom.io"

    def test_derives_from_sendgrid_from_email(self, monkeypatch):
        monkeypatch.delenv("LINKEDIN_PARSE_DOMAIN", raising=False)
        monkeypatch.setenv("SENDGRID_FROM_EMAIL", "no-reply@christopherqueenconsulting.com")
        from cqc_lem.utilities.linkedin.verification_pin import pin_reply_address
        assert pin_reply_address("t") == "pin+t@parse.christopherqueenconsulting.com"

    def test_derives_from_public_base_url_when_no_from_email(self, monkeypatch):
        monkeypatch.delenv("LINKEDIN_PARSE_DOMAIN", raising=False)
        monkeypatch.setenv("SENDGRID_FROM_EMAIL", "")
        monkeypatch.setenv("PUBLIC_BASE_URL", "https://lem.christopherqueenconsulting.com")
        from cqc_lem.utilities.linkedin.verification_pin import _default_parse_domain
        assert _default_parse_domain() == "parse.christopherqueenconsulting.com"

    def test_falls_back_to_example_when_nothing_configured(self, monkeypatch):
        monkeypatch.delenv("LINKEDIN_PARSE_DOMAIN", raising=False)
        monkeypatch.setenv("SENDGRID_FROM_EMAIL", "")
        monkeypatch.setenv("PUBLIC_BASE_URL", "")
        from cqc_lem.utilities.linkedin.verification_pin import _default_parse_domain
        assert _default_parse_domain() == "parse.example.com"


class TestTokenRoundTrip:
    def test_create_then_submit_by_token(self, fake_redis):
        store = {}
        fake_redis.set.side_effect = lambda k, v, ex=None: store.__setitem__(k, v)
        fake_redis.get.side_effect = lambda k: store.get(k)
        from cqc_lem.utilities.linkedin.verification_pin import create_pin_request, get_pin, submit_pin_by_token
        token = create_pin_request(42)
        assert token
        uid = submit_pin_by_token(token, "246810")
        assert uid == 42
        assert get_pin(42) == "246810"

    def test_submit_unknown_token_returns_none(self, fake_redis):
        fake_redis.get.return_value = None
        from cqc_lem.utilities.linkedin.verification_pin import submit_pin_by_token
        assert submit_pin_by_token("nope", "123456") is None

    def test_bytes_values_decoded(self, fake_redis):
        fake_redis.get.return_value = b"7"
        from cqc_lem.utilities.linkedin.verification_pin import submit_pin_by_token
        assert submit_pin_by_token("tok", "123456") == 7


class TestNoRedisFailsOpen:
    def test_create_returns_token_without_redis(self):
        with patch(f"{_MOD}._redis_client", return_value=None):
            from cqc_lem.utilities.linkedin.verification_pin import create_pin_request
            assert create_pin_request(1)  # still returns a token

    def test_get_pin_none_without_redis(self):
        with patch(f"{_MOD}._redis_client", return_value=None):
            from cqc_lem.utilities.linkedin.verification_pin import get_pin
            assert get_pin(1) is None

    def test_submit_false_without_redis(self):
        with patch(f"{_MOD}._redis_client", return_value=None):
            from cqc_lem.utilities.linkedin.verification_pin import submit_pin
            assert submit_pin(1, "123456") is False


class TestGetAndClear:
    def test_get_pin_decodes_bytes(self, fake_redis):
        fake_redis.get.return_value = b"135790"
        from cqc_lem.utilities.linkedin.verification_pin import get_pin
        assert get_pin(9) == "135790"

    def test_get_pin_swallows_error(self, fake_redis):
        fake_redis.get.side_effect = RuntimeError("down")
        from cqc_lem.utilities.linkedin.verification_pin import get_pin
        assert get_pin(9) is None

    def test_clear_deletes_key(self, fake_redis):
        from cqc_lem.utilities.linkedin.verification_pin import clear_pin
        clear_pin(9)
        fake_redis.delete.assert_called_once_with("linkedin:pin:9")
