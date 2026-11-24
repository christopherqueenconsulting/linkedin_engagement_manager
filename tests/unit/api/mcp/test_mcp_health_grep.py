"""The runbook's "Am I healthy?" grep matches the MCP tool-call line exactly as the file log writes it.

`docs/mcp-server.md` tells an operator to grep `/opt/lem/logs/cqc_lem_YYYY_MM_DD.log` for failed
tool calls. The pattern is read out of the doc itself and run against a line produced by the REAL
file formatter (`logger._formatter`, the one `_file_handler` uses) from the REAL call site
(`calls._report`), so renaming the message, reordering it or changing the formatter breaks this
test instead of silently breaking the runbook.
"""

import logging
import re
import time
from pathlib import Path
from typing import Any

import pytest

from cqc_lem.api.mcp import calls
from cqc_lem.api.mcp.gate import McpPrincipal
from cqc_lem.utilities import logger as logger_mod

pytestmark = pytest.mark.unit

_DOC = Path(__file__).resolve().parents[4] / "docs" / "mcp-server.md"
_PRINCIPAL = McpPrincipal(user_id=39, session_id=901, scope="mcp", expires_at=None,
                          client_name="test")


def _documented_pattern() -> str:
    """The `grep -E '<pattern>' logs/cqc_lem_…` pattern from the runbook's health check."""
    text = _DOC.read_text()
    match = re.search(r"grep -E '([^']+)' logs/cqc_lem_\$\(date -u \+%Y_%m_%d\)\.log", text)
    assert match, "the runbook no longer documents the health grep"
    return match.group(1)


class _Capture(logging.Handler):
    """Formats every record with the production file formatter."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.setFormatter(logger_mod._formatter)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


def _formatted_line(outcome: str, monkeypatch: pytest.MonkeyPatch) -> str:
    handler = _Capture()
    target = logging.getLogger("cqc-lem")
    target.addHandler(handler)
    monkeypatch.setattr(target, "level", logging.DEBUG)
    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(calls, "track_mcp_tool_call", lambda *a, **k: None)
            mp.setattr(calls, "capture_mcp_analytics", lambda *a, **k: None)
            calls._report(_PRINCIPAL, "lem_whoami", outcome, time.monotonic(), None)
    finally:
        target.removeHandler(handler)
    lines = [line for line in handler.lines if "MCP tool call" in line]
    assert len(lines) == 1
    return lines[0]


@pytest.mark.parametrize("outcome", ["upstream_error", "timeout"])
def test_the_documented_grep_matches_a_failed_call(outcome: str,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    line = _formatted_line(outcome, monkeypatch)
    assert re.search(_documented_pattern(), line), line
    assert line.endswith("| user_id=39"), "the user is on the line for triage"


@pytest.mark.parametrize("outcome", ["ok", "refused", "invalid_args", "rate_limited"])
def test_it_does_not_match_a_call_that_did_not_fail_upstream(
        outcome: str, monkeypatch: pytest.MonkeyPatch) -> None:
    assert not re.search(_documented_pattern(), _formatted_line(outcome, monkeypatch))


def test_the_documented_sample_line_is_what_the_formatter_writes(
        monkeypatch: pytest.MonkeyPatch) -> None:
    line = _formatted_line("ok", monkeypatch)
    shape = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\dZ MCP tool call: tool=lem_whoami "
                       r"outcome=ok latency_ms=\d+ \| user_id=39$")
    assert shape.match(line), line
    assert "MCP tool call: tool=lem_whoami outcome=ok latency_ms=42 | user_id=39" in _DOC.read_text()


def test_the_message_template_is_the_one_documented() -> None:
    assert calls.MCP_TOOL_CALL_LOG == (
        "MCP tool call: tool={tool} outcome={outcome} latency_ms={latency_ms}")


def test_the_posthog_query_filters_on_the_emitted_property(monkeypatch: Any) -> None:
    from cqc_lem.utilities.observability import EVENTS

    text = _DOC.read_text()
    assert "event = 'mcp_tool_call'" in text and "properties.outcome IN" in text
    assert "outcome" in {f.name for f in EVENTS["mcp_tool_call"].fields if f.filtered}
