-- Automated activation checklist + stalled-user nudges (issue #500). "Aha" = the user's first AI post
-- is published AND their first automated comment/DM has gone out under their own voice, within 7 days.
--
-- Step completion is DERIVED from data we already store (session cookie, engagement prefs, posts,
-- logs), but the FIRST time each step flips is persisted here so (a) the SPA checklist is stable,
-- (b) the PostHog step event is emitted exactly once, and (c) the nudge clock has a start.
CREATE TABLE IF NOT EXISTS onboarding_state (
    user_id                 INT NOT NULL PRIMARY KEY,
    started_at              DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,  -- trial start when known
    linkedin_connected_at   DATETIME NULL,
    voice_set_at            DATETIME NULL,
    first_post_approved_at  DATETIME NULL,
    caps_enabled_at         DATETIME NULL,
    activated_at            DATETIME NULL,
    updated_at              DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    KEY idx_onboarding_activated (activated_at),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

-- One row per nudge actually sent, so each nudge fires AT MOST ONCE per user (a stalled user gets the
-- next-best nudge tomorrow instead of the same one again) and the daily cooldown reads MAX(sent_at).
CREATE TABLE IF NOT EXISTS onboarding_nudges (
    user_id     INT NOT NULL,
    nudge_key   VARCHAR(32) NOT NULL,
    sent_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, nudge_key),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
