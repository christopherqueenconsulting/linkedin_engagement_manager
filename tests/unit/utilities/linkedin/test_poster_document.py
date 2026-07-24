"""Unit tests for the native document (PDF) publish path — issue #390."""

import pytest
from unittest.mock import MagicMock, patch


@pytest.mark.unit
class TestDocumentTitle:
    def test_uses_first_non_empty_line(self):
        from cqc_lem.utilities.linkedin.poster import _document_title

        assert _document_title("\n\n5 lessons from 2026\n\nrest of the post") == "5 lessons from 2026"

    def test_truncates_to_linkedin_max(self):
        from cqc_lem.utilities.linkedin.poster import _document_title, DOCUMENT_TITLE_MAX

        title = _document_title("x" * 300)
        assert len(title) == DOCUMENT_TITLE_MAX

    def test_falls_back_when_content_is_blank(self):
        from cqc_lem.utilities.linkedin.poster import _document_title

        assert _document_title("   \n  ") == "Document"
        assert _document_title(None) == "Document"


@pytest.mark.unit
class TestUploadDocument:
    def _init_response(self, value=None):
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json.return_value = {"value": value if value is not None else {
            "uploadUrl": "https://upload.linkedin.example/doc",
            "document": "urn:li:document:abc123",
        }}
        return response

    def test_initializes_uploads_and_returns_document_urn(self, tmp_path):
        from cqc_lem.utilities.linkedin.poster import upload_document

        pdf = tmp_path / "deck.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")

        put_response = MagicMock(status_code=201)
        with patch("requests.post", return_value=self._init_response()) as mock_post, \
             patch("requests.put", return_value=put_response) as mock_put:
            urn = upload_document("token", "sub-1", str(pdf))

        assert urn == "urn:li:document:abc123"
        assert "documents?action=initializeUpload" in mock_post.call_args[0][0]
        assert mock_post.call_args.kwargs["json"] == {
            "initializeUploadRequest": {"owner": "urn:li:person:sub-1"}
        }
        assert mock_post.call_args.kwargs["headers"]["LinkedIn-Version"]
        assert mock_put.call_args[0][0] == "https://upload.linkedin.example/doc"
        assert mock_put.call_args.kwargs["data"] == b"%PDF-1.4 fake"

    def test_raises_when_no_upload_url_returned(self, tmp_path):
        from cqc_lem.utilities.linkedin.poster import upload_document

        pdf = tmp_path / "deck.pdf"
        pdf.write_bytes(b"%PDF-1.4")

        with patch("requests.post", return_value=self._init_response(value={})):
            with pytest.raises(ValueError):
                upload_document("token", "sub-1", str(pdf))

    def test_retired_api_version_is_named_in_the_error(self, tmp_path):
        """A retired LinkedIn-Version 426s every versioned call — say so instead of
        letting it look like the app simply isn't provisioned for documents."""
        import requests
        from cqc_lem.utilities.linkedin.poster import upload_document

        pdf = tmp_path / "deck.pdf"
        pdf.write_bytes(b"%PDF-1.4")

        retired = MagicMock(status_code=426)
        retired.raise_for_status.side_effect = requests.HTTPError("426")

        with patch("requests.post", return_value=retired), \
             patch("cqc_lem.utilities.linkedin.poster.log_error") as mock_log:
            with pytest.raises(requests.HTTPError):
                upload_document("token", "sub-1", str(pdf))

        assert "retired" in mock_log.call_args[0][0]
        assert mock_log.call_args.kwargs["http_status"] == 426

    def test_raises_when_upload_put_fails(self, tmp_path):
        from cqc_lem.utilities.linkedin.poster import upload_document

        pdf = tmp_path / "deck.pdf"
        pdf.write_bytes(b"%PDF-1.4")

        with patch("requests.post", return_value=self._init_response()), \
             patch("requests.put", return_value=MagicMock(status_code=500)):
            with pytest.raises(RuntimeError):
                upload_document("token", "sub-1", str(pdf))


