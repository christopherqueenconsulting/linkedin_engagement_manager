-- Issue #2047 (phase 2 of #1971): the per-user forbidden-claim list.
--
-- Phase 1 shipped the list as ONE global env knob (`FORBIDDEN_CLAIM_TERMS`), which cannot express
-- the operating need — a user saying "never attach a cost or latency figure to $ARTIFACT" and
-- having the product enforce it for THEIR posts and comments. The subject is on the list because
-- no figure about it can be sourced, so the store is the user's own row of engagement preferences,
-- where every other per-user list already lives.
--
-- Same shape as the table's other list columns (include/exclude keywords, focus_topics): a JSON
-- array of strings, NULL until the user saves one, which the reader decodes as an empty list. The
-- global env list still applies to every user — the effective list is the union, never a
-- replacement — so an existing row forbids exactly what it forbade yesterday.
ALTER TABLE engagement_preferences
    ADD COLUMN forbidden_claim_terms JSON NULL;
