-- Profile-view-to-call attribution (issue #2668, Phase 3 of #2632).
-- A booked call's stage event records the FIRST and LAST LEM touch found by walking back 30 days
-- through the lead. first_touch / last_touch read '<kind>@<UTC ISO time>' (e.g. 'profile_view@2026-11-01T09:00:00').
-- attribution is 'attributed' when a touch was found and 'unattributed' when none was: a call with no
-- recorded touch is never guessed. NULL on every other event, and on a booking not yet evaluated.
ALTER TABLE lead_stage_events
    ADD COLUMN first_touch VARCHAR(64) NULL,
    ADD COLUMN last_touch  VARCHAR(64) NULL,
    ADD COLUMN attribution ENUM('attributed','unattributed') NULL;
