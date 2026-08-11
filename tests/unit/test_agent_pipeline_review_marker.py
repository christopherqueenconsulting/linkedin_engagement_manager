"""Guards for the adversarial-review marker's detectability.

The marker is how the merge gate answers "has this been reviewed?". If the agent cannot find the
marker it just posted, it concludes the review did not happen and posts again — which is exactly
what occurred on PR #1273: five identical comments from one run, because the marker opens with a
non-BMP emoji that did not survive the round-trip and landed as U+FFFD replacement characters.

The lesson these tests encode: DETECTION must never depend on a decorative character surviving a
trip through a model, a shell, and an API — and, equally, must never be so loose that a comment
which merely MENTIONS the review passes as one. Both halves gate a merge.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_PIPELINE = Path(__file__).resolve().parents[2] / "scripts" / "agent-pipeline"
sys.path.insert(0, str(_PIPELINE / "v2"))

from lemd import github  # noqa: E402

TICK = _PIPELINE / "tick.sh"

needs_jq = pytest.mark.skipif(shutil.which("jq") is None, reason="the detector is a jq filter")


def _var(name: str) -> str:
    """Read a top-level assignment out of tick.sh."""
    m = re.search(rf'^{name}="([^"]*)"', TICK.read_text(), re.M)
    assert m, f"{name} not found in tick.sh"
    return m.group(1)


def _detector_body() -> str:
    """The source of `claude_reviewed_at`, comments stripped.

    The rationale comment names `startswith` to explain why it was wrong, so a naive substring
    check over the raw function reads that explanation as the code still doing it. (Same trap as
    the KillMode test in the v2 defect suite — worth failing for once and never again.)
    """
    body = TICK.read_text()
    fn = body[body.index("claude_reviewed_at()"):]
    fn = fn[: fn.index("\n}")]
    return "\n".join(line for line in fn.splitlines() if not line.lstrip().startswith("#"))


def _jq_filter() -> str:
    """The REAL jq expression tick.sh runs, lifted out of the script.

    Tests that paste a copy of the filter prove only that the copy works; this one fails when the
    shipped query regresses, which is the whole point of testing a detector.
    """
    m = re.search(r"'(\[\(\.comments.*?)'", _detector_body(), re.S)
    assert m, "could not lift the jq filter out of claude_reviewed_at"
    return m.group(1)


def _detect(*comments: dict) -> str:
    """Run the shipped filter over a comments payload and return the timestamp it resolves."""
    out = subprocess.run(
        ["jq", "-r", "--arg", "m", _var("CLAUDE_REVIEW_MARKER_TEXT"), _jq_filter()],
        input=json.dumps({"comments": list(comments)}),
        capture_output=True, text=True, check=False,
    )
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


MANGLED = "���� Claude adversarial review — no findings"


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


def test_detector_never_matches_the_decorated_marker():
    """Matching the DECORATED marker is precisely what broke #1273.

    The decoration cannot survive the trip, so any comparison against `$CLAUDE_REVIEW_MARKER`
    matches nothing and the agent re-reviews forever.
    """
    code = _detector_body()
    assert "CLAUDE_REVIEW_MARKER_TEXT" in code, "detection must use the ASCII anchor"
    assert '"$CLAUDE_REVIEW_MARKER"' not in code, "the decorated marker must not gate detection"


@needs_jq
def test_detector_finds_a_marker_whose_emoji_was_mangled():
    """The real failure: four U+FFFD where the emoji should be."""
    assert _detect({"body": MANGLED, "createdAt": "2026-08-10T03:04:02Z"}) == "2026-08-10T03:04:02Z"


@needs_jq
@pytest.mark.parametrize("body", [
    "🔎 Claude adversarial review — PASS",
    "**🔎 Claude adversarial review** — FIXED 2 findings",
    "## Claude adversarial review\n- a finding",
])
def test_detector_still_finds_an_intact_marker(body):
    """Stripping the decoration must not cost us the un-mangled forms agents actually post."""
    assert _detect({"body": body, "createdAt": "2026-08-10T03:04:02Z"}) == "2026-08-10T03:04:02Z"


@needs_jq
def test_detector_ignores_unrelated_comments():
    """Anchoring on a phrase must not make every comment look like a review."""
    assert _detect({"body": "looks good to me", "createdAt": "2026-01-01T00:00:00Z"}) == ""


@needs_jq
def test_a_comment_that_only_mentions_the_review_is_not_review_evidence():
    """The loosening this fix introduced, bounded.

    MODE=selfreview escalates by posting a Decision Comment INSTEAD of the marker — the review
    deliberately did NOT pass. This repo is also public, so any commenter can write the phrase in
    prose. An unbounded `contains` would read either as "reviewed" and merge the PR.
    """
    decision = (
        "## 🧑‍⚖️ Human decision needed — reply with option letters\n"
        "Held (`needs-human`). Found while running the Claude adversarial review; I cannot "
        "safely fix it.\n"
    )
    assert _detect({"body": decision, "createdAt": "2026-08-10T04:00:00Z"}) == ""
    assert _detect(
        {"body": "the Claude adversarial review missed the race in the sweep",
         "createdAt": "2026-08-10T04:00:00Z"},
    ) == ""


@needs_jq
def test_detector_returns_the_newest_marker():
    """Freshness is compared against the head commit, so the wrong pick silently re-reviews."""
    assert _detect(
        {"body": MANGLED, "createdAt": "2026-08-09T01:00:00Z"},
        {"body": "unrelated", "createdAt": "2026-08-09T02:00:00Z"},
        {"body": MANGLED, "createdAt": "2026-08-10T03:04:02Z"},
    ) == "2026-08-10T03:04:02Z"


def test_pipeline_exports_a_utf8_locale():
    """Cron supplies no locale; under C, non-ASCII text is mangled at the source."""
    body = TICK.read_text()
    assert re.search(r'^export LANG=', body, re.M), "tick.sh must set a locale explicitly"
    assert "C.UTF-8" in body or "UTF-8" in body


@pytest.mark.parametrize("var", ["CLAUDE_REVIEW_MARKER", "CLAUDE_REVIEW_MARKER_TEXT"])
def test_marker_vars_are_defined_before_use(var):
    """A marker must be assigned before it is used.

    Referenced before assignment it silently becomes the empty string, which `contains` treats as
    matching EVERY comment — turning a detection bug into an auto-approve bug.
    """
    body = TICK.read_text()
    assign = body.index(f'{var}="')
    first_use = body.find(f'${var}')
    if first_use != -1:
        assert assign < first_use, f"{var} is used before it is assigned"


# --- v1/v2 agreement ---------------------------------------------------------------------------
# github.py pins these strings to tick.sh's on purpose: "during migration both runners must agree
# on what counts as a review, or v2's shadow decisions diverge from v1's for a reason that is not a
# defect." Fixing only v1 IS that divergence.


def test_v2_pins_the_same_anchor_as_tick_sh():
    """v2 shadows v1's merge decisions; a different anchor makes the shadow lie."""
    assert github.CLAUDE_REVIEW_MARKER_TEXT == _var("CLAUDE_REVIEW_MARKER_TEXT")
    assert github.CLAUDE_REVIEW_MARKER == _var("CLAUDE_REVIEW_MARKER")


