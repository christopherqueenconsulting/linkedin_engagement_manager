-- Add 'error' to the posts.status ENUM (issue #1567).
--
-- PostStatus.ERROR has existed in Python since carousel/video generation gained a terminal failure
-- state, but the column's ENUM was never widened past V6, so every write of it raised
-- "1265 (01000): Data truncated for column 'status' at row 1" and the failed post silently kept
-- whatever status it already had. Restates every existing value plus the new one, as MySQL requires.
ALTER TABLE posts
MODIFY COLUMN status ENUM('planning','pending','approved','rejected','scheduled','posted','error') NOT NULL DEFAULT 'pending';
