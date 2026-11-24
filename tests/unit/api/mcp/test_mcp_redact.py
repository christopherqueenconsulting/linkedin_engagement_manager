"""`api/mcp/redact.py`: never-returned keys, third-party fencing, control strip, 64 KB cap."""

import json

import pytest

from cqc_lem.api.mcp import redact

pytestmark = pytest.mark.unit


class TestNeverReturned:
    def test_dropped_at_every_depth(self) -> None:
        value = {"a": {"stripe_customer_id": "cus_x", "keep": 1},
                 "rows": [{"reply_inbound_address": "pin+t@x", "gmail_forward_confirmation": "9"}],
                 "reply_inbound_token": "t"}
        assert redact.finalize(value) == {"a": {"keep": 1}, "rows": [{}]}


class TestFencing:
    def test_withheld_without_include_text(self) -> None:
        row = {"id": 1, "snippet": "hi", "name": "Pat"}
        assert redact.fence_row(row, ("snippet",), False) == {"id": 1, "name": "Pat"}

    def test_fenced_capped_and_null_kept_with_include_text(self) -> None:
        row = {"id": 1, "snippet": "x" * 500, "draft": None, "signals": ["a" * 300]}
        out = redact.fence_row(row, ("snippet", "draft", "signals", "absent"), True)
        assert out["third_party"]["note"] == redact.THIRD_PARTY_NOTE
        assert len(out["third_party"]["snippet"]) == redact.TEXT_CAP
        assert out["third_party"]["draft"] is None, "nothing there is not the same as withheld"
        assert len(out["third_party"]["signals"][0]) == redact.TEXT_CAP
        assert "absent" not in out["third_party"]

    def test_non_rows_pass_through(self) -> None:
        assert redact.fence_rows(None, ("x",), True) is None
        assert redact.fence_rows([1, {"x": "y"}], ("x",), False) == [1, {}]


class TestControlCharacters:
    @pytest.mark.parametrize("char", ["\x00", "\x1b", "\x7f", "‮", "⁦", "​", "﻿"])
    def test_stripped(self, char: str) -> None:
        assert redact.finalize({"t": f"a{char}b"}) == {"t": "ab"}

    def test_newline_and_tab_kept(self) -> None:
        assert redact.strip_controls("a\nb\tc") == "a\nb\tc"


class TestTheSizeCap:
    def test_a_small_result_is_untouched(self) -> None:
        value = {"items": [1, 2, 3]}
        assert redact.cap_result(value) is value

    def test_the_longest_list_is_trimmed_and_marked(self) -> None:
        value = {"items": ["x" * 1000 for _ in range(200)], "small": [1]}
        out = redact.cap_result(value)
        assert len(json.dumps(out).encode()) <= redact.RESULT_CAP_BYTES
        assert out["truncated"] is True
        assert 0 < len(out["items"]) < 200 and out["small"] == [1]

    def test_an_unlistable_oversize_result_becomes_a_notice(self) -> None:
        out = redact.cap_result({"blob": "x" * (redact.RESULT_CAP_BYTES + 10)})
        assert out["truncated"] is True and "blob" not in out
