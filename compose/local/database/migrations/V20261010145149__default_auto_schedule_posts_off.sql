-- Issue #2366: new accounts start with approval-before-publish.
--
-- `users.auto_schedule_posts` was created with DEFAULT 1 (V22), so every account created without
-- an explicit value had its generated posts approved and scheduled with no review. This changes the
-- column DEFAULT only. `ALTER COLUMN ... SET DEFAULT` is a metadata change: it rewrites no row, so
-- every existing account keeps the value it has today. Type and NOT NULL are unchanged.
ALTER TABLE users
    ALTER COLUMN auto_schedule_posts SET DEFAULT 0;
