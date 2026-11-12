"""perf_snapshot.sh writes null, never 0, for an engagement count it could not read.

metrics.jsonl has all-zero d1 and cumulative rows from 2026-10-08 through 2026-10-15: the `:-0`
defaults turned empty reads into rows that read as real measurements ("unscored is never zero").
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "perf_snapshot.sh"

_COUNT_KEYS = {
    "d1": ("comments", "replies", "dms", "reactions", "posts"),
    "cumulative": (
        "posts_tracked", "own_reactions", "own_comments", "own_impressions",
        "engagers", "commented_posts", "followups",
    ),
}


def _run(tmp_path: Path, sudo_body: str) -> tuple[dict, str]:
    """Run the snapshot with a stub `sudo` on PATH; return the JSON line and snapshot.log."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    sudo = bin_dir / "sudo"
    sudo.write_text(f"#!/usr/bin/env bash\n{sudo_body}\n", encoding="utf-8")
    sudo.chmod(0o755)
    perf = tmp_path / "perf"
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "PERF_DIR": str(perf),
        "LEM_ENV_FILE": str(tmp_path / "missing.env"),
        "LEM_ROOT": str(tmp_path),
        "HOME": os.environ.get("HOME", str(tmp_path)),
    }
    subprocess.run(["bash", str(_SCRIPT)], env=env, check=True, capture_output=True, text=True)
    lines = (perf / "metrics.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    return json.loads(lines[0]), (perf / "snapshot.log").read_text(encoding="utf-8")


class TestUnreadableCountsAreNull:
    def test_unreadable_credentials_write_null_counts_and_say_why(self, tmp_path):
        row, log = _run(tmp_path, "exit 1")

        for block, keys in _COUNT_KEYS.items():
            for key in keys:
                assert row[block][key] is None, f"{block}.{key}"
        assert "DB credentials not readable" in log
        assert "snapshot written" in log

    def test_empty_query_results_write_null_counts_and_say_why(self, tmp_path):
        # Credentials read fine, but every docker exec (queries and margin) returns nothing.
        body = (
            'if [ "$2" = grep ]; then case "$4" in '
            '*USER*) echo MYSQL_USER=u;; *PASSWORD*) echo MYSQL_PASSWORD=p;; *DATABASE*) echo MYSQL_DATABASE=d;; '
            "esac; exit 0; fi\nexit 1"
        )
        row, log = _run(tmp_path, body)

        assert row["cumulative"]["posts_tracked"] is None
        assert row["d1"]["comments"] is None
        assert "null fields: d1.comments,d1.replies,d1.dms,d1.reactions,d1.posts,cumulative.posts_tracked" in log

    def test_real_counts_pass_through_as_integers(self, tmp_path):
        body = (
            'if [ "$2" = grep ]; then case "$4" in '
            '*USER*) echo MYSQL_USER=u;; *PASSWORD*) echo MYSQL_PASSWORD=p;; *DATABASE*) echo MYSQL_DATABASE=d;; '
            "esac; exit 0; fi\n"
            'case "$*" in *CONCAT_WS*) echo 61,10,4,900;; *mysql*) echo 7;; *) exit 1;; esac'
        )
        row, log = _run(tmp_path, body)

        assert row["cumulative"]["posts_tracked"] == 61
        assert row["cumulative"]["own_impressions"] == 900
        assert row["cumulative"]["commented_posts"] == 7
        assert row["d1"]["comments"] == 7
        assert "unavailable" not in log.replace("margin block unavailable", "")

    def test_a_partial_failure_names_only_the_null_fields(self, tmp_path):
        # post_stats answers; every plain COUNT(*) query returns nothing.
        body = (
            'if [ "$2" = grep ]; then case "$4" in '
            '*USER*) echo MYSQL_USER=u;; *PASSWORD*) echo MYSQL_PASSWORD=p;; *DATABASE*) echo MYSQL_DATABASE=d;; '
            "esac; exit 0; fi\n"
            'case "$*" in *CONCAT_WS*) echo 61,10,4,900;; *) exit 1;; esac'
        )
        row, log = _run(tmp_path, body)

        assert row["cumulative"]["posts_tracked"] == 61
        assert row["d1"]["comments"] is None
        assert (
            "null fields: d1.comments,d1.replies,d1.dms,d1.reactions,d1.posts,"
            "cumulative.engagers,cumulative.commented_posts,cumulative.followups"
        ) in log
        assert "posts_tracked" not in log

    def test_a_password_containing_equals_is_kept_whole(self, tmp_path):
        body = (
            'if [ "$2" = grep ]; then case "$4" in '
            '*USER*) echo MYSQL_USER=u;; *PASSWORD*) echo MYSQL_PASSWORD=a=b;; *DATABASE*) echo MYSQL_DATABASE=d;; '
            "esac; exit 0; fi\n"
            'case "$*" in *--env-file*) f="${5}"; grep -qx "MYSQL_PWD=a=b" "$f" && echo 7 || exit 1;; *) exit 1;; esac'
        )
        row, _ = _run(tmp_path, body)

        assert row["cumulative"]["engagers"] == 7


class TestTheCauseIsNamed:
    """#2431: every count went null from 2026-10-20 with no cause in snapshot.log."""

    def test_the_first_query_error_is_logged_with_the_container(self, tmp_path):
        body = (
            'if [ "$2" = grep ]; then case "$4" in '
            '*USER*) echo MYSQL_USER=u;; *PASSWORD*) echo MYSQL_PASSWORD=p;; *DATABASE*) echo MYSQL_DATABASE=d;; '
            "esac; exit 0; fi\n"
            'case "$*" in *mysql*) echo "mysql: [Warning] Using a password on the command line" >&2; '
            "echo \"ERROR 1045 (28000): Access denied for user 'u'@'localhost'\" >&2; exit 1;; esac\nexit 1"
        )
        row, log = _run(tmp_path, body)

        assert row["cumulative"]["posts_tracked"] is None
        assert "container mysql_db; first error: ERROR 1045 (28000): Access denied" in log
        assert "Using a password" not in log

    def test_no_stderr_says_none_printed(self, tmp_path):
        body = (
            'if [ "$2" = grep ]; then case "$4" in '
            '*USER*) echo MYSQL_USER=u;; *PASSWORD*) echo MYSQL_PASSWORD=p;; *DATABASE*) echo MYSQL_DATABASE=d;; '
            "esac; exit 0; fi\nexit 1"
        )
        _, log = _run(tmp_path, body)

        assert "first error: none printed" in log


class TestEnvValuesAreReadLikeCompose:
    def test_quoted_duplicated_values_and_mysql_host_reach_the_client_clean(self, tmp_path):
        # Quotes, a trailing comment and a duplicate (last wins) — all of which compose reads fine.
        body = (
            'if [ "$2" = grep ]; then case "$4" in '
            "*USER*) printf 'MYSQL_USER=old\\nMYSQL_USER=\"u\"\\n';; "
            "*PASSWORD*) echo \"MYSQL_PASSWORD='p w'  # rotated\";; "
            "*DATABASE*) echo 'MYSQL_DATABASE=d # prod';; *HOST*) echo MYSQL_HOST=lem_mysql;; "
            "esac; exit 0; fi\n"
            'case "$*" in *"--env-file "*" lem_mysql mysql -uu -N"*" d.post_engagers "*) '
            'grep -qx "MYSQL_PWD=p w" "$5" && echo 7 || exit 1;; *) exit 1;; esac'
        )
        row, _ = _run(tmp_path, body)

        assert row["cumulative"]["engagers"] == 7

    def test_an_unsafe_mysql_host_falls_back_to_mysql_db(self, tmp_path):
        body = (
            'if [ "$2" = grep ]; then case "$4" in '
            '*USER*) echo MYSQL_USER=u;; *PASSWORD*) echo MYSQL_PASSWORD=p;; *DATABASE*) echo MYSQL_DATABASE=d;; '
            "*HOST*) echo 'MYSQL_HOST=--privileged';; esac; exit 0; fi\n"
            'case "$*" in *" mysql_db mysql "*) echo 7;; *) exit 1;; esac'
        )
        row, log = _run(tmp_path, body)

        assert row["cumulative"]["engagers"] == 7
        assert "--privileged" not in log
