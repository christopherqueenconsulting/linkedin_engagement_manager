from unittest.mock import MagicMock, patch

from cqc_lem.utilities.email import generate_pin, hash_pin, send_pin_email


class TestGeneratePin:
    def test_is_six_digits(self):
        pin = generate_pin()
        assert len(pin) == 6
        assert pin.isdigit()

    def test_zero_padded(self):
        pin = generate_pin()
        assert len(pin) == 6

    def test_returns_string(self):
        assert isinstance(generate_pin(), str)


class TestHashPin:
    def test_deterministic(self):
        h1 = hash_pin("123456", "test@example.com")
        h2 = hash_pin("123456", "test@example.com")
        assert h1 == h2

    def test_different_pins_produce_different_hashes(self):
        h1 = hash_pin("000000", "test@example.com")
        h2 = hash_pin("999999", "test@example.com")
        assert h1 != h2

    def test_different_emails_produce_different_hashes(self):
        h1 = hash_pin("123456", "a@example.com")
        h2 = hash_pin("123456", "b@example.com")
        assert h1 != h2

    def test_returns_64_char_hex(self):
        h = hash_pin("123456", "test@example.com")
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)


class TestSendPinEmailSendGrid:
    @patch("cqc_lem.utilities.email.SENDGRID_API_KEY", "sg_test_key")
    @patch("cqc_lem.utilities.email.SMTP_USER", None)
    @patch("cqc_lem.utilities.email.SMTP_PASSWORD", None)
    @patch("sendgrid.SendGridAPIClient")
    def test_sendgrid_success_returns_true_not_bypassed(self, mock_sg_class):
        mock_sg = MagicMock()
        mock_sg.send.return_value = MagicMock(status_code=202)
        mock_sg_class.return_value = mock_sg

        success, bypassed = send_pin_email("test@example.com", "123456")

        assert success is True
        assert bypassed is False
        mock_sg.send.assert_called_once()

    @patch("cqc_lem.utilities.email.SENDGRID_API_KEY", "sg_test_key")
    @patch("cqc_lem.utilities.email.SMTP_USER", None)
    @patch("cqc_lem.utilities.email.SMTP_PASSWORD", None)
    @patch("sendgrid.SendGridAPIClient")
    def test_sendgrid_failure_returns_false_not_bypassed(self, mock_sg_class):
        mock_sg = MagicMock()
        mock_sg.send.side_effect = Exception("Network error")
        mock_sg_class.return_value = mock_sg

        success, bypassed = send_pin_email("test@example.com", "123456")

        assert success is False
        assert bypassed is False

    @patch("cqc_lem.utilities.email.SENDGRID_API_KEY", "sg_test_key")
    @patch("cqc_lem.utilities.email.SMTP_USER", None)
    @patch("cqc_lem.utilities.email.SMTP_PASSWORD", None)
    @patch("sendgrid.SendGridAPIClient")
    def test_new_user_flag_does_not_crash(self, mock_sg_class):
        mock_sg = MagicMock()
        mock_sg_class.return_value = mock_sg

        send_pin_email("new@example.com", "000001", is_new_user=True)

        mock_sg.send.assert_called_once()

    @patch("cqc_lem.utilities.email.SENDGRID_API_KEY", "sg_test_key")
    @patch("cqc_lem.utilities.email.SMTP_USER", None)
    @patch("cqc_lem.utilities.email.SMTP_PASSWORD", None)
    @patch("sendgrid.SendGridAPIClient")
    def test_click_tracking_disabled_on_transactional_mail(self, mock_sg_class):
        mock_sg = MagicMock()
        mock_sg_class.return_value = mock_sg

        send_pin_email("test@example.com", "123456")

        sent = mock_sg.send.call_args[0][0]
        assert sent.get()["tracking_settings"]["click_tracking"]["enable"] is False


