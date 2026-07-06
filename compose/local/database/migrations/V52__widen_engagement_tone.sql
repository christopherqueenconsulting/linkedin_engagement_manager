-- Voice & Tone (and every other engagement setting) silently failed to persist: the whole
-- engagement_preferences upsert is one row, so an over-long `tone` raised MySQL 1406
-- ("Data too long for column 'tone'") and rolled back ALL sections at once. tone was
-- VARCHAR(64) — realistic tone descriptors ("direct, warm, credible, plainspoken - a
-- practitioner, not a pitch" = 65 chars) overflow it. Widen to VARCHAR(255), matching
-- comment_style, so a normal tone phrase can never break the save.
ALTER TABLE engagement_preferences
    MODIFY COLUMN tone VARCHAR(255) NULL;
