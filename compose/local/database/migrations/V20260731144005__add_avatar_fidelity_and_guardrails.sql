-- Avatar likeness fidelity, preview/approval gate and usage guardrails
-- (issue #744 — Phase 2 of #548, decisions 3A + 4A).
--
-- 3A: the user SELF-DECLARES gender presentation and an optional age band. Nothing infers them,
-- no model ever classifies the user's face, and NULL means "not declared" — which renders an
-- empty subject clause rather than a guess.
--
-- 4A: an avatar is unusable until its sample renders have been seen and APPROVED, use is opt-in
-- per content surface (default OFF), and `avatar_disabled` is the explicit "don't use my avatar"
-- switch that forces the base-Flux / Pexels path.

ALTER TABLE avatar_trainings
    ADD COLUMN gender_presentation     VARCHAR(32) NULL,
    ADD COLUMN age_band                VARCHAR(16) NULL,
    ADD COLUMN attributes_confirmed_at DATETIME    NULL,
    ADD COLUMN approval_status         ENUM('pending','approved','rejected') NOT NULL DEFAULT 'pending',
    ADD COLUMN approved_at             DATETIME    NULL,
    ADD COLUMN sample_paths            TEXT        NULL,
    ADD COLUMN samples_generated_at    DATETIME    NULL,
    ADD COLUMN sample_regen_count      INT         NOT NULL DEFAULT 0;

-- Per-user guardrails. Every opt-in defaults to 0: an avatar that has never been previewed and
-- approved must never reach a published post (that is exactly how the wrong-gender renders in
-- #548 shipped), so existing rows deliberately land in the OFF state too.
ALTER TABLE users
    ADD COLUMN avatar_disabled       TINYINT(1) NOT NULL DEFAULT 0,
    ADD COLUMN avatar_use_post_image TINYINT(1) NOT NULL DEFAULT 0,
    ADD COLUMN avatar_use_carousel   TINYINT(1) NOT NULL DEFAULT 0,
    ADD COLUMN avatar_use_video      TINYINT(1) NOT NULL DEFAULT 0;

-- use_avatar is the compose-time toggle (PostRequest.use_avatar), which until now was accepted by
-- the API and dropped. NULL = follow the per-user opt-ins; 0/1 = this post overrides them.
-- avatar_media records that generated media for this post actually came out of the avatar LoRA,
-- so the caption disclosure can be applied to avatar images and not just to video.
ALTER TABLE posts
    ADD COLUMN use_avatar   TINYINT(1) NULL,
    ADD COLUMN avatar_media TINYINT(1) NOT NULL DEFAULT 0;
