-- Opportunistic "connection accepted" reading (issue #2658, P3). Set only when an existing visit
-- already sees the 1st-degree badge for a target whose invite we SENT; no new navigation reads it.
-- Additive: no ENUM change, existing rows stay accepted = 0 (unknown, not "declined").
ALTER TABLE connection_requests
    ADD COLUMN accepted TINYINT(1) NOT NULL DEFAULT 0,
    ADD COLUMN accepted_at DATETIME NULL;
