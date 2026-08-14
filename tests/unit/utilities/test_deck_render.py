"""Unit tests for the carousel render receipt — issue #1513.

The receipt is the only record of what a slide layout actually painted, so these cover the two
things that decide whether a deck reading exists at all: a write that must never cost a user their
carousel, and a read that must tell "no receipt" apart from "broken receipt".
"""

import json
from unittest.mock import patch

import pytest

from cqc_lem.utilities import deck_render as dr

pytestmark = pytest.mark.unit


class TestWriteDeckRenderReceipt:
    def test_writes_the_template_and_every_slide(self, tmp_path):
        slides = [{"index": 1, "role": dr.SLIDE_ROLE_COVER, "body_chars": 120,
                   "chars_dropped": 3, "band": False}]
        path = dr.write_deck_render_receipt(str(tmp_path), 87, "bold_listicle", slides)
        assert path == str(tmp_path / dr.DECK_RENDER_FILENAME)
        payload = json.loads((tmp_path / dr.DECK_RENDER_FILENAME).read_text())
        assert payload == {"post_id": 87, "template": "bold_listicle", "slides": slides}

    def test_a_failed_write_warns_and_returns_none(self, tmp_path):
        # Telemetry is never worth a rendered deck: the slides are already on disk by this point.
        with patch("cqc_lem.utilities.logger.log_warning") as warn, \
                patch("builtins.open", side_effect=OSError("read-only volume")):
            assert dr.write_deck_render_receipt(str(tmp_path), 87, "story_arc", []) is None
        assert warn.call_count == 1


class TestReadDeckRenderReceipt:
    def test_reads_back_what_was_written(self, tmp_path):
        dr.write_deck_render_receipt(str(tmp_path), 5, "stat_reveal",
                                     [{"index": 1, "chars_dropped": 0}])
        receipt, probe = dr.read_deck_render_receipt(dr.deck_render_receipt_path(str(tmp_path)))
        assert probe == dr.DECK_PROBE_OK
        assert receipt["template"] == "stat_reveal" and len(receipt["slides"]) == 1

    def test_absent_and_broken_are_different_readings(self, tmp_path):
        assert dr.read_deck_render_receipt(str(tmp_path / "nope.json")) == (None,
                                                                           dr.DECK_PROBE_MISSING)
        assert dr.read_deck_render_receipt(None) == (None, dr.DECK_PROBE_MISSING)
        broken = tmp_path / dr.DECK_RENDER_FILENAME
        broken.write_text("{truncated")
        assert dr.read_deck_render_receipt(str(broken)) == (None, dr.DECK_PROBE_UNREADABLE)

    def test_a_receipt_without_slides_is_unreadable_not_empty(self, tmp_path):
        # An empty deck and a receipt whose shape we do not recognise must not score the same.
        path = tmp_path / dr.DECK_RENDER_FILENAME
        path.write_text(json.dumps({"post_id": 5, "template": "bold_listicle"}))
        assert dr.read_deck_render_receipt(str(path)) == (None, dr.DECK_PROBE_UNREADABLE)
        path.write_text(json.dumps(["not", "a", "receipt"]))
        assert dr.read_deck_render_receipt(str(path)) == (None, dr.DECK_PROBE_UNREADABLE)