def test_v2_review_state_sees_a_mangled_marker(monkeypatch):
    """The #1273 body, through v2's own reader."""
    monkeypatch.setattr(github, "gh_json", lambda *a, **k: {"data": {"repository": {
        "pullRequest": {
            "commits": {"nodes": [{"commit": {"committedDate": "2026-08-10T03:00:00Z"}}]},
            "reviews": {"nodes": []},
            "comments": {"nodes": [{"createdAt": "2026-08-10T03:04:02Z", "body": MANGLED}]},
            "reviewThreads": {"nodes": []},
        }}}})
    state = github.review_state("o/r", 1)
    assert state.reviewed_at == "2026-08-10T03:04:02Z"
    assert state.fresh is True


def test_v2_ignores_a_comment_that_only_mentions_the_review(monkeypatch):
    """Same rule as tick.sh: a comment the phrase does not OPEN is not review evidence."""
    body = "## 🧑‍⚖️ Human decision needed\nHeld during the Claude adversarial review.\n"
    monkeypatch.setattr(github, "gh_json", lambda *a, **k: {"data": {"repository": {
        "pullRequest": {
            "commits": {"nodes": [{"commit": {"committedDate": "2026-08-10T03:00:00Z"}}]},
            "reviews": {"nodes": []},
            "comments": {"nodes": [{"createdAt": "2026-08-10T03:04:02Z", "body": body}]},
            "reviewThreads": {"nodes": []},
        }}}})
    state = github.review_state("o/r", 1)
    assert state.reviewed_at == ""
    assert state.fresh is False


# ---------------------------------------------------------------- dispatcher/gate agreement (#1380)


def _v2_path(*parts: str) -> Path:
    """A file under the v2 tree."""
    return Path(__file__).resolve().parents[2].joinpath("scripts", "agent-pipeline", "v2", *parts)


def test_the_marker_the_dispatcher_asks_for_is_one_the_gate_accepts():
    """The two halves drifted and nothing noticed for a day.

    `agent_run.sh` prompts MODE=selfreview with a MARKER; `review_state` decides freshness by
    matching the comment's opening text. When the dispatcher's fallback said "Claude self-review"
    and the gate matched only "Claude adversarial review", every self-review this pipeline produced
    was invisible to it: `review_fresh` stayed False, `decide` re-dispatched selfreview until the
    budget of 2 was spent, and the PR parked `selfreview_exhausted`. Seven open PRs were stuck on
    that treadmill, one holding a PASSING review posted 13 seconds after its own head commit.
    """
    import re as _re
    src = _v2_path("actions", "agent_run.sh").read_text()
    line = next(ln for ln in src.splitlines() if ln.strip().startswith("selfreview)"))
    marker = _re.search(r"MARKER='([^']*)'", line).group(1)
    # Resolve the shell default the same way bash would when CLAUDE_REVIEW_MARKER is unset — which
    # is always, because that variable belongs to v1's tick.sh and is never exported to a v2 action.
    default = _re.sub(r"^\$\{[A-Z_]+:-", "", marker).rstrip("}")
    undecorated = github.MARKER_DECORATION_RE.sub("", default)
    assert undecorated.startswith(github.REVIEW_MARKER_TEXTS), (
        f"agent_run.sh asks for {default!r}, which review_state() would not count as a review"
    )


def test_a_self_review_marker_is_still_accepted_evidence():
    """Historical markers must keep counting — seven parked PRs are holding them right now."""
    assert github.MARKER_DECORATION_RE.sub("", "🤖 Claude self-review — PASS").startswith(
        github.REVIEW_MARKER_TEXTS)


def test_an_unrelated_comment_is_still_not_review_evidence():
    """Widening the accepted set must not make every comment a review."""
    for body in ("Addressed your review direction: rebased", "🛑 Human decision needed", "LGTM"):
        assert not github.MARKER_DECORATION_RE.sub("", body).startswith(github.REVIEW_MARKER_TEXTS)
