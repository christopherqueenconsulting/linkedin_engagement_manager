-- MCP W2 (issue #2688, epic #2682): mark rows a user's own coding agent wrote, so the Daily review
-- can badge them "Agent-created" and NEVER sweep one into a multi-item approve.
--
-- scheduled_dms.source and connection_requests.source already carry this ('mcp'). The two queues
-- below had no provenance column at all:
--
-- 1. outreach_funnel_targets.created_via: 'mcp' when an MCP seat created the target
--    (lem_add_outreach_target). NULL = created by a person or by LEM itself (every existing row).
-- 2. catchup_touches.edited_via: 'mcp' when an MCP seat last rewrote the drafted message
--    (lem_edit_catchup_draft). LEM drafts every touch; the agent can only edit a pending one, so
--    the provenance worth showing is who wrote the text the human is about to approve.
--
-- Additive only: two NULLable VARCHAR columns, no default, no index (the review queue reads them
-- off rows it already selects by user and status). VARCHAR, not ENUM, so a later writer needs no
-- migration. The plan's P7 `created_via` on outreach targets (owner ruling O9) is this column.
ALTER TABLE outreach_funnel_targets
    ADD COLUMN created_via VARCHAR(32) NULL;

ALTER TABLE catchup_touches
    ADD COLUMN edited_via VARCHAR(32) NULL;
