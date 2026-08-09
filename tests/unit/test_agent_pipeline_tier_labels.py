"""Regression tests for agent:tier:* label bootstrap and routing (#1228).

`lib/dispatch.sh` reads `agent:tier:*` labels to choose an Ollama cloud model tier, but those
labels were never created, so `gh issue edit --add-label agent:tier:1` silently failed the
entire edit and the documented override could not actually route. This test verifies both that
the labels are bootstrapped in `AI_LABELS` and that `_pick_ollama_tier` maps each one to the
correct LiteLLM alias.
"""

import re
import subprocess
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

LABELS_SH = Path(__file__).resolve().parents[2] / "scripts" / "agent-pipeline" / "lib" / "labels.sh"
DISPATCH_SH = Path(__file__).resolve().parents[2] / "scripts" / "agent-pipeline" / "lib" / "dispatch.sh"


def _bash(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", textwrap.dedent(script)],
        capture_output=True,
        text=True,
        check=True,
    )


def _ai_labels() -> set[str]:
    """Return the AI_LABELS array from labels.sh by sourcing it."""
    out = _bash(f'''
        source "{LABELS_SH}"
        printf "%s\\n" "${{AI_LABELS[@]}}"
    ''')
    return set(out.stdout.strip().splitlines())


def _pick_ollama_tier(labels: str, mode: str = "start") -> str:
    """Extract _pick_ollama_tier from dispatch.sh and run it with the given inputs."""
    source = DISPATCH_SH.read_text(encoding="utf-8")
    match = re.search(r"\n_pick_ollama_tier\(\) \{.*?\n\}\n", source, re.S)
    assert match, "_pick_ollama_tier not found in dispatch.sh"
    func = match.group(0)
    out = _bash(f'''
        OLLAMA_DEFAULT_TIER="lem-agent-tier2"
        ISSUE_LABELS="{labels}"
        MODE="{mode}"
        {func}
        _pick_ollama_tier
    ''')
    return out.stdout.strip()


class TestTierLabelBootstrap:
    def test_ai_labels_includes_all_tier_overrides(self):
        labels = _ai_labels()
        for tier in ("agent:tier:1", "agent:tier:2", "agent:tier:2-alt", "agent:tier:3"):
            assert tier in labels, f"{tier} must be bootstrapped by ensure_ai_labels()"


class TestTierRouting:
    def test_tier1_override_routes_to_tier1(self):
        assert _pick_ollama_tier("agent:tier:1") == "lem-agent-tier1"

    def test_tier2_alt_override_routes_to_tier2_alt(self):
        assert _pick_ollama_tier("agent:tier:2-alt") == "lem-agent-tier2-alt"

    def test_tier3_override_routes_to_tier3(self):
        assert _pick_ollama_tier("agent:tier:3") == "lem-agent-tier3"

    def test_plain_tier2_falls_through_to_default(self):
        # The code does not explicitly match agent:tier:2; tier2 is the default.
        assert _pick_ollama_tier("agent:tier:2") == "lem-agent-tier2"

    def test_no_labels_default_to_tier2(self):
        assert _pick_ollama_tier("") == "lem-agent-tier2"

    def test_selfreview_mode_defaults_to_tier3(self):
        assert _pick_ollama_tier("", mode="selfreview") == "lem-agent-tier3"

    def test_review_mode_defaults_to_tier3(self):
        assert _pick_ollama_tier("", mode="review") == "lem-agent-tier3"

    def test_label_overrides_mode_default(self):
        assert _pick_ollama_tier("agent:tier:1", mode="selfreview") == "lem-agent-tier1"
        assert _pick_ollama_tier("agent:tier:3", mode="start") == "lem-agent-tier3"

    def test_extra_labels_do_not_confuse_matching(self):
        assert _pick_ollama_tier("priority:high agent:tier:1 agent:ready") == "lem-agent-tier1"
