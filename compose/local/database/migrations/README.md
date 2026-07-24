# Database migrations (Flyway)

## Naming: NEW migrations use a TIMESTAMP version

```
V<YYYYMMDDHHMMSS>__short_description.sql
```
Get the version at authoring time (UTC):
```
date -u +%Y%m%d%H%M%S        # e.g. 20260723211500
# -> V20260723211500__add_widget_table.sql
```

### Why
Sequential integers (V57, V58, …) collide when two branches are open at once — each grabs the
same "next" number, both merge (different filenames don't git-conflict), and `flyway migrate`
then fails *"Found more than one migration with version N"*, blocking every deploy. Timestamps are
unique per authoring second and always sort **after** the legacy integer migrations.

### Rules
- **Never** use a bare integer version for a new migration.
- **Never** rename/renumber an already-merged migration — Flyway tracks applied ones by
  version + checksum, so renaming a shipped migration breaks validation everywhere it ran.
- Legacy `V1`–`V58` are frozen and grandfathered; everything new is a timestamp.
- Enforced by the **Migration Versions** CI check.

Flyway runs with `outOfOrder=true` (see `../flyway-entrypoint.sh`). Timestamps make versions
unique, but PRs merge in an arbitrary order — a PR held open for review lands its *older*
timestamp after newer migrations are already applied to prod. With `outOfOrder=false` that older
migration is "resolved but not applied below the high-water mark", which makes `validate` fail and
kills every deploy at the migration step. `outOfOrder=true` applies it in place instead. This is
safe because migrations are independent additive DDL — do **not** author a migration that depends
on a later-timestamped one having already run. Duplicate *versions* are still rejected (and caught
pre-merge by the **Migration Versions** check); only out-of-order *application* is allowed.
