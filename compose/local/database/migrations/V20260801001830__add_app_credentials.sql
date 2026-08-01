-- App-level (not per-user) credential store (issue #742). The YouTube OAuth refresh token used to
-- live ONLY in /opt/lem/.env, so re-minting it meant editing the box and recreating containers —
-- and the weekly probe had nowhere to persist a token Google rotated underneath us.
--
-- One row per named credential. `value` is NULL-able so a row can record "this credential is
-- managed here" while still falling back to its env seed. Access posture matches the LinkedIn
-- tokens already in `users`: DB-access-controlled, never returned by any API (the status endpoint
-- reports state only, never the secret).
CREATE TABLE IF NOT EXISTS app_credentials (
    name        VARCHAR(100) NOT NULL,
    value       TEXT NULL,
    note        VARCHAR(255) NULL,
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
