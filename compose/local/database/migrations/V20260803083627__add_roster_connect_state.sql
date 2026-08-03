-- Connect escalation for roster targets following did not unlock (issue #979, Part 4 of #962).
--
-- The ladder per target is: blocked -> follow (#962 Part 3) -> STILL blocked -> needs connection ->
-- (opt-in) auto-connect. A target that was never followed is never escalated: "following didn't
-- work" is a claim we have to have evidence for, and the evidence is a blocked visit recorded AFTER
-- followed_at.
--
-- connect_status        'unknown'          nothing known / nothing to do — the resting state
--                       'needs_connection' followed, and STILL un-commentable on a later visit
--                       'requested'        an invite went out (or a Pending control was read)
--                       'connected'        1st-degree — the ladder is done
--                       'failed'           the invite could not be sent; NEVER auto-retried
-- connect_requested_at  when that invite went out (or Pending was first seen). Stamped once.
--
-- 'requested' and 'failed' are BOTH terminal for automation — one shot per target. LinkedIn's own
-- withdraw/expire cycle governs the request from there and the user decides manually; a second
-- automatic invite to someone who declined the first is exactly the pattern that gets accounts
-- restricted.
ALTER TABLE engagement_targets
    ADD COLUMN connect_status
        ENUM('unknown','needs_connection','requested','connected','failed')
        NOT NULL DEFAULT 'unknown',
    ADD COLUMN connect_requested_at DATETIME NULL;

-- Opt-in auto-connect, OFF by default and INDEPENDENT of roster_auto_follow: an invite is a
-- heavier, less reversible act than a follow, and it spends the account's ONE combined invite
-- budget (max_invites_per_day), which the profile-viewer and proactive flows already share. 0 = off
-- is the honest default for a NOT NULL boolean — the feature not existing yesterday and the user
-- leaving it off must behave identically.
--
-- No cap column of its own on purpose: roster invites take at most a minority share of whatever the
-- day's invite budget has left, so the ladder can never starve #398's lanes.
ALTER TABLE engagement_preferences
    ADD COLUMN roster_auto_connect TINYINT(1) NOT NULL DEFAULT 0;
