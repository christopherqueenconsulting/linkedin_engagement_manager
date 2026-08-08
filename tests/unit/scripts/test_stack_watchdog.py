"""Regression tests for the backup-freshness check in scripts/stack_watchdog.sh (#1090)."""

import os
import subprocess
import time
from pathlib import Path

WATCHDOG_SH = Path(__file__).resolve().parents[3] / "scripts" / "stack_watchdog.sh"


def _touch(tmp_path: Path, pattern: str, age_hours: float, size_bytes: int = 2048) -> Path:
    """Create a file with the given name pattern, mtime and size."""
    f = tmp_path / pattern
    f.write_bytes(b"x" * size_bytes)
    mtime = time.time() - (age_hours * 3600)
    os.utime(f, (mtime, mtime))
    return f


def _run(tmp_path: Path, bash_code: str) -> subprocess.CompletedProcess[str]:
    """Source stack_watchdog.sh with a fake state/env dir and run bash_code."""
    env = os.environ.copy()
    env["LEM_DIR"] = str(tmp_path)
    env["LEM_ENV_FILE"] = str(tmp_path / ".env")
    env["WATCHDOG_STATE_DIR"] = str(tmp_path / "state")
    env["WATCHDOG_BACKUP_AGE_HOURS"] = "48"
    env["WATCHDOG_SH"] = str(WATCHDOG_SH)

    # A minimal .env so env_value never falls through to a real /opt/lem/.env.
    (tmp_path / ".env").write_text("POSTHOG_API_KEY=\n", encoding="utf-8")

    return subprocess.run(
        ["bash", "-c", bash_code],
        capture_output=True,
        text=True,
        env=env,
    )


class TestBackupFreshness:
    def test_fresh_db_backup_is_not_down(self, tmp_path: Path) -> None:
        backups = tmp_path / "backups"
        backups.mkdir()
        (tmp_path / ".env").write_text(f"BACKUP_DIR={backups}\n", encoding="utf-8")
        _touch(backups, "db-20260808-030001.sql.gz", age_hours=12)

        result = _run(
            tmp_path,
            'source "$WATCHDOG_SH"; check_backup_freshness; echo "down=${#down[@]}"; printf "%s\\n" "${down[@]}"',
        )
        assert result.returncode == 0, result.stderr + result.stdout
        assert "down=0" in result.stdout

    def test_stale_db_backup_is_reported_down(self, tmp_path: Path) -> None:
        backups = tmp_path / "backups"
        backups.mkdir()
        (tmp_path / ".env").write_text(f"BACKUP_DIR={backups}\n", encoding="utf-8")
        _touch(backups, "db-20260806-030001.sql.gz", age_hours=50)

        result = _run(
            tmp_path,
            'source "$WATCHDOG_SH"; check_backup_freshness; echo "down=${#down[@]}"; printf "%s\\n" "${down[@]}"',
        )
        assert result.returncode == 0, result.stderr + result.stdout
        assert "down=1" in result.stdout
        assert "backup:db:stale:" in result.stdout

    def test_missing_db_backup_is_reported_down(self, tmp_path: Path) -> None:
        backups = tmp_path / "backups"
        backups.mkdir()
        (tmp_path / ".env").write_text(f"BACKUP_DIR={backups}\n", encoding="utf-8")

        result = _run(
            tmp_path,
            'source "$WATCHDOG_SH"; check_backup_freshness; echo "down=${#down[@]}"; printf "%s\\n" "${down[@]}"',
        )
        assert result.returncode == 0, result.stderr + result.stdout
        assert "down=1" in result.stdout
        assert "backup:db:missing" in result.stdout

    def test_fresh_but_empty_db_backup_is_reported_down(self, tmp_path: Path) -> None:
        """A dump that failed still leaves a valid, FRESH 20-byte .gz — age alone would pass it."""
        backups = tmp_path / "backups"
        backups.mkdir()
        (tmp_path / ".env").write_text(f"BACKUP_DIR={backups}\n", encoding="utf-8")
        _touch(backups, "db-20260808-030001.sql.gz", age_hours=1, size_bytes=20)

        result = _run(
            tmp_path,
            'source "$WATCHDOG_SH"; check_backup_freshness; echo "down=${#down[@]}"; printf "%s\\n" "${down[@]}"',
        )
        assert result.returncode == 0, result.stderr + result.stdout
        assert "down=1" in result.stdout
        assert "backup:db:empty:20b" in result.stdout

    def test_missing_backup_directory_is_reported_down(self, tmp_path: Path) -> None:
        """No backups directory at all is the same fault as no dump inside one."""
        (tmp_path / ".env").write_text(
            f"BACKUP_DIR={tmp_path / 'nope'}\n", encoding="utf-8"
        )

        result = _run(
            tmp_path,
            'source "$WATCHDOG_SH"; check_backup_freshness; echo "down=${#down[@]}"; printf "%s\\n" "${down[@]}"',
        )
        assert result.returncode == 0, result.stderr + result.stdout
        assert "down=1" in result.stdout
        assert "backup:db:missing" in result.stdout

    def test_stale_chrome_profile_backup_warns_but_is_not_down(self, tmp_path: Path) -> None:
        """A decommissioned chrome-profile volume must not alert forever — cookies live in the DB."""
        backups = tmp_path / "backups"
        backups.mkdir()
        (tmp_path / ".env").write_text(f"BACKUP_DIR={backups}\n", encoding="utf-8")
        _touch(backups, "db-20260808-030001.sql.gz", age_hours=12)
        _touch(backups, "chrome-profile-20260806-030001.tar.gz", age_hours=50, size_bytes=500)

        result = _run(
            tmp_path,
            'source "$WATCHDOG_SH"; check_backup_freshness; echo "down=${#down[@]}"; printf "%s\\n" "${down[@]}"',
        )
        assert result.returncode == 0, result.stderr + result.stdout
        assert "down=0" in result.stdout
        assert "50h old" in result.stdout

    def test_missing_chrome_profile_backup_is_not_down(self, tmp_path: Path) -> None:
        backups = tmp_path / "backups"
        backups.mkdir()
        (tmp_path / ".env").write_text(f"BACKUP_DIR={backups}\n", encoding="utf-8")
        _touch(backups, "db-20260808-030001.sql.gz", age_hours=12)

        result = _run(
            tmp_path,
            'source "$WATCHDOG_SH"; check_backup_freshness; echo "down=${#down[@]}"; printf "%s\\n" "${down[@]}"',
        )
        assert result.returncode == 0, result.stderr + result.stdout
        assert "down=0" in result.stdout
        assert "chrome-profile" not in result.stdout

    def test_tiny_chrome_profile_archive_logs_warning(self, tmp_path: Path) -> None:
        backups = tmp_path / "backups"
        backups.mkdir()
        (tmp_path / ".env").write_text(f"BACKUP_DIR={backups}\n", encoding="utf-8")
        _touch(backups, "db-20260808-030001.sql.gz", age_hours=12)
        _touch(backups, "chrome-profile-20260808-030001.tar.gz", age_hours=12, size_bytes=87)

        result = _run(
            tmp_path,
            'source "$WATCHDOG_SH"; check_backup_freshness',
        )
        assert result.returncode == 0, result.stderr + result.stdout
        assert "only 87 bytes" in result.stdout
        assert "cookies now live encrypted in the database" in result.stdout
        assert "down=0" not in result.stdout  # not a down event
