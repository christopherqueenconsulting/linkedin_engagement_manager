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

Flyway runs `outOfOrder=false` with validation on, so a duplicate/out-of-order migration *fails
the deploy* rather than silently skipping — timestamps prevent it entirely.
