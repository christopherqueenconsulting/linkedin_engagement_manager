-- Public, front-page FAQ (issue #506). The last consumer of the feedback->auto-work loop: questions
-- the classifier routes to FAQ (`FeedbackRoute.FAQ`, no code change needed) are clustered and the
-- auto-FAQ pass keeps the published answers here current, so the landing page answers what people
-- actually ask instead of hardcoded marketing copy.
--
--   cluster_id -> the feedback cluster this answer came from (NULL for the hand-written seeds).
--                 Plain INT, like `feedback.cluster_id` — clusters are ids, not their own table.
--   status     -> only 'published' rows are served publicly; 'draft' is an auto-generated answer
--                 awaiting review, 'archived' is retired copy kept for history.
--   sort_order -> display order on the landing page, lowest first (ties broken by id).
CREATE TABLE IF NOT EXISTS faq_entries (
    id INT AUTO_INCREMENT PRIMARY KEY,
    question VARCHAR(512) NOT NULL,
    answer TEXT NOT NULL,
    cluster_id INT NULL,
    status ENUM('published','draft','archived') NOT NULL DEFAULT 'published',
    sort_order INT NOT NULL DEFAULT 0,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uniq_faq_question (question),
    INDEX idx_faq_published (status, sort_order),
    INDEX idx_faq_cluster (cluster_id)
);

-- Seed answers (INSERT IGNORE: re-running never duplicates a question, and never overwrites an
-- answer the auto-FAQ pass has since improved).
INSERT IGNORE INTO faq_entries (question, answer, sort_order) VALUES
('What does the automation actually do?',
 'LEM writes and schedules your LinkedIn content, then engages for you: it comments on relevant posts in your feed, replies to comments on your own posts, seeds a first comment under what you publish, and sends appreciation and follow-up DMs. Every action follows the targeting rules, tone and per-day caps you set, and posts go through a preview/approval step before anything is published.',
 10),
('How much does it cost, and is there a free trial?',
 'Every plan starts with a 14-day free trial — no credit card required. After the trial, Starter is $29/month, Professional is $79/month and Enterprise is $199/month. The trial gives you the full Professional feature set so you can evaluate content generation, scheduling and engagement automation before you pay.',
 20),
('Is this against LinkedIn''s Terms of Service?',
 'LinkedIn''s User Agreement restricts automated tools, so LEM is built to act like you, not like a bot: it works from your own logged-in session, keeps human-like pacing with per-day caps, and never mass-messages, scrapes profiles in bulk, or resells LinkedIn data. Content is approval-gated by default, so you decide what gets posted. You are responsible for your own account, and you can pause automation or reduce caps at any time.',
 30),
('What happens to my data and my LinkedIn credentials?',
 'Your credentials and session cookies are stored encrypted and used only to run the automation you configured — we never sell or share your data, and we don''t use your content to train public models. Generated posts, comments and DMs stay in your account, and deleting your account removes them.',
 40),
('How does the AI match my voice?',
 'LEM builds a voice profile from your existing LinkedIn posts and profile plus the tone, style, emoji and hashtag preferences you set on the Account page. Every post, comment and DM is generated against that profile and your focus topics, and you can edit or reject anything in the preview step — those edits feed back into how future drafts read.',
 50),
('Can I cancel anytime?',
 'Yes. There are no long-term contracts and no cancellation fees — cancel whenever you like and your account stays active through the end of the current billing period. You can also pause the automation without cancelling if you just need a break.',
 60);
