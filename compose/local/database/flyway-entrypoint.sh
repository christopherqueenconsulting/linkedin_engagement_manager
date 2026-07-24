#!/bin/bash

# Source the .env file to load environment variables
if [ -f .env ]; then
    export $(grep -v '^#' .env | xargs)
fi

# outOfOrder=true is REQUIRED, not optional, for our timestamp-versioned migrations
# (V<YYYYMMDDHHMMSS>__...). Timestamps guarantee unique versions, but PRs merge in an
# arbitrary order — a PR held open for review lands its OLDER-timestamped migration AFTER
# newer ones are already applied to prod. With the Flyway default (outOfOrder=false) that
# older migration is "resolved but not applied below the high-water mark", so `validate`
# fails and every deploy dies at this step. The migrations are independent additive DDL,
# so applying them out of order is safe; this makes deploys self-heal that situation.
FLYWAY_ARGS="-baselineOnMigrate=True -validateMigrationNaming=True -outOfOrder=True -url=jdbc:mysql://${MYSQL_HOST}/${MYSQL_DATABASE}?allowPublicKeyRetrieval=true -schemas=${MYSQL_DATABASE} -user=${MYSQL_USER} -password=${MYSQL_PASSWORD} -connectRetries=3"

flyway ${FLYWAY_ARGS} repair
flyway ${FLYWAY_ARGS} migrate