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


def _pick_ollama_tier(labels: str, mode: str = "start", default_tier: str = "lem-agent-tier2") -> str:
    """Extract _pick_ollama_tier from dispatch.sh and run it with the given inputs.

    `default_tier` is separable from the tier2 alias on purpose: a pin that only ever agrees with
    the default is indistinguishable from no pin at all.
    """
    source = DISPATCH_SH.read_text(encoding="utf-8")
    match = re.search(r"\n_pick_ollama_tier\(\) \{.*?\n\}\n", source, re.S)
    assert match, "_pick_ollama_tier not found in dispatch.sh"
    func = match.group(0)
    out = _bash(f'''
        OLLAMA_DEFAULT_TIER="{default_tier}"
        ISSUE_LABELS="{labels}"
        MODE="{mode}"
        {func}
        _pick_ollama_tier
    ''')
    return out.stdout.strip()


def _ensure_ai_labels_creates(existing: list[str], tmp_path: Path) -> list[str]:
    """Run ensure_ai_labels() against a stub `gh` and return the labels it tried to create."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    created = tmp_path / "created.txt"
    listing = tmp_path / "existing.txt"
    listing.write_text("\n".join(existing) + ("\n" if existing else ""), encoding="utf-8")
    gh_stub = bin_dir / "gh"
    gh_stub.write_text(
        "#!/usr/bin/env bash\n"
        'case "$1 $2" in\n'
        f'  "label list") cat "{listing}" ;;\n'
        f'  "label create") shift 2; while [ "$1" = "--repo" ]; do shift 2; done; echo "$1" >> "{created}" ;;\n'
        "esac\n",
        encoding="utf-8",
    )
    gh_stub.chmod(0o755)
    _bash(f'''
        export PATH="{bin_dir}:$PATH"
        source "{LABELS_SH}"
        ensure_ai_labels
    ''')
    if not created.exists():
        return []
    return created.read_text(encoding="utf-8").split()


class TestTierLabelBootstrap:
    def test_ai_labels_includes_all_tier_overrides(self):
        labels = _ai_labels()
        for tier in ("agent:tier:1", "agent:tier:2", "agent:tier:2-alt", "agent:tier:3"):
            assert tier in labels, f"{tier} must be bootstrapped by ensure_ai_labels()"

    def test_every_declared_label_is_created_when_the_repo_has_none(self, tmp_path):
        assert sorted(_ensure_ai_labels_creates([], tmp_path)) == sorted(_ai_labels())

    def test_existing_labels_are_not_recreated(self, tmp_path):
        assert _ensure_ai_labels_creates(sorted(_ai_labels()), tmp_path) == []

    def test_a_longer_name_does_not_mask_its_prefix(self, tmp_path):
        # `agent:tier:2` is a substring of `agent:tier:2-alt` (same for ai:ollama-cloud:tier2).
        # A substring existence check would report the prefix as present and never create it —
        # reintroducing the silent missing-label trap this issue exists to close.
        existing = [name for name in _ai_labels() if name not in ("agent:tier:2", "ai:ollama-cloud:tier2")]
        created = _ensure_ai_labels_creates(sorted(existing), tmp_path)
        assert sorted(created) == ["agent:tier:2", "ai:ollama-cloud:tier2"]


class TestTierRouting:
    def test_tier1_override_routes_to_tier1(self):
        assert _pick_ollama_tier("agent:tier:1") == "lem-agent-tier1"

    def test_tier2_alt_override_routes_to_tier2_alt(self):
        assert _pick_ollama_tier("agent:tier:2-alt") == "lem-agent-tier2-alt"

    def test_tier3_override_routes_to_tier3(self):
        assert _pick_ollama_tier("agent:tier:3") == "lem-agent-tier3"

    def test_tier2_override_is_a_real_pin_not_the_default(self):
        # A label the owner can apply that never changes the tier is the #1228 bug in a new
        # costume, so agent:tier:2 must win over a CHANGED default, not merely agree with it.
        assert _pick_ollama_tier("agent:tier:2", default_tier="lem-agent-tier1") == "lem-agent-tier2"

    def test_tier2_override_beats_the_selfreview_mode_default(self):
        assert _pick_ollama_tier("agent:tier:2", mode="selfreview") == "lem-agent-tier2"

    def test_tier2_pin_does_not_swallow_tier2_alt(self):
        assert _pick_ollama_tier("agent:tier:2-alt") == "lem-agent-tier2-alt"

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
