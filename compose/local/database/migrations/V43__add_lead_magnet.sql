-- Comment→DM lead magnet: when someone comments a trigger keyword on the user's post, auto-DM
-- them a resource. The compliant way to distribute links (links in the post body / first comment
-- are penalized). Opt-in. lead_magnet_sent dedupes so a person is DM'd at most once per post.
CREATE TABLE IF NOT EXISTS lead_magnet_settings (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    user_id     INT NOT NULL UNIQUE,
    enabled     TINYINT(1) NOT NULL DEFAULT 0,
    keyword     VARCHAR(128) NULL,
    message     TEXT NULL,
    created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS lead_magnet_sent (
    id                 INT AUTO_INCREMENT PRIMARY KEY,
    user_id            INT NOT NULL,
    recipient_profile  VARCHAR(512) NOT NULL,
    post_id            INT NULL,
    sent_at            DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_user_recipient (user_id, recipient_profile),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
