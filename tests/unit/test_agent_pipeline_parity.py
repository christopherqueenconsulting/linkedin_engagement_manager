"""Model router phase 4 (#2562): harness parity, subagent aliases, review replay report, queue safety."""

import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

PIPELINE = Path(__file__).resolve().parents[2] / "scripts" / "agent-pipeline"
RUN_LANE = (PIPELINE / "lib" / "run_lane.sh").read_text(encoding="utf-8")
TEMPLATE = PIPELINE / "config" / "codex-pipeline.config.toml"
sys.path.insert(0, str(PIPELINE / "v2"))

import review_replay as rr  # noqa: E402

# ------------------------------------------------------------------ tool parity


def test_codex_template_gives_the_servers_the_claude_profile_allows():
    servers = tomllib.loads(TEMPLATE.read_text(encoding="utf-8"))["mcp_servers"]
    profile = json.loads((PIPELINE / "config" / "claude-headless.json").read_text(encoding="utf-8"))
    allowed = {a.split("__")[1] for a in profile["permissions"]["allow"] if a.startswith("mcp__")}
    assert set(servers) == allowed
    assert servers["graft"] == {"command": "graft", "args": ["mcp"]}


def test_looot_stays_out_of_both_harnesses_until_the_owner_asks():
    assert "looot" not in tomllib.loads(TEMPLATE.read_text(encoding="utf-8"))["mcp_servers"]
    profile = json.loads((PIPELINE / "config" / "claude-headless.json").read_text(encoding="utf-8"))
    assert not [a for a in profile["permissions"]["allow"] if "looot" in a]


def _install(tmp_path: Path, existing: str | None) -> str:
    fn = re.search(r"\nCODEX_CONFIG_MARKER=.*?\n_install_codex_config\(\) \{.*?\n\}\n", RUN_LANE, re.S)
    assert fn, "_install_codex_config not found in lib/run_lane.sh"
    base = tmp_path / "base"
    (base / "config").mkdir(parents=True)
    (base / "config" / TEMPLATE.name).write_text(TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    if existing is not None:
        (home / "config.toml").write_text(existing, encoding="utf-8")
    script = f'log() {{ :; }}\nBASE="{base}"\n{fn.group(0)}\n_install_codex_config "{home}"\n'
    subprocess.run(["bash", "-c", script], check=True, capture_output=True)
    return (home / "config.toml").read_text(encoding="utf-8")


def test_codex_config_installed_when_absent_or_managed(tmp_path):
    assert "[mcp_servers.graft]" in _install(tmp_path / "a", None)
    managed = "# lem-pipeline-managed: codex-pipeline.config.toml\nold = 1\n"
    assert "old" not in _install(tmp_path / "b", managed)


def test_hand_edited_codex_config_is_never_overwritten(tmp_path):
    assert _install(tmp_path, 'model = "mine"\n') == 'model = "mine"\n'


# ------------------------------------------------------------------ subagent alias on a non-Claude lane


def test_ollama_lane_maps_claude_aliases_onto_the_tier():
    """A subagent with `model: sonnet` must resolve to the lane's tier, not to an unserved name."""
    block = RUN_LANE[RUN_LANE.index('if [ "$LANE" = "ollama" ]; then\n    ( cd'):]
    block = block[: block.index("rc=$?")]
    for alias in ("SONNET", "OPUS", "HAIKU"):
        assert f'ANTHROPIC_DEFAULT_{alias}_MODEL="${{AGENT_TIER}}"' in block


# ------------------------------------------------------------------ queue safety


def test_the_only_management_path_requested_is_the_read_only_listing():
    from lemd import usage_signal

    assert usage_signal.AUTH_FILES_PATH == "/v0/management/auth-files"
    needle = "usage" + "-queue"
    users = [p.name for p in PIPELINE.rglob("*") if p.is_file() and p.suffix in {".py", ".sh", ".md"}
             and needle in p.read_text(errors="replace")]
    assert users == []


# ------------------------------------------------------------------ review replay report

SEEDS = {"1": {"defects": ["a", "b"], "risk_high": True}, "2": {"defects": ["c"]}, "3": {"clean": True}}


def _row(pr, model, verdict, found=(), unmatched=0, cost=0.1, run="r"):
    return {"run_id": f"{run}{pr}", "pr": pr, "model": model, "verdict": verdict, "found": list(found),
            "unmatched": unmatched, "tokens_in": 100, "tokens_out": 10, "cost_usd": cost}


def test_seed_without_defects_or_clean_is_refused():
    with pytest.raises(ValueError):
        rr.load_seeds({"9": {}})


def test_summary_numbers():
    seeds = rr.load_seeds(SEEDS)
    rows = [
        _row(1, rr.DEFAULT_BASELINE, "request_changes", ["a"], cost=1.0),
        _row(2, rr.DEFAULT_BASELINE, "request_changes", ["c"], cost=1.0),
        _row(3, rr.DEFAULT_BASELINE, "approve", cost=1.0),
        _row(1, "codex-auto-review", "request_changes", ["a", "b"], cost=0.2),
        _row(2, "codex-auto-review", "approve", [], unmatched=2, cost=0.2),
        _row(3, "codex-auto-review", "approve", cost=0.2),
    ]
    s = rr.summarize(seeds, rows)["codex-auto-review"]
    assert s["agreement"] == pytest.approx(2 / 3)
    assert (s["defects_found"], s["defects_seeded"], s["recall"]) == (2, 3, pytest.approx(2 / 3))
    assert s["false_positives"] == 2 and s["tokens"] == 330 and s["cost_usd"] == pytest.approx(0.6)
    assert rr.summarize(seeds, rows)[rr.DEFAULT_BASELINE]["agreement"] is None
    mix = rr.model_mix(seeds, rows)
    assert mix["router_reviews"] == {"codex-auto-review": 3, rr.DEFAULT_BASELINE: 1}
    assert mix["today_cost_usd"] == pytest.approx(3.0) and mix["router_cost_usd"] == pytest.approx(1.6)


def test_missing_cost_is_unmeasured_not_zero():
    seeds = rr.load_seeds(SEEDS)
    rows = [_row(3, "m", "approve", cost=None), _row(3, rr.DEFAULT_BASELINE, "approve", cost=None)]
    assert rr.summarize(seeds, rows)["m"]["cost_usd"] is None
    assert "n/a" in rr.render(rr.summarize(seeds, rows), rr.model_mix(seeds, rows))


def test_cli_prints_report_and_no_verdict(tmp_path, capsys):
    (tmp_path / "s.json").write_text(json.dumps(SEEDS), encoding="utf-8")
    rows = [_row(3, rr.DEFAULT_BASELINE, "approve"), _row(3, "codex-auto-review", "approve")]
    (tmp_path / "r.ndjson").write_text("\n".join(json.dumps(r) for r in rows) + "\n\n", encoding="utf-8")
    assert rr.main([str(tmp_path / "s.json"), str(tmp_path / "r.ndjson")]) == 0
    out = capsys.readouterr().out
    assert "codex-auto-review" in out and "owner's" in out