class TestSendPinEmailSmtpFallback:
    @patch("cqc_lem.utilities.email.SENDGRID_API_KEY", None)
    @patch("cqc_lem.utilities.email.SMTP_USER", "user@gmail.com")
    @patch("cqc_lem.utilities.email.SMTP_PASSWORD", "app_password")
    @patch("cqc_lem.utilities.email.smtplib.SMTP")
    def test_smtp_success_when_sendgrid_missing(self, mock_smtp_class):
        mock_server = MagicMock()
        mock_smtp_class.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp_class.return_value.__exit__ = MagicMock(return_value=False)

        success, bypassed = send_pin_email("test@example.com", "654321")

        assert success is True
        assert bypassed is False

    @patch("cqc_lem.utilities.email.SENDGRID_API_KEY", "sg_key")
    @patch("cqc_lem.utilities.email.SMTP_USER", "user@gmail.com")
    @patch("cqc_lem.utilities.email.SMTP_PASSWORD", "app_password")
    @patch("cqc_lem.utilities.email.smtplib.SMTP")
    @patch("sendgrid.SendGridAPIClient")
    def test_smtp_fallback_used_when_sendgrid_fails(self, mock_sg_class, mock_smtp_class):
        mock_sg = MagicMock()
        mock_sg.send.side_effect = Exception("SendGrid down")
        mock_sg_class.return_value = mock_sg

        mock_server = MagicMock()
        mock_smtp_class.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp_class.return_value.__exit__ = MagicMock(return_value=False)

        success, bypassed = send_pin_email("test@example.com", "654321")

        assert success is True
        assert bypassed is False

    @patch("cqc_lem.utilities.email.SENDGRID_API_KEY", None)
    @patch("cqc_lem.utilities.email.SMTP_USER", "user@gmail.com")
    @patch("cqc_lem.utilities.email.SMTP_PASSWORD", "app_password")
    @patch("cqc_lem.utilities.email.smtplib.SMTP")
    def test_smtp_failure_returns_false(self, mock_smtp_class):
        mock_smtp_class.side_effect = Exception("SMTP connection refused")

        success, bypassed = send_pin_email("test@example.com", "654321")

        assert success is False
        assert bypassed is False


class TestSendPinEmailBypass:
    @patch("cqc_lem.utilities.email.SENDGRID_API_KEY", None)
    @patch("cqc_lem.utilities.email.SMTP_USER", None)
    @patch("cqc_lem.utilities.email.SMTP_PASSWORD", None)
    def test_bypass_when_no_provider_configured(self):
        success, bypassed = send_pin_email("test@example.com", "123456")

        assert success is True
        assert bypassed is True

    @patch("cqc_lem.utilities.email.SENDGRID_API_KEY", None)
    @patch("cqc_lem.utilities.email.SMTP_USER", "user@gmail.com")
    @patch("cqc_lem.utilities.email.SMTP_PASSWORD", None)
    @patch("cqc_lem.utilities.email.smtplib.SMTP")
    def test_no_bypass_when_smtp_user_set_but_password_missing(self, mock_smtp_class):
        # SMTP_USER without SMTP_PASSWORD → treated as no SMTP provider → bypass
        mock_smtp_class.side_effect = Exception("Should not be called")

        success, bypassed = send_pin_email("test@example.com", "123456")

        # No provider configured (password missing), so bypass
        assert success is True
        assert bypassed is True
        mock_smtp_class.assert_not_called()


class TestSendLoginApprovalEmail:
    @patch("cqc_lem.utilities.email.SENDGRID_API_KEY", None)
    @patch("cqc_lem.utilities.email.SMTP_USER", None)
    @patch("cqc_lem.utilities.email.SMTP_PASSWORD", None)
    def test_returns_false_when_no_provider(self):
        from cqc_lem.utilities.email import send_login_approval_email
        assert send_login_approval_email("u@e.com") is False

    @patch("cqc_lem.utilities.email.SENDGRID_API_KEY", "sg_test_key")
    @patch("cqc_lem.utilities.email.SMTP_USER", None)
    @patch("cqc_lem.utilities.email.SMTP_PASSWORD", None)
    @patch("sendgrid.SendGridAPIClient")
    def test_sendgrid_sets_high_priority_headers(self, mock_sg_class):
        from cqc_lem.utilities.email import send_login_approval_email
        mock_sg = MagicMock()
        mock_sg_class.return_value = mock_sg

        sent_messages = []
        mock_sg.send.side_effect = lambda m: sent_messages.append(m)

        result = send_login_approval_email("u@e.com", vnc_url="https://vnc.example/?x=1")

        assert result is True
        mock_sg.send.assert_called_once()
        # High-priority headers must be attached
        header_blob = str(sent_messages[0].get())
        assert "X-Priority" in header_blob and "1" in header_blob
        assert "Importance" in header_blob

    @patch("cqc_lem.utilities.email.SENDGRID_API_KEY", None)
    @patch("cqc_lem.utilities.email.SMTP_USER", "user@gmail.com")
    @patch("cqc_lem.utilities.email.SMTP_PASSWORD", "app_password")
    @patch("cqc_lem.utilities.email.smtplib.SMTP")
    def test_smtp_sets_high_priority_headers(self, mock_smtp_class):
        from cqc_lem.utilities.email import send_login_approval_email
        sent = {}
        server = MagicMock()
        server.sendmail.side_effect = lambda frm, to, body: sent.update(body=body)
        mock_smtp_class.return_value.__enter__.return_value = server

        result = send_login_approval_email("u@e.com")

        assert result is True
        assert "X-Priority: 1" in sent["body"]
        assert "Importance: High" in sent["body"]

    @patch("cqc_lem.utilities.email.SENDGRID_API_KEY", None)
    @patch("cqc_lem.utilities.email.SMTP_USER", "user@gmail.com")
    @patch("cqc_lem.utilities.email.SMTP_PASSWORD", "app_password")
    @patch("cqc_lem.utilities.email.smtplib.SMTP")
    def test_smtp_failure_returns_false(self, mock_smtp_class):
        from cqc_lem.utilities.email import send_login_approval_email
        mock_smtp_class.side_effect = Exception("SMTP refused")
        assert send_login_approval_email("u@e.com") is False


