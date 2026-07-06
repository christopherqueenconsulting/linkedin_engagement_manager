-- User-declared subject steering for generated posts AND feed comments. Without these, the generator
-- falls back to the user's full profile/recent activity for subject matter — which, for users whose
-- recent activity is dominated by a project they're building, makes that project the SUBJECT of
-- otherwise unrelated comments (LEM-drift). `focus_topics` (JSON array of subjects the user wants to
-- be known for) plus freeform `business_goals` / `personal_goals` give the model an explicit angle to
-- steer toward while staying grounded in the target post / chosen industry.
ALTER TABLE engagement_preferences
    ADD COLUMN focus_topics JSON NULL AFTER exclude_authors,
    ADD COLUMN business_goals TEXT NULL AFTER focus_topics,
    ADD COLUMN personal_goals TEXT NULL AFTER business_goals;
