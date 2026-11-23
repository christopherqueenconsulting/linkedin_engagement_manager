-- DM follow-ups: an end state for a thread that never becomes readable (issue #2622).
--
-- A follow-up row always has history (it is enqueued only after a sent DM or a reply), yet
-- `open_message_thread` used to accept a bare overlay composer as a real, empty thread and stop
-- before the routes that show the messages. The row then re-read forever with no end state.
--
-- `thread_unreadable_reads` counts ONLY per-thread unreadable reads (a composer rendered, no
-- history) — never a missing self-name, an exception or a route that rendered nothing, which can be
-- an egress/429 fault rather than one bad thread. The terminal stop is one-way, so it counts a
-- narrower thing than `unreadable_reads` (which keeps driving the read backoff).
-- `stop_reason` records why a row was stopped; NULL on every row that was not stopped by a machine
-- verdict, and it stays unused if the rollback flag DM_THREAD_EXPECT_HISTORY is turned off.
ALTER TABLE dm_followups
    ADD COLUMN stop_reason VARCHAR(64) NULL,
    ADD COLUMN thread_unreadable_reads INT NOT NULL DEFAULT 0;

-- The counts accumulated against the old, wrong ladder describe the bug, not the threads: restart
-- every still-pending streak so the corrected ladder gets a fair read. Generic on purpose.
UPDATE dm_followups SET unreadable_reads = 0 WHERE status = 'pending';
