#!/usr/bin/env bash
# Build the integration lane's MySQL schema: the same one the stack runs.
#
# Lives in a script rather than inline in integration-coverage.yml because the workflow starts it in
# the BACKGROUND (so it overlaps `poetry install`), and wrapping this much quoting — a heredoc-less
# `mysql <` redirect, and a GRANT carrying both single quotes and backticks — inside a `bash -c '…'`
# is how you get a silently truncated command.
#
# Expects the MySQL service to be reachable on 127.0.0.1:3306 as root/test_root. GitHub has already
# waited on the service healthcheck before any step runs, so there is no readiness loop here.
set -euo pipefail

REPO_ROOT="${GITHUB_WORKSPACE:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

# Base tables, as docker-entrypoint-initdb.d applies them in compose.
docker run --rm --network host \
  -v "$REPO_ROOT/compose/local/database:/sql:ro" mysql:8 \
  sh -c 'mysql -h 127.0.0.1 -P 3306 -uroot -ptest_root < /sql/setup.sql'

# Then every Flyway migration on top.
docker run --rm --network host \
  -v "$REPO_ROOT/compose/local/database/migrations:/flyway/sql:ro" \
  redgate/flyway:latest-alpine \
  -url="jdbc:mysql://127.0.0.1:3306/linkedin_manager?allowPublicKeyRetrieval=true" \
  -schemas=linkedin_manager -user=root -password=test_root \
  -connectRetries=10 -outOfOrder=true -baselineOnMigrate=true migrate

# A no-op migrate exits 0, which would leave the tests silently skipping on an empty schema —
# assert the newest table is really there before running them.
docker run --rm --network host mysql:8 \
  mysql -h 127.0.0.1 -P 3306 -uroot -ptest_root linkedin_manager \
  -e 'SELECT 1 FROM comment_outcomes LIMIT 1'

# Each xdist worker clones the schema above into a database of its own
# (tests/integration/conftest.py). The service container grants MYSQL_USER rights on MYSQL_DATABASE
# and nothing else, so widen that to the sibling names the workers build.
# `\_` is a literal underscore: unescaped, `_` is a single-character wildcard in a GRANT.
docker run --rm --network host mysql:8 \
  mysql -h 127.0.0.1 -P 3306 -uroot -ptest_root \
  -e "GRANT ALL PRIVILEGES ON \`linkedin\_manager\_%\`.* TO 'test_user'@'%'; FLUSH PRIVILEGES;"
