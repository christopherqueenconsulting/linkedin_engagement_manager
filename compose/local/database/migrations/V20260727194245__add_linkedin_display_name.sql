-- The user's own name EXACTLY as LinkedIn renders it on their messages (issue #731).
--
-- Reply detection asks one question: "is the last sender in this thread us?" — so it needs the name
-- LinkedIn puts on OUR message group. Until now that came only from the scraped profile, and when
-- the scrape was stale/unavailable the answer was a guess: pre-#731 a missing self-name read as
-- "they replied", and post-#731 it is UNKNOWN, which correctly stops the follow-up but also stops
-- the sequence entirely. Storing it makes the comparison a user-declared fact instead of a scrape.
--
-- One field, not first/last: the message-group label is the full display name as one string, and
-- splitting it here would only invite a rejoin that doesn't match (suffixes, middle names, "Dr.").
-- Kept on users (identity — same reasoning as V55's reply_inbound_token), NOT on the one-row
-- engagement_preferences upsert, so a prefs save can never clobber it.
ALTER TABLE users
    ADD COLUMN linkedin_display_name VARCHAR(255) NULL;

-- Backfill from the profile we already scraped for each user, so existing accounts start with the
-- right value instead of an empty required field. Only fills blanks, and only from a name that is
-- actually there — a NULL stays NULL and the user is asked for it in Settings.
UPDATE users u
    JOIN profiles p ON p.user_id = u.id
SET u.linkedin_display_name = TRIM(JSON_UNQUOTE(JSON_EXTRACT(p.data, '$.full_name')))
WHERE u.linkedin_display_name IS NULL
  AND JSON_UNQUOTE(JSON_EXTRACT(p.data, '$.full_name')) IS NOT NULL
  AND TRIM(JSON_UNQUOTE(JSON_EXTRACT(p.data, '$.full_name'))) <> '';
