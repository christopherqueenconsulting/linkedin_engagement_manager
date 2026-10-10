"""Regression tests for scripts/backup.sh (issue #1090)."""

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

BACKUP_SH = Path(__file__).resolve().parents[3] / "scripts" / "backup.sh"


_FAKE_DOCKER = textwrap.dedent(
    """\
    #!/bin/sh
    # Fake docker for unit tests. Handles the three calls backup.sh makes.
    set -e
    case "$1" in
      exec)
        shift
        # Look for the mysqldump keyword; everything else is ignored.
        if echo "$*" | grep -q mysqldump; then
          case "${BACKUP_DUMP_MODE:-ok}" in
            fail)
              # The #1090 outage shape: mysqldump errors out. `set -o pipefail` aborts the
              # script, and gzip has already created the (partial, useless) output file.
              echo "mysqldump: Got error: 1045: Access denied" >&2
              exit 1
              ;;
            empty)
              # No SQL output; the real `gzip` on the pipe will produce a valid
              # gzip with 0 uncompressed bytes, which the freshness guard rejects.
              exit 0
              ;;
            *)
              # A valid gzipped SQL-ish payload. backup.sh checks gzip validity,
              # file size, and uncompressed size.
              printf '%s\\n' '-- dummy dump' 'CREATE TABLE t (id INT);' 'INSERT INTO t VALUES (1);' | gzip -c
              exit 0
              ;;
          esac
        fi
        exit 0
        ;;
      volume)
        shift
        if [ "$1" = "ls" ]; then
          if [ "${BACKUP_CHROME_VOL:-1}" = "1" ]; then
            echo "lem_chrome-profile"
          fi
        fi
        exit 0
        ;;
      run)
        shift
        # docker run --rm -v vol:/data:ro -v dir:/backup alpine tar czf /backup/chrome-profile-....tar.gz -C /data .
        # Parse the host backup dir from -v mounts and the output path from tar czf,
        # then create the archive directly on the host.
        host_backup=""
        out=""
        while [ $# -gt 0 ]; do
          case "$1" in
            -v)
              mount="$2"
              case "$mount" in
                *:/backup)   host_backup="${mount%:/backup}" ;;
                *:/backup:*) host_backup="${mount%:/backup:*}" ;;
              esac
              shift 2 ;;
            czf) out="$2"; shift 2 ;;
            --rm|alpine|tar) shift ;;
            -C|/data|.) shift ;;
            *) shift ;;
          esac
        done
        if [ -n "$out" ]; then
          if [ -n "$host_backup" ] && [ "${out#/backup/}" != "$out" ]; then
            out="${host_backup}/${out#/backup/}"
          fi
          mkdir -p "$(dirname "$out")"
          size="${BACKUP_CHROME_SIZE:-500}"
          dd if=/dev/zero bs=1 count="$size" of="$out" 2>/dev/null
        fi
        exit 0
        ;;
    esac
    exit 0
    """
)


def _write_env(tmp_path: Path, **values: str) -> Path:
    """Write a fake .env file, deliberately including unquoted spaced values."""
    env_file = tmp_path / ".env"
    lines = [
        "# Fake .env with unquoted spaced values — must not be sourced raw",
        "EMAIL_FROM_NAME=Acme Engagement Manager",
        "SOME_SHELL_WORD=rm -rf /",
        "MYSQL_HOST=mysql_db",
        "MYSQL_DATABASE=linkedin_manager",
        f"MYSQL_ROOT_PASSWORD={values.get('password', 'secret')}",
    ]
    if "backup_dir" in values:
        lines.append(f"BACKUP_DIR={values['backup_dir']}")
    if "backup_remote" in values:
        lines.append(f"BACKUP_REMOTE={values['backup_remote']}")
    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return env_file


