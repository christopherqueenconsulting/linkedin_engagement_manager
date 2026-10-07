-- Seed the owner's (user 1) brand kit. Idempotent and non-destructive: touches only a row that
-- exists AND has no kit yet, so a kit the user has saved is never overwritten, and a database with
-- no user 1 (fresh install, integration lane) is a 0-row no-op.
--
-- Needs the brand_kit column from V20261006201704 — an EARLIER version shipped in the same PR, so
-- Flyway always applies it first. A data UPDATE, so the release classifier HOLDS it for sign-off.
UPDATE engagement_preferences
SET brand_kit = JSON_OBJECT(
        'primary_hex', '#e9d437',
        'secondary_hex', '#a89816',
        'neutral_dark_hex', '#1f1f1f',
        'neutral_light_hex', '#f7f5ef',
        'font_vibe', 'clean geometric sans, bold headlines',
        'visual_mood', 'practitioner field notes: real work, warm light, confident and specific, no hype',
        'avoid', JSON_ARRAY('pipes', 'gears', 'lightbulbs', 'puzzle pieces', 'robots', 'glowing brains')
    )
WHERE user_id = 1
  AND brand_kit IS NULL;
