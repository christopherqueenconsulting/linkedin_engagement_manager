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


class TestTunnelOrigins:
    """The tunnel-origin check (`check_tunnel_origins`).

    The fault it exists for: cloudflared running and green while every request it forwards is
    dropped at the origin. A ufw rule pinned to a container IP stopped matching after cloudflared
    was handed a new one, and GitHub webhook deliveries timed out for fifteen days with nothing red.
    """

    @staticmethod
    def _fake_docker(tmp_path: Path, *, status: str, logs: str, logs_rc: int = 0) -> Path:
        """A `docker` stub on PATH — the cloudflared image has no shell, so the real check reads logs."""
        bindir = tmp_path / "bin"
        bindir.mkdir(exist_ok=True)
        stub = bindir / "docker"
        stub.write_text(
            "#!/usr/bin/env bash\n"
            'if [[ "$1" == "inspect" ]]; then\n'
            f'  printf "%s\\n" {status!r}\n'
            "  exit 0\n"
            "fi\n"
            'if [[ "$1" == "logs" ]]; then\n'
            f"  cat <<'LOGEOF'\n{logs}\nLOGEOF\n"
            f"  exit {logs_rc}\n"
            "fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        stub.chmod(0o755)
        return bindir

    def _run_check(self, tmp_path: Path, bindir: Path, extra: str = "") -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["LEM_DIR"] = str(tmp_path)
        env["LEM_ENV_FILE"] = str(tmp_path / ".env")
        env["WATCHDOG_STATE_DIR"] = str(tmp_path / "state")
        env["WATCHDOG_SH"] = str(WATCHDOG_SH)
        env["PATH"] = f"{bindir}:{env['PATH']}"
        (tmp_path / ".env").write_text("POSTHOG_API_KEY=\n", encoding="utf-8")
        return subprocess.run(
            [
                "bash",
                "-c",
                f'source "$WATCHDOG_SH"; {extra} check_tunnel_origins; '
                'echo "down=${#down[@]}"; echo "recovered=${#recovered[@]}"; '
                'printf "%s\\n" "${down[@]}"',
            ],
            capture_output=True,
            text=True,
            env=env,
        )

    ERRORS = "\n".join(
        'ERR error="Unable to reach the origin service. The service may be down or it may not be '
        'responding to traffic from cloudflared: dial tcp 172.18.0.1:8420: i/o timeout" '
        "connIndex=0 ingressRule=5 originService=http://172.18.0.1:8420"
        for _ in range(6)
    )

    def test_healthy_tunnel_is_not_down(self, tmp_path: Path) -> None:
        bindir = self._fake_docker(tmp_path, status="running", logs="INF Registered tunnel connection")
        result = self._run_check(tmp_path, bindir)
        assert result.returncode == 0, result.stderr + result.stdout
        assert "down=0" in result.stdout

    def test_errors_under_threshold_are_not_down(self, tmp_path: Path) -> None:
        one = self.ERRORS.split("\n")[0]
        bindir = self._fake_docker(tmp_path, status="running", logs=one)
        result = self._run_check(tmp_path, bindir)
        assert "down=0" in result.stdout

    def test_first_sighting_starts_a_grace_window_and_does_not_alert(self, tmp_path: Path) -> None:
        """A deploy recreates an origin container; the first burst of errors must not page."""
        bindir = self._fake_docker(tmp_path, status="running", logs=self.ERRORS)
        result = self._run_check(tmp_path, bindir)
        assert "down=0" in result.stdout
        assert "starting grace window" in result.stdout
        assert (tmp_path / "state" / "tunnel-origin.down").exists()

    def test_still_failing_past_grace_is_reported_down_and_names_the_origin(self, tmp_path: Path) -> None:
        bindir = self._fake_docker(tmp_path, status="running", logs=self.ERRORS)
        state = tmp_path / "state"
        state.mkdir(exist_ok=True)
        # First seen well outside the grace window.
        (state / "tunnel-origin.down").write_text(f"{int(time.time()) - 9999} http://172.18.0.1:8420\n")

        result = self._run_check(tmp_path, bindir)
        assert "down=1" in result.stdout
        # The alert must name the origin, so it points at the rule to fix, not at "the tunnel".
        assert "tunnel-origin:http://172.18.0.1:8420" in result.stdout

    def test_recovery_clears_the_marker(self, tmp_path: Path) -> None:
        bindir = self._fake_docker(tmp_path, status="running", logs="INF Registered tunnel connection")
        state = tmp_path / "state"
        state.mkdir(exist_ok=True)
        marker = state / "tunnel-origin.down"
        marker.write_text(f"{int(time.time()) - 9999} http://172.18.0.1:8420\n")

        result = self._run_check(tmp_path, bindir)
        assert "down=0" in result.stdout
        assert "recovered=1" in result.stdout
        assert not marker.exists()

    def test_missing_container_is_unreadable_not_a_fault(self, tmp_path: Path) -> None:
        """Absence of Docker is not evidence of an outage — it must never alert."""
        bindir = tmp_path / "bin"
        bindir.mkdir(exist_ok=True)
        stub = bindir / "docker"
        stub.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
        stub.chmod(0o755)

        result = self._run_check(tmp_path, bindir)
        assert "down=0" in result.stdout
        assert "skipping tunnel origin check" in result.stdout

    def test_stopped_container_is_left_to_check_services(self, tmp_path: Path) -> None:
        """One incident, one row: check_services already reports a container that is not running."""
        bindir = self._fake_docker(tmp_path, status="exited", logs=self.ERRORS)
        result = self._run_check(tmp_path, bindir)
        assert "down=0" in result.stdout


class TestAlertEmailResolution:
    """Recipient resolution (`resolve_alert_email`, #1804).

    `WATCHDOG_ALERT_EMAIL=you@example.com` — a copy-pasted placeholder from docs/install-script
    sample text — silently disabled alerting for an unknown length of time: `${TO:-fallback}` only
    ever catches empty, never garbage, so the placeholder beat the correct `COST_ALERT_EMAIL` sitting
    three lines below it in the same `.env`.
    """

    def _resolve(
        self, tmp_path: Path, env_contents: str, extra_env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.pop("WATCHDOG_ALERT_EMAIL", None)
        env["LEM_DIR"] = str(tmp_path)
        env["LEM_ENV_FILE"] = str(tmp_path / ".env")
        env["WATCHDOG_STATE_DIR"] = str(tmp_path / "state")
        env["WATCHDOG_SH"] = str(WATCHDOG_SH)
        env.update(extra_env or {})
        (tmp_path / ".env").write_text(env_contents, encoding="utf-8")
        return subprocess.run(
            ["bash", "-c", 'source "$WATCHDOG_SH"; resolve_alert_email'],
            capture_output=True,
            text=True,
            env=env,
        )

    def test_valid_configured_value_wins(self, tmp_path: Path) -> None:
        result = self._resolve(tmp_path, "WATCHDOG_ALERT_EMAIL=ops@realcompany.com\n")
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "ops@realcompany.com"
        assert "ERROR" not in result.stderr + result.stdout

    def test_placeholder_is_rejected_and_falls_through_to_cost_alert_email(self, tmp_path: Path) -> None:
        result = self._resolve(
            tmp_path,
            "WATCHDOG_ALERT_EMAIL=you@example.com\nCOST_ALERT_EMAIL=finance@realcompany.com\n",
        )
        assert result.returncode == 0, result.stderr
        # Every call site does `TO="$(resolve_alert_email)"` — stdout IS the resolved recipient, so
        # it must be the address alone, never an ERROR line mixed in (that corrupts the "to" address
        # SendGrid receives instead of just being visible).
        assert result.stdout.strip() == "finance@realcompany.com"
        # Discarding a configured-but-bad value is still logged loudly (to stderr, so it reaches the
        # journal without landing in the captured recipient), not silently ignored.
        assert "ERROR" in result.stderr
        assert "you@example.com" in result.stderr

    def test_changeme_placeholder_is_also_rejected(self, tmp_path: Path) -> None:
        result = self._resolve(
            tmp_path,
            "WATCHDOG_ALERT_EMAIL=changeme@realcompany.com\nCOST_ALERT_EMAIL=finance@realcompany.com\n",
        )
        assert result.stdout.strip().splitlines()[-1] == "finance@realcompany.com"

    def test_empty_value_falls_through(self, tmp_path: Path) -> None:
        result = self._resolve(tmp_path, "WATCHDOG_ALERT_EMAIL=\nCOST_ALERT_EMAIL=finance@realcompany.com\n")
        assert result.stdout.strip().splitlines()[-1] == "finance@realcompany.com"

    def test_placeholder_cost_alert_email_also_falls_through_to_terminal_default(self, tmp_path: Path) -> None:
        result = self._resolve(
            tmp_path,
            "WATCHDOG_ALERT_EMAIL=you@example.com\nCOST_ALERT_EMAIL=billing@example.org\n",
        )
        assert result.stdout.strip() == "christopher.queen@gmail.com"
        assert result.stderr.count("ERROR") == 2  # both discards logged, on stderr

    def test_nothing_configured_terminates_in_a_real_address(self, tmp_path: Path) -> None:
        result = self._resolve(tmp_path, "")
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip().splitlines()[-1] == "christopher.queen@gmail.com"

    def test_process_env_var_overrides_env_file(self, tmp_path: Path) -> None:
        result = self._resolve(
            tmp_path,
            "WATCHDOG_ALERT_EMAIL=fromfile@realcompany.com\n",
            extra_env={"WATCHDOG_ALERT_EMAIL": "fromenv@realcompany.com"},
        )
        assert result.stdout.strip().splitlines()[-1] == "fromenv@realcompany.com"
