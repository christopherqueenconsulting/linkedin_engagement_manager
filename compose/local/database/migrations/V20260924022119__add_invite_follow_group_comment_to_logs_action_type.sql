-- #2117: logs.action_type had no type for a connection invite, a follow or a group comment, so
-- invites (connect, withdrawal, company page) landed as 'engaged', group comments as 'comment',
-- and roster follows were not logged at all. Additive only: every existing value is restated, and
-- rows already written keep their type — the invite cap readers count 'engaged' too.
ALTER TABLE logs
    MODIFY COLUMN action_type ENUM('comment','dm','reply','post','engaged','followup',
                                   'invite','follow','group_comment') NOT NULL;
