"""Unit tests for the daily secret-encryption beat task — issue #745, PR 2a.

The task is thin on purpose (all the work is in db.encrypt_secrets_at_rest), so what's worth
asserting is the operator-facing behaviour: it says nothing changed when encryption is off, and it
escalates to WARNING while any secret is still unprotected — that number is the gate on flipping
ENCRYPTION_REQUIRED, so it must not be reported quietly.
"""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

RS = "cqc_lem.app.run_scheduler"
DB = "cqc_lem.utilities.db.encrypt_secrets_at_rest"


def _run(stats):
    from cqc_lem.app.run_scheduler import auto_encrypt_secrets_at_rest
    with patch(DB, return_value=stats), patch(f"{RS}.log_warning") as warn:
        result = auto_encrypt_secrets_at_rest.run()
    return result, warn


class TestEncryptSecretsTask:
    def test_no_key_reports_disabled_and_does_not_warn(self):
        result, warn = _run({"enabled": False, "scanned": 0, "rewritten": 0,
                             "failed": 0, "plaintext_remaining": 0})
        assert "Encryption disabled" in result
        warn.assert_not_called()

    def test_clean_run_reports_the_count_without_warning(self):
        result, warn = _run({"enabled": True, "scanned": 3, "rewritten": 3,
                             "failed": 0, "plaintext_remaining": 0})
        assert "Re-encrypted 3 secret(s)" in result
        warn.assert_not_called()

    def test_remaining_plaintext_is_warned_not_swallowed(self):
        result, warn = _run({"enabled": True, "scanned": 4, "rewritten": 2,
                             "failed": 2, "plaintext_remaining": 2})
        assert "2 still unprotected" in result
        warn.assert_called_once()
        assert "still unencrypted at rest" in warn.call_args[0][0]

    def test_idempotent_day_is_a_no_op(self):
        result, warn = _run({"enabled": True, "scanned": 0, "rewritten": 0,
                             "failed": 0, "plaintext_remaining": 0})
        assert "Re-encrypted 0 secret(s)" in result
        warn.assert_not_called()


def test_task_is_on_the_beat_schedule():
    """A backfill nobody schedules is a backfill nobody runs — and this pass is also the key
    rotation, so it has to be recurring, not one-shot.
    """
    from cqc_lem.app.my_celery import app
    entry = app.conf.beat_schedule["encrypt-secrets-at-rest"]
    assert entry["task"] == "cqc_lem.app.run_scheduler.auto_encrypt_secrets_at_rest"
