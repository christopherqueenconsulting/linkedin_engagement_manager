-- Dwell-time optimization (issue #391 — C2). Dwell (how long a reader holds on a post) is the
-- dominant 2026 ranking signal, and nothing we already store proxies it. content_framework's
-- dwell_report() scores every generated post 0-100 deterministically (hook placement vs the
-- '...more' fold, read-time target, scannability, re-readable structure) and it is persisted here
-- alongside authenticity_score (V57) so post quality can be tracked on both axes.
-- Advisory only: unlike the authenticity gate this never demotes a post's status.
-- NULL = not yet scored (pre-existing rows).
ALTER TABLE posts ADD COLUMN dwell_score INT NULL;
