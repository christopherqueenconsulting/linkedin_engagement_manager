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
import yaml

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
LABELS_SH = _ROOT / "scripts" / "agent-pipeline" / "lib" / "labels.sh"
DISPATCH_SH = _ROOT / "scripts" / "agent-pipeline" / "lib" / "dispatch.sh"
LITELLM_CONFIG = _ROOT / ".litellm" / "config.yaml"


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


def _ollama_tier_context_tokens(tier: str, overrides: dict[str, str] | None = None) -> str:
    """Extract _ollama_tier_context_tokens from dispatch.sh and run it for `tier`."""
    source = DISPATCH_SH.read_text(encoding="utf-8")
    match = re.search(r"\n_ollama_tier_context_tokens\(\) \{.*?\n\}\n", source, re.S)
    assert match, "_ollama_tier_context_tokens not found in dispatch.sh"
    func = match.group(0)
    env = "".join(f'{k}="{v}"\n' for k, v in (overrides or {}).items())
    out = _bash(f'''
        {env}{func}
        _ollama_tier_context_tokens "{tier}"
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


class TestTierContextWindow:
    def test_tier_context_windows_match_documented_defaults(self):
        # Every default is clamped to the fallback floor: what ships is the smallest window in the
        # tier's LiteLLM fallback chain, not the tier's own (see test_default_never_exceeds_*).
        expected = {
            "lem-agent-tier1": "262144",
            "lem-agent-tier2": "262144",
            "lem-agent-tier2-alt": "262144",
            "lem-agent-tier3": "262144",
        }
        for tier, window in expected.items():
            assert _ollama_tier_context_tokens(tier) == window, f"{tier} default context window mismatch"

    def test_raising_the_floor_exposes_the_models_own_window(self):
        """The floor is the only thing holding tier1/tier2-alt down — the real windows are mapped."""
        high = {"OLLAMA_CONTEXT_FLOOR_TOKENS": "1048576"}
        assert _ollama_tier_context_tokens("lem-agent-tier1", high) == "1048576"
        assert _ollama_tier_context_tokens("lem-agent-tier2-alt", high) == "524288"
        assert _ollama_tier_context_tokens("lem-agent-tier2", high) == "262144"

    def test_junk_floor_does_not_win(self):
        """A malformed floor must fall back to the model window, never to an empty/garbage export."""
        assert _ollama_tier_context_tokens("lem-agent-tier1", {"OLLAMA_CONTEXT_FLOOR_TOKENS": "lots"}) == "1048576"

    def test_default_never_exceeds_any_litellm_fallback_target(self):
        """A fallback replays the SAME prompt, so a tier may never be told more than its chain serves.

        Guards the regression this clamp exists for: raising a tier's window without raising every
        target it degrades into turns the fallback ladder into a guaranteed context-length failure.
        """
        config = yaml.safe_load(LITELLM_CONFIG.read_text(encoding="utf-8"))
        chains = {
            alias: targets
            for entry in config["router_settings"]["fallbacks"]
            for alias, targets in entry.items()
            if alias.startswith("lem-agent-")
        }
        assert chains, "no lem-agent-* fallbacks found in .litellm/config.yaml"
        for alias, targets in chains.items():
            own = int(_ollama_tier_context_tokens(alias))
            for target in targets:
                assert own <= int(_ollama_tier_context_tokens(target)), (
                    f"{alias} exports {own} but falls back to {target}, which cannot accept it"
                )

    def test_tier_context_windows_are_overridable(self):
        overrides = {
            "TIER1_CONTEXT_TOKENS": "999999",
            "TIER2_CONTEXT_TOKENS": "111111",
            "TIER2_ALT_CONTEXT_TOKENS": "222222",
            "TIER3_CONTEXT_TOKENS": "333333",
        }
        assert _ollama_tier_context_tokens("lem-agent-tier1", overrides) == "999999"
        assert _ollama_tier_context_tokens("lem-agent-tier2", overrides) == "111111"
        assert _ollama_tier_context_tokens("lem-agent-tier2-alt", overrides) == "222222"
        assert _ollama_tier_context_tokens("lem-agent-tier3", overrides) == "333333"

    def test_unknown_tier_falls_back_to_safe_default(self):
        assert _ollama_tier_context_tokens("lem-agent-tier9") == "262144"
