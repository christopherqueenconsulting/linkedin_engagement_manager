"""Tests for scripts/install_watchdog.sh's alert-recipient handling (#1804).

The installer used to write `WATCHDOG_ALERT_EMAIL=you@example.com` verbatim on `--` and, worse,
report an already-placeholder value back as "already set" on every idempotent re-run — actively
reassuring the operator that the broken state was correct. `resolve_and_write_alert_email` is
sourced in isolation here (no root/systemd needed) to cover both.
"""

import os
import subprocess
from pathlib import Path

INSTALL_SH = Path(__file__).resolve().parents[3] / "scripts" / "install_watchdog.sh"


def _run(tmp_path: Path, bash_code: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["INSTALL_SH"] = str(INSTALL_SH)
    return subprocess.run(
        ["bash", "-c", f'source "$INSTALL_SH"; {bash_code}'],
        capture_output=True,
        text=True,
        env=env,
    )


class TestResolveAndWriteAlertEmail:
    def test_real_address_argument_is_written(self, tmp_path: Path) -> None:
        envf = tmp_path / ".env"
        envf.write_text("UNRELATED=1\n", encoding="utf-8")
        result = _run(tmp_path, f'resolve_and_write_alert_email "ops@realcompany.com" "{envf}"')
        assert result.returncode == 0, result.stderr
        assert "WATCHDOG_ALERT_EMAIL=ops@realcompany.com" in envf.read_text()

    def test_placeholder_argument_is_refused_not_written(self, tmp_path: Path) -> None:
        envf = tmp_path / ".env"
        envf.write_text("UNRELATED=1\n", encoding="utf-8")
        result = _run(tmp_path, f'resolve_and_write_alert_email "you@example.com" "{envf}"')
        assert result.returncode != 0
        assert "refusing to write placeholder" in result.stderr
        assert "WATCHDOG_ALERT_EMAIL" not in envf.read_text()

    def test_changeme_argument_is_also_refused(self, tmp_path: Path) -> None:
        envf = tmp_path / ".env"
        envf.write_text("", encoding="utf-8")
        result = _run(tmp_path, f'resolve_and_write_alert_email "changeme@realcompany.com" "{envf}"')
        assert result.returncode != 0
        assert "WATCHDOG_ALERT_EMAIL" not in envf.read_text()

    def test_existing_placeholder_is_not_reported_as_already_set(self, tmp_path: Path) -> None:
        envf = tmp_path / ".env"
        envf.write_text("WATCHDOG_ALERT_EMAIL=you@example.com\n", encoding="utf-8")
        result = _run(tmp_path, f'resolve_and_write_alert_email "" "{envf}"')
        assert result.returncode == 0, result.stderr
        assert "already set" not in result.stdout
        assert "treating it as unset" in result.stdout
        assert "EMAIL ALERTS WILL NOT SEND" in result.stdout

    def test_existing_placeholder_falls_back_to_cost_alert_email_warning(self, tmp_path: Path) -> None:
        envf = tmp_path / ".env"
        envf.write_text(
            "WATCHDOG_ALERT_EMAIL=you@example.com\nCOST_ALERT_EMAIL=finance@realcompany.com\n",
            encoding="utf-8",
        )
        result = _run(tmp_path, f'resolve_and_write_alert_email "" "{envf}"')
        assert result.returncode == 0, result.stderr
        assert "already set" not in result.stdout
        assert "will fall back to COST_ALERT_EMAIL (finance@realcompany.com)" in result.stdout

    def test_existing_real_value_is_reported_as_already_set(self, tmp_path: Path) -> None:
        envf = tmp_path / ".env"
        envf.write_text("WATCHDOG_ALERT_EMAIL=ops@realcompany.com\n", encoding="utf-8")
        result = _run(tmp_path, f'resolve_and_write_alert_email "" "{envf}"')
        assert result.returncode == 0, result.stderr
        assert "already set to ops@realcompany.com" in result.stdout

    def test_no_value_anywhere_warns_alerts_will_not_send(self, tmp_path: Path) -> None:
        envf = tmp_path / ".env"
        envf.write_text("UNRELATED=1\n", encoding="utf-8")
        result = _run(tmp_path, f'resolve_and_write_alert_email "" "{envf}"')
        assert result.returncode == 0, result.stderr
        assert "EMAIL ALERTS WILL NOT SEND" in result.stdout