@pytest.mark.unit
class TestShareDocumentOnLinkedin:

    def _slides(self, tmp_path, count=2):
        paths = []
        for i in range(count):
            p = tmp_path / f"slide_{i}.png"
            p.write_bytes(b"png")
            paths.append(str(p))
        return paths

    def test_returns_none_without_credentials(self):
        from cqc_lem.utilities.linkedin.poster import share_document_on_linkedin

        with patch("cqc_lem.utilities.linkedin.poster.get_user_linked_sub_id", return_value=None), \
             patch("cqc_lem.utilities.linkedin.poster.get_user_access_token", return_value=None):
            assert share_document_on_linkedin(1, "content", ["/tmp/slide.png"]) is None

    def test_returns_none_when_a_slide_is_not_a_real_image(self):
        """Same posture as carousels: never publish a partial/placeholder deck."""
        from cqc_lem.utilities.linkedin.poster import share_document_on_linkedin

        with patch("cqc_lem.utilities.linkedin.poster.get_user_linked_sub_id", return_value="sub-1"), \
             patch("cqc_lem.utilities.linkedin.poster.get_user_access_token", return_value="token"), \
             patch("cqc_lem.utilities.linkedin.poster.upload_document") as mock_upload:
            assert share_document_on_linkedin(1, "content", ["A slide title, not a path"]) is None
            mock_upload.assert_not_called()

    def test_returns_none_when_pdf_cannot_be_built(self, tmp_path):
        from cqc_lem.utilities.linkedin.poster import share_document_on_linkedin

        with patch("cqc_lem.utilities.linkedin.poster.get_user_linked_sub_id", return_value="sub-1"), \
             patch("cqc_lem.utilities.linkedin.poster.get_user_access_token", return_value="token"), \
             patch("cqc_lem.utilities.carousel_creator.create_carousel_pdf", return_value=None), \
             patch("cqc_lem.utilities.linkedin.poster.upload_document") as mock_upload:
            assert share_document_on_linkedin(1, "content", self._slides(tmp_path), post_id=9) is None
            mock_upload.assert_not_called()

    def test_publishes_through_versioned_documents_api(self, tmp_path):
        from cqc_lem.utilities.linkedin.poster import share_document_on_linkedin

        with patch("cqc_lem.utilities.linkedin.poster.get_user_linked_sub_id", return_value="sub-1"), \
             patch("cqc_lem.utilities.linkedin.poster.get_user_access_token", return_value="token"), \
             patch("cqc_lem.utilities.carousel_creator.create_carousel_pdf",
                   return_value=str(tmp_path / "deck.pdf")) as mock_pdf, \
             patch("cqc_lem.utilities.linkedin.poster.upload_document",
                   return_value="urn:li:document:abc") as mock_upload, \
             patch("cqc_lem.utilities.linkedin.poster._create_document_post_versioned",
                   return_value="urn:li:share:999") as mock_post:
            urn = share_document_on_linkedin(1, "Deck title\nbody", self._slides(tmp_path), post_id=9)

        assert urn == "urn:li:share:999"
        assert mock_pdf.call_args[0][1] == 9
        mock_upload.assert_called_once()
        assert mock_post.call_args[0][3] == "urn:li:document:abc"
        assert mock_post.call_args[0][4] == "Deck title"

    def test_downloads_slide_urls_and_cleans_up_temp_files(self, tmp_path):
        from cqc_lem.utilities.linkedin.poster import share_document_on_linkedin

        downloaded = str(tmp_path / "downloaded.png")
        with open(downloaded, "wb") as f:
            f.write(b"png")

        with patch("cqc_lem.utilities.linkedin.poster.get_user_linked_sub_id", return_value="sub-1"), \
             patch("cqc_lem.utilities.linkedin.poster.get_user_access_token", return_value="token"), \
             patch("cqc_lem.utilities.linkedin.poster.download_media", return_value=downloaded) as mock_dl, \
             patch("cqc_lem.utilities.carousel_creator.create_carousel_pdf",
                   return_value=str(tmp_path / "deck.pdf")) as mock_pdf, \
             patch("cqc_lem.utilities.linkedin.poster.upload_document", return_value="urn:li:document:abc"), \
             patch("cqc_lem.utilities.linkedin.poster._create_document_post_versioned",
                   return_value="urn:li:share:999"):
            urn = share_document_on_linkedin(1, "content", ["https://assets.example/slide_1.png"], post_id=9)

        assert urn == "urn:li:share:999"
        mock_dl.assert_called_once_with("https://assets.example/slide_1.png")
        assert mock_pdf.call_args[0][0] == [downloaded]
        import os
        assert not os.path.exists(downloaded), "temp download should be cleaned up"

    def test_falls_back_to_legacy_ugcpost_when_versioned_fails(self, tmp_path):
        from cqc_lem.utilities.linkedin.poster import share_document_on_linkedin

        with patch("cqc_lem.utilities.linkedin.poster.get_user_linked_sub_id", return_value="sub-1"), \
             patch("cqc_lem.utilities.linkedin.poster.get_user_access_token", return_value="token"), \
             patch("cqc_lem.utilities.carousel_creator.create_carousel_pdf",
                   return_value=str(tmp_path / "deck.pdf")), \
             patch("cqc_lem.utilities.linkedin.poster.upload_document",
                   side_effect=RuntimeError("not provisioned")), \
             patch("cqc_lem.utilities.linkedin.poster._create_document_post_legacy",
                   return_value="urn:li:ugcPost:555") as mock_legacy:
            urn = share_document_on_linkedin(1, "content", self._slides(tmp_path), post_id=9)

        assert urn == "urn:li:ugcPost:555"
        mock_legacy.assert_called_once()

    def test_returns_none_when_both_paths_fail(self, tmp_path):
        from cqc_lem.utilities.linkedin.poster import share_document_on_linkedin

        with patch("cqc_lem.utilities.linkedin.poster.get_user_linked_sub_id", return_value="sub-1"), \
             patch("cqc_lem.utilities.linkedin.poster.get_user_access_token", return_value="token"), \
             patch("cqc_lem.utilities.carousel_creator.create_carousel_pdf",
                   return_value=str(tmp_path / "deck.pdf")), \
             patch("cqc_lem.utilities.linkedin.poster.upload_document", side_effect=RuntimeError("boom")), \
             patch("cqc_lem.utilities.linkedin.poster._create_document_post_legacy",
                   side_effect=RuntimeError("boom too")):
            assert share_document_on_linkedin(1, "content", self._slides(tmp_path), post_id=9) is None


