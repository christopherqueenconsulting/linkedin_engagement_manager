-- Agent-added story-bank entries land UNVERIFIED (issue #2804, epic #2797 ruling 4).
--
-- Story-bank entries are FACT (#620): the content engine may cite them as the author's own
-- experience. An entry an AI agent added through the MCP server has not been confirmed by the user,
-- so it must never anchor a post, comment or DM until they confirm it in LEM.
--
-- source        who created the row. NULL = the user, in the SPA (every row that exists today, and
--               every row an older app version inserts during a blue/green deploy). 'mcp' = an
--               agent's `lem_add_story_draft`. Provenance only: it is never cleared.
-- verified_at   when the user confirmed an agent-added entry (Confirm, or edit + save). NULL on an
--               agent row means "not yet confirmed".
--
-- A row is VERIFIED when `source IS NULL OR verified_at IS NOT NULL`. Existing rows therefore need
-- NO backfill: they have source NULL, so they count as verified the instant this applies, and an
-- older app inserting rows mid-deploy (it knows neither column) cannot create an unverified one.
-- That keeps this migration purely additive (two nullable columns), and it deliberately leaves
-- `active` alone: `active` is the user's own on/off switch.
ALTER TABLE story_bank
    ADD COLUMN source VARCHAR(32) NULL,
    ADD COLUMN verified_at TIMESTAMP NULL;
