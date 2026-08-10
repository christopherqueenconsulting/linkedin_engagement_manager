"""Guards for the adversarial-review marker's detectability.

The marker is how the merge gate answers "has this been reviewed?". If the agent cannot find the
marker it just posted, it concludes the review did not happen and posts again — which is exactly
what occurred on PR #1273: five identical comments from one run, because the marker opens with a
non-BMP emoji that did not survive the round-trip and landed as U+FFFD replacement characters.

The lesson these tests encode: DETECTION must never depend on a decorative character surviving a
trip through a model, a shell, and an API.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

TICK = Path(__file__).resolve().parents[2] / "scripts" / "agent-pipeline" / "tick.sh"


def _var(name: str) -> str:
    """Read a top-level assignment out of tick.sh."""
    m = re.search(rf'^{name}="([^"]*)"', TICK.read_text(), re.M)
    assert m, f"{name} not found in tick.sh"
    return m.group(1)


def test_detection_anchor_is_bmp_only():
    """The string detection keys on must survive any UTF-8-capable path.

    Non-BMP (astral, 4-byte) characters are the documented hazard in this repo — CLAUDE.md calls
    out `strip_non_bmp()` for Selenium for the same reason. A detection anchor containing one is a
    silent failure waiting for the first model that normalises it.
    """
    anchor = _var("CLAUDE_REVIEW_MARKER_TEXT")
    assert anchor, "the ASCII anchor must exist"
    for ch in anchor:
        assert ord(ch) <= 0xFFFF, f"{ch!r} (U+{ord(ch):04X}) is non-BMP and must not gate detection"


def test_anchor_is_a_substring_of_the_decorated_marker():
    """The decoration may change; the anchor must keep matching what agents are told to post."""
    assert _var("CLAUDE_REVIEW_MARKER_TEXT") in _var("CLAUDE_REVIEW_MARKER")


def test_detector_matches_by_contains_not_startswith():
    """`startswith` on the decorated marker is precisely what broke #1273."""
    body = TICK.read_text()
    fn = body[body.index("claude_reviewed_at()"):]
    fn = fn[: fn.index("\n}")]
    # Strip comments before asserting: the rationale explains why `startswith` was wrong, and a
    # naive substring check reads that explanation as the code still doing it. (Same trap as the
    # KillMode test in the v2 defect suite — worth failing for once and never again.)
    code = "\n".join(
        line for line in fn.splitlines() if not line.lstrip().startswith("#")
    )
    assert "CLAUDE_REVIEW_MARKER_TEXT" in code, "detection must use the ASCII anchor"
    assert "startswith" not in code, "startswith on the decorated marker cannot match a mangled post"


def test_detector_finds_a_marker_whose_emoji_was_mangled():
    """The real failure: four U+FFFD where the emoji should be.

    Reproduced through the same jq expression tick.sh uses, so this fails if the query regresses.
    """
    anchor = _var("CLAUDE_REVIEW_MARKER_TEXT")
    mangled = "���� Claude adversarial review — no findings"
    payload = (
        '{"comments":[{"body":' + __import__("json").dumps(mangled)
        + ',"createdAt":"2026-08-10T03:04:02Z"}]}'
    )
    out = subprocess.run(
        ["jq", "-r", "--arg", "m", anchor,
         '[(.comments // [])[] | select((.body // "") | contains($m))] | last | .createdAt // empty'],
        input=payload, capture_output=True, text=True, check=False,
    )
    assert out.stdout.strip() == "2026-08-10T03:04:02Z"


def test_detector_ignores_unrelated_comments():
    """Anchoring on a phrase must not make every comment look like a review."""
    anchor = _var("CLAUDE_REVIEW_MARKER_TEXT")
    payload = '{"comments":[{"body":"looks good to me","createdAt":"2026-01-01T00:00:00Z"}]}'
    out = subprocess.run(
        ["jq", "-r", "--arg", "m", anchor,
         '[(.comments // [])[] | select((.body // "") | contains($m))] | last | .createdAt // empty'],
        input=payload, capture_output=True, text=True, check=False,
    )
    assert out.stdout.strip() == ""


def test_pipeline_exports_a_utf8_locale():
    """Cron supplies no locale; under C, non-ASCII text is mangled at the source."""
    body = TICK.read_text()
    assert re.search(r'^export LANG=', body, re.M), "tick.sh must set a locale explicitly"
    assert "C.UTF-8" in body or "UTF-8" in body


@pytest.mark.parametrize("var", ["CLAUDE_REVIEW_MARKER", "CLAUDE_REVIEW_MARKER_TEXT"])
def test_marker_vars_are_defined_before_use(var):
    """A marker referenced before assignment silently becomes the empty string, which `contains`
    treats as matching EVERY comment — turning a detection bug into an auto-approve bug.
    """
    body = TICK.read_text()
    assign = body.index(f'{var}="')
    first_use = body.find(f'${var}')
    if first_use != -1:
        assert assign < first_use, f"{var} is used before it is assigned"