@pytest.mark.unit
class TestDocumentPostRequests:

    def test_versioned_post_sends_document_media(self):
        from cqc_lem.utilities.linkedin.poster import _create_document_post_versioned

        response = MagicMock(headers={"x-restli-id": "urn:li:share:1"})
        response.raise_for_status = MagicMock()
        with patch("requests.post", return_value=response) as mock_post:
            urn = _create_document_post_versioned("token", "urn:li:person:sub-1", "body",
                                                  "urn:li:document:abc", "Deck")

        assert urn == "urn:li:share:1"
        body = mock_post.call_args.kwargs["json"]
        assert body["content"]["media"] == {"title": "Deck", "id": "urn:li:document:abc"}
        assert body["commentary"] == "body"
        assert body["lifecycleState"] == "PUBLISHED"

    def test_versioned_post_falls_back_to_body_id(self):
        from cqc_lem.utilities.linkedin.poster import _create_document_post_versioned

        response = MagicMock(headers={})
        response.raise_for_status = MagicMock()
        response.json.return_value = {"id": "urn:li:share:2"}
        with patch("requests.post", return_value=response):
            urn = _create_document_post_versioned("token", "urn:li:person:sub-1", "body",
                                                  "urn:li:document:abc", "Deck")

        assert urn == "urn:li:share:2"

    def test_legacy_post_uses_document_share_media_category(self):
        from cqc_lem.utilities.linkedin.poster import _create_document_post_legacy

        restli = MagicMock()
        restli.create.return_value = MagicMock(entity_id="urn:li:ugcPost:2")
        with patch("cqc_lem.utilities.linkedin.poster.upload_media",
                   return_value="urn:li:digitalmediaAsset:xyz"), \
             patch("cqc_lem.utilities.linkedin.poster._restli", return_value=restli):
            urn = _create_document_post_legacy("token", "sub-1", "body", "/tmp/deck.pdf", "Deck")

        assert urn == "urn:li:ugcPost:2"
        entity = restli.create.call_args.kwargs["entity"]
        share_content = entity["specificContent"]["com.linkedin.ugc.ShareContent"]
        assert share_content["shareMediaCategory"] == "DOCUMENT"
        assert share_content["media"][0]["media"] == "urn:li:digitalmediaAsset:xyz"
        assert share_content["media"][0]["title"] == {"text": "Deck"}
