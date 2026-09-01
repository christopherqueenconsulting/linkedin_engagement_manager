-- DM follow-ups: bound the re-read rate of a permanently-unreadable thread (issue #1815).
--
-- `send-due-dm-followups` runs every 30 minutes and dispatches `process_user_followups` for every
-- user with a due row. A thread `check_dm_replied` can never read (a rotated selector, a dead
-- profile) stayed `status='pending'` with `due_at` untouched, so it was due again on every single
-- beat forever — ~2.7 hours a day of a shared `se_outreach` Chrome slot spent re-opening one thread
-- that never becomes readable, every run reporting SUCCESS.
--
-- `unreadable_reads` counts consecutive UNKNOWN reads (issue #731's fail-closed "could not tell,
-- so skip" verdict). `reset_unreadable_reads` clears it on any state it CAN read, and once it
-- crosses a small ceiling backs the row off by a growing interval instead of re-reading every run —
-- the row stays 'pending' throughout, so a thread that later becomes readable is never lost the way
-- moving it to 'failed' would lose it (`get_due_followups` only ever selects 'pending').
ALTER TABLE dm_followups
    ADD COLUMN unreadable_reads INT NOT NULL DEFAULT 0;
