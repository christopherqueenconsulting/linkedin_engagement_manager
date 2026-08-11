-- Media on the weekly group post (issue #1224). Group posts shipped as plain text only: the draft
-- table had nowhere to record an image or a video, so the one format LinkedIn Groups reward least
-- (text-only, with an external link worst of all) was the only one LEM could publish.
--
-- media_url stores the SAME public `/api/assets?file_name=` URL `posts.image_url` does, so the
-- Content Studio reuses the post-image upload/render surface and the publish run resolves it back
-- to a file on disk. NULL = a text-only group post, which stays the default.
--
-- media_type is 'image' or 'video' (GroupPostMediaType), derived from the stored file's extension
-- at write time — never taken from the client — so the publish run never has to guess what it is
-- handing LinkedIn's composer.
ALTER TABLE group_post_drafts
    ADD COLUMN media_url  VARCHAR(1024) NULL AFTER content,
    ADD COLUMN media_type VARCHAR(20)   NULL AFTER media_url;
