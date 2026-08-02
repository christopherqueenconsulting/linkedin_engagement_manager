-- Avatar surface for newsletter covers (image-generation overhaul).
-- Same contract as the other avatar_use_* flags (issue #744): default OFF, the /avatars page
-- is the only writer, and guardrails.resolve_avatar_for is the only reader.
ALTER TABLE users
    ADD COLUMN avatar_use_newsletter TINYINT(1) NOT NULL DEFAULT 0;