def _run(
    tmp_path: Path,
    env_overrides: dict[str, str],
    bash_code: str = 'source "$BACKUP_SH"; backup',
) -> subprocess.CompletedProcess[str]:
    """Source backup.sh in a subshell with a fake docker on PATH and run bash_code."""
    fake = tmp_path / "docker"
    fake.write_text(_FAKE_DOCKER, encoding="utf-8")
    fake.chmod(0o755)

    env = os.environ.copy()
    # tests/conftest.py calls load_dotenv(), so a dev box with a real .env would export the very
    # keys backup.sh prefers over the file — sending the dump somewhere real, or (via
    # BACKUP_REMOTE) rclone-ing it to production storage from a unit test.
    for leaked in (
        "MYSQL_HOST",
        "MYSQL_DATABASE",
        "MYSQL_ROOT_PASSWORD",
        "BACKUP_DIR",
        "RETAIN_DAYS",
        "BACKUP_REMOTE",
    ):
        env.pop(leaked, None)
    env.update(env_overrides)
    env["PATH"] = f"{tmp_path}{os.pathsep}{env.get('PATH', '')}"
    env["BACKUP_SH"] = str(BACKUP_SH)

    return subprocess.run(
        ["bash", "-c", bash_code],
        capture_output=True,
        text=True,
        cwd=str(BACKUP_SH.parent.parent),
        env=env,
    )


class TestEnvParsing:
    """The 2026-07-08 outage: sourcing an unquoted spaced value tried to execute it."""

    def test_env_value_reads_mysql_values(self, tmp_path: Path) -> None:
        env_file = _write_env(tmp_path, password="hunter2")
        result = _run(
            tmp_path,
            {"LEM_ENV_FILE": str(env_file)},
            'source "$BACKUP_SH"; echo "host=$(env_value MYSQL_HOST)"'
            ' "db=$(env_value MYSQL_DATABASE)" "pw=$(env_value MYSQL_ROOT_PASSWORD)"',
        )
        assert result.returncode == 0, result.stderr + result.stdout
        assert "host=mysql_db" in result.stdout
        assert "db=linkedin_manager" in result.stdout
        assert "pw=hunter2" in result.stdout

    def test_env_value_tolerates_unquoted_spaced_values(self, tmp_path: Path) -> None:
        """An unquoted value containing spaces is returned as a literal string.

        It must never be interpreted as a command.
        """
        env_file = _write_env(tmp_path, password="secret")
        result = _run(
            tmp_path,
            {"LEM_ENV_FILE": str(env_file)},
            'source "$BACKUP_SH"; echo "name=$(env_value EMAIL_FROM_NAME)"',
        )
        assert result.returncode == 0, result.stderr + result.stdout
        assert "name=Acme Engagement Manager" in result.stdout
        # The dangerous shell word should also be returned literally.
        result2 = _run(
            tmp_path,
            {"LEM_ENV_FILE": str(env_file)},
            'source "$BACKUP_SH"; echo "word=$(env_value SOME_SHELL_WORD)"',
        )
        assert result2.returncode == 0, result2.stderr + result2.stdout
        assert "word=rm -rf /" in result2.stdout


