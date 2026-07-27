-- Affiliate / ambassador program (issue #737). Three tables because the three things they hold have
-- different lifetimes and different blast radius:
--
--   affiliate_enrollments -> the per-user STATE, and the reason this is a table rather than two
--                            columns on `users`: it carries TWO independent toggles that must never
--                            collapse into one. `status` is (A) affiliate STATUS — enrolled by
--                            default, one-click opt-out. `promo_content_opt_in` is (B) whether LEM
--                            may publish promotional content about LEM from the user's OWN LinkedIn
--                            account, which is their professional identity: it defaults to 0 and its
--                            consent timestamp/version is the record that they said yes.
--   affiliate_referrals   -> who arrived from whose link. UNIQUE on referred_user_id, so a person
--                            can only ever be attributed to ONE referrer and a replayed signup can
--                            never mint a second pending referral. Self-referral is stored REJECTED
--                            rather than dropped: a fraud signal we can count is worth more than a
--                            row we never wrote.
--   affiliate_rewards     -> the append-only ledger of trial days granted (and revoked, as a
--                            negative delta), mirroring the credit-ledger pattern already used by
--                            avatar_credit_ledger / video_credit_ledger. The user's total granted
--                            days is SUM(trial_days), which is what the per-user cap is enforced on.
CREATE TABLE IF NOT EXISTS affiliate_enrollments (
    user_id               INT NOT NULL PRIMARY KEY,
    status                ENUM('enrolled','opted_out') NOT NULL DEFAULT 'enrolled',
    referral_code         VARCHAR(64) NOT NULL,
    enrolled_at           DATETIME NULL,
    opted_out_at          DATETIME NULL,
    notice_seen_at        DATETIME NULL,
    promo_content_opt_in  TINYINT(1) NOT NULL DEFAULT 0,
    promo_consent_at      DATETIME NULL,
    promo_consent_version VARCHAR(32) NULL,
    created_at            DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at            DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_affiliate_referral_code (referral_code),
    INDEX idx_affiliate_status (status)
);

CREATE TABLE IF NOT EXISTS affiliate_referrals (
    id                INT AUTO_INCREMENT PRIMARY KEY,
    referrer_user_id  INT NOT NULL,
    referred_user_id  INT NOT NULL,
    referral_code     VARCHAR(64) NOT NULL,
    status            ENUM('pending','converted','rejected') NOT NULL DEFAULT 'pending',
    reject_reason     VARCHAR(64) NULL,
    created_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    converted_at      DATETIME NULL,
    UNIQUE KEY uq_affiliate_referred (referred_user_id),
    INDEX idx_affiliate_referrer (referrer_user_id, status)
);

CREATE TABLE IF NOT EXISTS affiliate_rewards (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    user_id       INT NOT NULL,
    referral_id   INT NULL,
    kind          ENUM('enrollment','referral','revoked') NOT NULL,
    trial_days    INT NOT NULL,
    reason        VARCHAR(64) NULL,
    trial_ends_at DATETIME NULL,
    granted_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    -- One reward per converted referral: a retried conversion hits this and rolls back rather than
    -- paying twice. MySQL allows repeated NULLs in a UNIQUE index, so this constrains referral
    -- rewards ONLY — the "one enrollment bonus per user" invariant is held transactionally in
    -- db.grant_affiliate_enrollment_bonus (SELECT … FOR UPDATE inside the granting transaction),
    -- because the enrollment bonus can legitimately be granted again after a revoke.
    UNIQUE KEY uq_affiliate_reward_referral (referral_id),
    INDEX idx_affiliate_reward_user (user_id, kind)
);
