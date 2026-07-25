-- Rolling forward buffer of ready posts (issue #544). Content generation used to fill one
-- calendar week at a time, so mid-week the current week's planning slots were all in the past
-- and nothing was generated until Saturday's next-week run — the ready queue could drop to ~1
-- post. Generation is now a bounded rolling top-up: keep up to content_buffer_max_posts ready
-- posts scheduled within the next content_buffer_days days, and never generate beyond that.
-- Both knobs are per-user so the ceiling on forward LLM/video spend stays explicit.
ALTER TABLE users
    ADD COLUMN content_buffer_days TINYINT UNSIGNED NOT NULL DEFAULT 5 AFTER auto_schedule_posts,
    ADD COLUMN content_buffer_max_posts TINYINT UNSIGNED NOT NULL DEFAULT 5 AFTER content_buffer_days;
