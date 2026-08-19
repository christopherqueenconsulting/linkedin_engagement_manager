-- Issue #1603 (Phase 2 of #1450) — per-user disable.
--
-- Additive only: NULL means "enabled" (the state every existing row already has), so no backfill is
-- needed. `get_active_user_ids()` reads this column directly — a disabled user is absent from every
-- automation gate that function feeds, not just from a badge nobody checks.
ALTER TABLE users
    ADD COLUMN disabled_at TIMESTAMP NULL;
