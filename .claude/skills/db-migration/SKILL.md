---
name: db-migration
description: Use when adding, altering, or dropping anything in the MySQL schema — any new file in compose/local/database/migrations/, or when a new enum/status value is needed. Covers Flyway timestamp naming, additive-only rules, and the risk:migration label.
---

# Adding a database migration

1. Version at authoring time (UTC): `date -u +%Y%m%d%H%M%S` → `V<stamp>__short_description.sql` in `compose/local/database/migrations/`. **Never** a bare integer; **never** rename/renumber a merged migration (Flyway tracks version + checksum).
2. Additive, independent DDL only. Flyway runs `outOfOrder=true`, so PRs apply in arbitrary order — a migration must NOT depend on a later-timestamped one having already run.
3. MySQL ENUM columns (`logs.action_type`, `posts.status`, …): adding a value = `ALTER TABLE … MODIFY COLUMN` restating **every** existing value plus the new one. Then use the enum in code via `PostStatus` / `PostType` / `LogActionType` from `db.py` — never raw strings.
4. All runtime access goes through functions in `src/cqc_lem/utilities/db.py` — no raw SQL anywhere else. Add the accessor there.
5. Label the PR's issue `risk:migration` — the agent still builds, but a human signs off before merge (park with `needs-human` + a Decision Comment).
6. The **Migration Versions** CI check enforces unique timestamp versions pre-merge.

Scheduling/time columns: store naive UTC via `to_naive_utc()` — see `docs/timezone-contract.md`.
Full rationale (why timestamps, why outOfOrder): `compose/local/database/migrations/README.md`.