class TestBackupRun:
    """End-to-end backup.sh run with a fake docker."""

    def test_successful_run_creates_dump_and_skips_chrome_when_no_volume(self, tmp_path: Path) -> None:
        backup_dir = tmp_path / "backups"
        env_file = _write_env(tmp_path, password="secret", backup_dir=str(backup_dir))
        result = _run(
            tmp_path,
            {
                "LEM_ENV_FILE": str(env_file),
                "BACKUP_CHROME_VOL": "0",
            },
            '"$BACKUP_SH"',
        )
        assert result.returncode == 0, result.stderr + result.stdout
        dumps = list(backup_dir.glob("db-*.sql.gz"))
        assert len(dumps) == 1
        assert "MySQL dump OK" in result.stdout
        assert "no *_chrome-profile volume found" in result.stdout
        assert "done" in result.stdout

    def test_empty_dump_exits_nonzero(self, tmp_path: Path) -> None:
        backup_dir = tmp_path / "backups"
        env_file = _write_env(tmp_path, password="secret", backup_dir=str(backup_dir))
        result = _run(
            tmp_path,
            {
                "LEM_ENV_FILE": str(env_file),
                "BACKUP_DUMP_MODE": "empty",
                "BACKUP_CHROME_VOL": "0",
            },
            '"$BACKUP_SH"',
        )
        assert result.returncode != 0, result.stdout
        assert "ERROR: MySQL dump produced empty uncompressed output" in (result.stdout + result.stderr)

    def test_empty_dump_leaves_no_artifact_behind(self, tmp_path: Path) -> None:
        """A husk .gz is FRESH, so the watchdog's age check would read it as a healthy backup."""
        backup_dir = tmp_path / "backups"
        env_file = _write_env(tmp_path, password="secret", backup_dir=str(backup_dir))
        result = _run(
            tmp_path,
            {
                "LEM_ENV_FILE": str(env_file),
                "BACKUP_DUMP_MODE": "empty",
                "BACKUP_CHROME_VOL": "0",
            },
            '"$BACKUP_SH"',
        )
        assert result.returncode != 0, result.stdout
        assert list(backup_dir.glob("db-*.sql.gz")) == []

    def test_failed_mysqldump_exits_nonzero_and_leaves_no_artifact(self, tmp_path: Path) -> None:
        """`set -o pipefail` aborts before the guards run, so cleanup must be a trap, not a branch."""
        backup_dir = tmp_path / "backups"
        env_file = _write_env(tmp_path, password="secret", backup_dir=str(backup_dir))
        result = _run(
            tmp_path,
            {
                "LEM_ENV_FILE": str(env_file),
                "BACKUP_DUMP_MODE": "fail",
                "BACKUP_CHROME_VOL": "0",
            },
            '"$BACKUP_SH"',
        )
        assert result.returncode != 0, result.stdout
        assert list(backup_dir.glob("db-*.sql.gz")) == []

    def test_tiny_chrome_profile_logs_warning_but_does_not_fail(self, tmp_path: Path) -> None:
        backup_dir = tmp_path / "backups"
        env_file = _write_env(tmp_path, password="secret", backup_dir=str(backup_dir))
        result = _run(
            tmp_path,
            {
                "LEM_ENV_FILE": str(env_file),
                "BACKUP_CHROME_VOL": "1",
                "BACKUP_CHROME_SIZE": "87",
            },
            '"$BACKUP_SH"',
        )
        assert result.returncode == 0, result.stderr + result.stdout
        assert "chrome-profile archive is only 87 bytes" in result.stdout
        assert "cookies now live encrypted in the database" in result.stdout

    def test_missing_required_database_exits_nonzero(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("MYSQL_HOST=mysql_db\nMYSQL_ROOT_PASSWORD=secret\n", encoding="utf-8")
        result = _run(
            tmp_path,
            {"LEM_ENV_FILE": str(env_file), "BACKUP_CHROME_VOL": "0"},
            '"$BACKUP_SH"',
        )
        assert result.returncode != 0, result.stdout
        assert "ERROR: MYSQL_DATABASE not set" in (result.stdout + result.stderr)


_FAKE_RCLONE = textwrap.dedent(
    """\
    #!/bin/sh
    # Fake rclone. `listremotes --long` reports one remote, lem-offsite, of type
    # FAKE_RCLONE_TYPE (default crypt). Any other call records its argv and exits FAKE_RCLONE_RC.
    if [ "$1" = "listremotes" ]; then
      echo "lem-offsite: ${FAKE_RCLONE_TYPE:-crypt}"
      exit 0
    fi
    printf '%s\\n' "$*" >> "$FAKE_RCLONE_LOG"
    exit "${FAKE_RCLONE_RC:-0}"
    """
)


def _install_fake_rclone(tmp_path: Path) -> Path:
    """Put a recording fake rclone on the PATH _run builds; return the file its argv lands in."""
    fake = tmp_path / "rclone"
    fake.write_text(_FAKE_RCLONE, encoding="utf-8")
    fake.chmod(0o755)
    return tmp_path / "rclone-calls.log"


class TestOffsiteCopy:
    """The off-host copy of the nightly dump (docs/offsite-backups.md)."""

    def test_unset_remote_says_so_and_never_calls_rclone(self, tmp_path: Path) -> None:
        backup_dir = tmp_path / "backups"
        env_file = _write_env(tmp_path, backup_dir=str(backup_dir))
        calls = _install_fake_rclone(tmp_path)
        result = _run(
            tmp_path,
            {"LEM_ENV_FILE": str(env_file), "BACKUP_CHROME_VOL": "0", "FAKE_RCLONE_LOG": str(calls)},
            '"$BACKUP_SH"',
        )
        assert result.returncode == 0, result.stderr + result.stdout
        assert "off-host copy: not configured" in result.stdout
        assert not calls.exists()
        assert not (backup_dir / ".offsite-last-ok").exists()

    def test_copies_only_the_new_dump_immutably(self, tmp_path: Path) -> None:
        backup_dir = tmp_path / "backups"
        env_file = _write_env(tmp_path, backup_dir=str(backup_dir), backup_remote="lem-offsite:")
        calls = _install_fake_rclone(tmp_path)
        result = _run(
            tmp_path,
            {"LEM_ENV_FILE": str(env_file), "BACKUP_CHROME_VOL": "1", "FAKE_RCLONE_LOG": str(calls)},
            '"$BACKUP_SH"',
        )
        assert result.returncode == 0, result.stderr + result.stdout
        (dump,) = backup_dir.glob("db-*.sql.gz")
        assert calls.read_text(encoding="utf-8").splitlines() == [
            f"copyto --immutable {dump} lem-offsite:{dump.name}"
        ]
        assert f"off-host copy OK: {dump.name}" in result.stdout
        assert (backup_dir / ".offsite-last-ok").read_text(encoding="utf-8").strip() == dump.name

    def test_remote_with_a_path_gets_one_separator(self, tmp_path: Path) -> None:
        backup_dir = tmp_path / "backups"
        env_file = _write_env(tmp_path, backup_dir=str(backup_dir), backup_remote="lem-offsite:db/")
        calls = _install_fake_rclone(tmp_path)
        result = _run(
            tmp_path,
            {"LEM_ENV_FILE": str(env_file), "BACKUP_CHROME_VOL": "0", "FAKE_RCLONE_LOG": str(calls)},
            '"$BACKUP_SH"',
        )
        assert result.returncode == 0, result.stderr + result.stdout
        (dump,) = backup_dir.glob("db-*.sql.gz")
        assert calls.read_text(encoding="utf-8").split()[-1] == f"lem-offsite:db/{dump.name}"

    def test_failed_copy_fails_the_run_and_keeps_the_local_dump(self, tmp_path: Path) -> None:
        backup_dir = tmp_path / "backups"
        env_file = _write_env(tmp_path, backup_dir=str(backup_dir), backup_remote="lem-offsite:")
        calls = _install_fake_rclone(tmp_path)
        result = _run(
            tmp_path,
            {
                "LEM_ENV_FILE": str(env_file),
                "BACKUP_CHROME_VOL": "0",
                "FAKE_RCLONE_LOG": str(calls),
                "FAKE_RCLONE_RC": "1",
            },
            '"$BACKUP_SH"',
        )
        assert result.returncode != 0, result.stdout
        assert "ERROR: off-host copy failed" in result.stderr
        assert len(list(backup_dir.glob("db-*.sql.gz"))) == 1
        assert not (backup_dir / ".offsite-last-ok").exists()

    def test_remote_set_without_rclone_fails_the_run(self, tmp_path: Path) -> None:
        backup_dir = tmp_path / "backups"
        env_file = _write_env(tmp_path, backup_dir=str(backup_dir), backup_remote="lem-offsite:")
        # No fake rclone, and a PATH holding only the tools backup.sh needs, so a real rclone on
        # the test host cannot be found either.
        tools = tmp_path / "tools"
        tools.mkdir()
        for tool in ("bash", "date", "mkdir", "gzip", "stat", "tail", "awk", "head", "find",
                     "basename", "sed", "dirname", "cat", "grep", "sh", "printf", "dd"):
            found = shutil.which(tool)
            if found:
                (tools / tool).symlink_to(found)
        result = _run(
            tmp_path,
            {"LEM_ENV_FILE": str(env_file), "BACKUP_CHROME_VOL": "0"},
            f'PATH="{tmp_path}:{tools}" "$BACKUP_SH"',
        )
        assert result.returncode != 0, result.stdout
        assert "ERROR: off-host copy: BACKUP_REMOTE is set but rclone is not installed" in result.stderr

    def test_a_remote_that_is_not_crypt_is_refused_before_any_upload(self, tmp_path: Path) -> None:
        """A plain bucket remote would ship member data readable by the provider: fail closed."""
        backup_dir = tmp_path / "backups"
        env_file = _write_env(tmp_path, backup_dir=str(backup_dir), backup_remote="lem-offsite:")
        calls = _install_fake_rclone(tmp_path)
        result = _run(
            tmp_path,
            {
                "LEM_ENV_FILE": str(env_file),
                "BACKUP_CHROME_VOL": "0",
                "FAKE_RCLONE_LOG": str(calls),
                "FAKE_RCLONE_TYPE": "s3",
            },
            '"$BACKUP_SH"',
        )
        assert result.returncode != 0, result.stdout
        assert "lem-offsite is not a crypt remote" in result.stderr
        assert "refusing to upload plaintext member data" in result.stderr
        assert not calls.exists()
        assert not (backup_dir / ".offsite-last-ok").exists()
        assert len(list(backup_dir.glob("db-*.sql.gz"))) == 1

    def test_an_unknown_remote_name_is_refused(self, tmp_path: Path) -> None:
        backup_dir = tmp_path / "backups"
        env_file = _write_env(tmp_path, backup_dir=str(backup_dir), backup_remote="lem-store:bucket")
        calls = _install_fake_rclone(tmp_path)
        result = _run(
            tmp_path,
            {"LEM_ENV_FILE": str(env_file), "BACKUP_CHROME_VOL": "0", "FAKE_RCLONE_LOG": str(calls)},
            '"$BACKUP_SH"',
        )
        assert result.returncode != 0, result.stdout
        assert "lem-store is not a crypt remote (type: not found)" in result.stderr
        assert not calls.exists()

    def test_inline_connection_strings_and_flag_shaped_values_are_refused(self, tmp_path: Path) -> None:
        """An inline ":s3,…" remote carries keys; a "-…" value would be parsed as an rclone flag."""
        for remote in (":s3,access_key_id=AKIDEXAMPLE:bucket", "--config=/tmp/x:", "no-colon"):
            backup_dir = tmp_path / "backups"
            env_file = _write_env(tmp_path, backup_dir=str(backup_dir), backup_remote=remote)
            calls = _install_fake_rclone(tmp_path)
            result = _run(
                tmp_path,
                {"LEM_ENV_FILE": str(env_file), "BACKUP_CHROME_VOL": "0", "FAKE_RCLONE_LOG": str(calls)},
                '"$BACKUP_SH"',
            )
            assert result.returncode != 0, (remote, result.stdout)
            assert "must name a configured rclone crypt remote" in result.stderr, remote
            assert "AKIDEXAMPLE" not in result.stdout + result.stderr
            assert not calls.exists()

    def test_log_names_the_remote_but_not_the_full_destination(self, tmp_path: Path) -> None:
        backup_dir = tmp_path / "backups"
        env_file = _write_env(tmp_path, backup_dir=str(backup_dir), backup_remote="lem-offsite:db/")
        calls = _install_fake_rclone(tmp_path)
        result = _run(
            tmp_path,
            {"LEM_ENV_FILE": str(env_file), "BACKUP_CHROME_VOL": "0", "FAKE_RCLONE_LOG": str(calls)},
            '"$BACKUP_SH"',
        )
        assert result.returncode == 0, result.stderr + result.stdout
        assert "-> lem-offsite:" in result.stdout
        assert "lem-offsite:db/" not in result.stdout

