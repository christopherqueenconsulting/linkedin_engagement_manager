-- Roster targets LEM can't comment on, and the opt-in paced auto-follow (issue #962).
--
-- The roster (issue #616) opens each target's /recent-activity/all/ page directly, so no follow is
-- needed to SEE their posts. Authors who restrict commenting to connections/followers render no
-- comment affordance at all, and `comment_on_roster_posts` skips them fail-closed — invisibly. The
-- user could never learn that following or connecting would unlock the account.
--
-- comment_blocked_streak  consecutive VISITS that found posts but no comment affordance. Reset to 0
--                         by the same UPDATE that records a successful comment, so a target that
--                         becomes commentable clears itself without a separate job.
-- last_blocked_at         when that streak last grew — the badge's "as of" date.
-- follow_status           'unknown'      never examined
--                         'not_following' a Follow control was seen and not (yet) clicked
--                         'following'     verified following — TERMINAL, never re-examined
--                         'follow_failed' attempts exhausted — TERMINAL, badged for the user
-- follow_attempts         failed click attempts so far; at the code's max the status goes terminal,
--                         which is what stops the roster re-trying the same profile every run.
--
-- Every column is automation-owned: `upsert_engagement_targets` writes only the editable fields, so
-- an operator saving the roster in the SPA can never reset a streak or a follow state.
ALTER TABLE engagement_targets
    ADD COLUMN comment_blocked_streak TINYINT UNSIGNED NOT NULL DEFAULT 0,
    ADD COLUMN last_blocked_at DATETIME NULL,
    ADD COLUMN follow_status ENUM('unknown','not_following','following','follow_failed')
        NOT NULL DEFAULT 'unknown',
    ADD COLUMN followed_at DATETIME NULL,
    ADD COLUMN follow_attempts TINYINT UNSIGNED NOT NULL DEFAULT 0;

-- Opt-in auto-follow, OFF by default: following is a classic bot signature at volume, so this can
-- only ever be switched on deliberately. 0 = off is the honest default for a NOT NULL boolean here
-- (unlike the invite caps, where NULL means "never chosen"), because the feature not existing
-- yesterday and the user leaving it off must behave identically.
--
-- max_follows_per_day is NULL = "never chosen" -> the conservative code default (3/day). An
-- explicit 0 IS "off" and is preserved. The lane draws its own paced budget from this cap and is
-- still bounded by the shared account envelope, so it can never enlarge a day's outbound traffic.
ALTER TABLE engagement_preferences
    ADD COLUMN roster_auto_follow TINYINT(1) NOT NULL DEFAULT 0,
    ADD COLUMN max_follows_per_day INT NULL DEFAULT NULL AFTER roster_auto_follow;