class TestPinEmailContent:
    def test_html_contains_code_and_no_links(self):
        from cqc_lem.utilities.email import EMAIL_FROM_NAME, _build_pin_html
        html = _build_pin_html("123456", "sign in")
        assert "123456" in html
        assert "expires" in html.lower()
        # No links at all in a login-code email (removes link-mismatch phishing signal)
        assert "href" not in html.lower()
        assert "http://" not in html and "https://" not in html
        # Anti-phishing reassurance + brand footer
        assert "never" in html.lower()
        assert EMAIL_FROM_NAME in html

    def test_text_part_contains_code_and_company(self):
        from cqc_lem.utilities.email import EMAIL_FROM_NAME, _build_pin_text
        text = _build_pin_text("654321", "create your account")
        assert "654321" in text
        assert "create your account" in text
        assert EMAIL_FROM_NAME in text
        # plain text must not carry HTML tags
        assert "<" not in text and ">" not in text

    def test_html_to_text_strips_tags(self):
        from cqc_lem.utilities.email import _html_to_text
        out = _html_to_text("<p>Hello <strong>world</strong></p><br><a href='x'>link</a>")
        assert "<" not in out and ">" not in out
        assert "Hello" in out and "world" in out


class TestPinEmailHeadersSendGrid:
    @patch("cqc_lem.utilities.email.EMAIL_REPLY_TO", "support@christopherqueenconsulting.com")
    @patch("cqc_lem.utilities.email.SENDGRID_FROM_EMAIL", "no-reply@lem.christopherqueenconsulting.com")
    @patch("cqc_lem.utilities.email.EMAIL_FROM_NAME", "Christopher Queen Consulting")
    @patch("cqc_lem.utilities.email.SENDGRID_API_KEY", "sg_test_key")
    @patch("cqc_lem.utilities.email.SMTP_USER", None)
    @patch("cqc_lem.utilities.email.SMTP_PASSWORD", None)
    @patch("sendgrid.SendGridAPIClient")
    def test_multipart_plain_and_html_plus_from_and_replyto(self, mock_sg_class):
        mock_sg = MagicMock()
        sent = []
        mock_sg.send.side_effect = lambda m: sent.append(m)
        mock_sg_class.return_value = mock_sg

        success, bypassed = send_pin_email("user@example.com", "111222")

        assert success is True and bypassed is False
        blob = str(sent[0].get())
        # Both MIME parts present
        assert "text/plain" in blob and "text/html" in blob
        # Aligned From on the authenticated domain, friendly name, and a Reply-To
        assert "no-reply@lem.christopherqueenconsulting.com" in blob
        assert "Christopher Queen Consulting" in blob
        assert "support@christopherqueenconsulting.com" in blob


class TestPinEmailHeadersSmtp:
    @patch("cqc_lem.utilities.email.EMAIL_REPLY_TO", "support@christopherqueenconsulting.com")
    @patch("cqc_lem.utilities.email.SENDGRID_FROM_EMAIL", "no-reply@lem.christopherqueenconsulting.com")
    @patch("cqc_lem.utilities.email.EMAIL_FROM_NAME", "Christopher Queen Consulting")
    @patch("cqc_lem.utilities.email.SENDGRID_API_KEY", None)
    @patch("cqc_lem.utilities.email.SMTP_USER", "user@gmail.com")
    @patch("cqc_lem.utilities.email.SMTP_PASSWORD", "app_password")
    @patch("cqc_lem.utilities.email.smtplib.SMTP")
    def test_smtp_multipart_from_display_and_replyto(self, mock_smtp_class):
        sent = {}
        server = MagicMock()
        server.sendmail.side_effect = lambda frm, to, body: sent.update(body=body, frm=frm)
        mock_smtp_class.return_value.__enter__.return_value = server

        success, bypassed = send_pin_email("user@example.com", "333444")

        assert success is True and bypassed is False
        body = sent["body"]
        assert "multipart/alternative" in body
        assert "text/plain" in body and "text/html" in body
        # Display-name From on the authenticated domain and a Reply-To header
        assert "Christopher Queen Consulting" in body
        assert "no-reply@lem.christopherqueenconsulting.com" in body
        assert "Reply-To: support@christopherqueenconsulting.com" in body
        # Envelope sender remains the SMTP login account
        assert sent["frm"] == "user@gmail.com"
