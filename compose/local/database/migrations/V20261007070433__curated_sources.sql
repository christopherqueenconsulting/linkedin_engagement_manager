-- Curated outside sources, phase 1 (docs/curated-sources.md, research in
-- docs/curated-sources-research.md).
--
-- `curated_sources` holds ONE candidate a curated post could comment on: a LinkedIn post from the
-- feed/roster, an item from the owner-edited RSS allowlist, a government-data release, or a URL the
-- owner pasted. It is the provenance snapshot every curated draft is built from — who said it
-- (author/publisher), what they said (title/excerpt), where (url), and under what terms (licence).
--
-- canonical_id is the reshareable LinkedIn share/ugcPost URN. activity_urn is what the feed shows,
-- and LinkedIn refuses it as a reshare parent, so it is kept beside the canonical id rather than in
-- place of it. link_only marks a LinkedIn item whose share URN could not be read: it may be linked,
-- never reshared.
--
-- status is the source's own lifecycle; the POST it became carries its own approval (posts.status
-- and posts.approved_by). block_reason records why the guardrails refused a candidate (political,
-- paywall, licence_nc, excluded_platform, analyst_terms, no_provenance), so a blocked row is kept as
-- evidence the filter ran rather than silently dropped — and a re-collected URL is not re-screened
-- as new (the UNIQUE key below dedupes per user).
CREATE TABLE IF NOT EXISTS curated_sources (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NOT NULL,
    platform VARCHAR(20) NOT NULL,
    url VARCHAR(1024) NOT NULL,
    url_hash CHAR(64) NOT NULL,
    canonical_id VARCHAR(128) NULL,
    activity_urn VARCHAR(128) NULL,
    link_only TINYINT(1) NOT NULL DEFAULT 0,
    author VARCHAR(255) NULL,
    publisher VARCHAR(255) NULL,
    title VARCHAR(512) NULL,
    excerpt TEXT NULL,
    og_image_url VARCHAR(1024) NULL,
    licence VARCHAR(32) NOT NULL DEFAULT 'unknown',
    paywalled TINYINT(1) NOT NULL DEFAULT 0,
    published_at DATETIME NULL,
    fetched_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    status ENUM('new', 'drafted', 'approved', 'published', 'rejected', 'blocked') NOT NULL DEFAULT 'new',
    block_reason VARCHAR(255) NULL,
    post_id INT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_curated_sources_user_url (user_id, url_hash),
    KEY idx_curated_sources_user_status (user_id, status),
    KEY idx_curated_sources_post (post_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- The post a curated source became. NULL on every existing post, which is what "not curated" means
-- everywhere: the ceiling counts non-NULL rows, and the publish path routes non-NULL rows through
-- the curated publisher and its owner-approval check.
ALTER TABLE posts ADD COLUMN curated_source_id INT NULL;

-- How the curated post presents its source. No screenshot member, by owner decision.
ALTER TABLE posts ADD COLUMN source_treatment ENUM('reshare', 'rechart', 'link') NULL;

ALTER TABLE posts ADD INDEX idx_posts_curated_source (curated_source_id);
