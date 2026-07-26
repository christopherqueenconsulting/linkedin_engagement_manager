-- Story bank / fact intake (issue #620): the human-sourced specifics every generated post is
-- anchored to. `profiles.synthesis` (V48) supplies the author's VOICE; this table supplies their
-- FACTS — a real anecdote, number, opinion, client win, mistake or artifact the user actually
-- lived. Generation may use ONLY what is stored here as personal specifics, which is what keeps
-- the absolute no-fabrication rule (issue #416) enforceable instead of aspirational.
--
-- kind          the shape of the raw material, so selection can prefer story-friendly archetypes.
-- happened_at   when it actually happened — lets a post say "back in March" truthfully.
-- used_count /  the rotation counters: the selector prefers the least-used, longest-unused entry so
-- last_used_at  the same anecdote does not show up in three posts a week.
-- active        soft-retire an entry without deleting the history of what it already anchored.
CREATE TABLE IF NOT EXISTS story_bank (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NOT NULL,
    kind ENUM('anecdote','number','opinion','client_win','mistake','artifact') NOT NULL DEFAULT 'anecdote',
    title VARCHAR(255) NOT NULL,
    body TEXT NOT NULL,
    happened_at DATE NULL,
    used_count INT NOT NULL DEFAULT 0,
    last_used_at DATETIME NULL,
    active TINYINT(1) NOT NULL DEFAULT 1,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_story_bank_user_active (user_id, active),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
