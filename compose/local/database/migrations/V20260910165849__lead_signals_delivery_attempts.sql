-- Issue #2028: an approved hot-lead response that did not land was terminal and silent.
--
-- `_send_lead_response` wrote `status='failed'` and stopped — no retry, no fallback channel, no
-- surface saying it never arrived. Three of the five lead signals this account has ever produced
-- ended there, and every one of them was a real person who had written a substantive reply to us.
-- That is the single highest-value event the system can observe, failing into a dead row.
--
-- The commonest cause was mechanical and is fixed separately (#2020): the reply path returns False
-- when it cannot find the person's comment on the page, and the comment readers were blind. So the
-- first failure is now retried rather than believed. This counter is what keeps that bounded — a
-- second failure is terminal, and warns loudly enough for a human to answer by hand.
--
-- Counts DELIVERY attempts, not approvals. Existing rows start at 0, which is correct: none of them
-- has been retried under the new path.
ALTER TABLE lead_signals
    ADD COLUMN delivery_attempts INT NOT NULL DEFAULT 0 AFTER status;
