"""The MCP servers an agent is told it has must actually exist in its worktree.

`.claude/settings.json` pre-enables servers by NAME in `enabledMcpjsonServers`; the definitions
live in `.mcp.json`. The two files were split across the tracked/ignored boundary — settings.json
checked in, `.mcp.json` gitignored — so a pipeline agent running in a fresh `git worktree` got a
checkout that enabled `selenium-lem` and `playwright` and defined neither. Nothing failed loudly:
the agent simply had no browser and reported UI work as unverifiable.

These tests keep the two halves on the same side of that boundary.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _tracked(relpath: str) -> bool:
    """True if git tracks the path — the only thing that makes it exist in a fresh worktree."""
    out = subprocess.run(
        ["git", "ls-files", "--error-unmatch", relpath],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    return out.returncode == 0


def _settings() -> dict:
    return json.loads((ROOT / ".claude" / "settings.json").read_text())


def _mcp() -> dict:
    return json.loads((ROOT / ".mcp.json").read_text())


def test_mcp_config_is_tracked():
    """An ignored .mcp.json does not exist in a worktree, so its servers cannot load."""
    assert _tracked(".mcp.json"), ".mcp.json must be tracked or worktree agents lose every server"


def test_every_enabled_server_is_defined():
    """Enabling a name that nothing defines is the exact silent failure this file exists for."""
    enabled = set(_settings().get("enabledMcpjsonServers", []))
    defined = set(_mcp().get("mcpServers", {}))
    assert enabled <= defined, f"enabled but undefined: {sorted(enabled - defined)}"


@pytest.mark.parametrize("name", sorted(json.loads((ROOT / ".mcp.json").read_text())["mcpServers"]))
def test_server_command_is_not_a_machine_specific_path(name):
    """A command resolved from PATH survives a different box; an absolute one does not.

    The playwright entry pointed at a Homebrew-specific node install
    (`/home/linuxbrew/.linuxbrew/opt/node@24/bin/npx`), which is now a tracked file every
    checkout inherits — including CI images and any contributor's machine.
    """
    command = _mcp()["mcpServers"][name]["command"]
    assert not command.startswith("/"), f"{name}: {command!r} is machine-specific — use PATH"


def test_referenced_local_scripts_are_tracked():
    """A server whose entrypoint is a repo file needs that file present in the worktree too."""
    for name, spec in _mcp()["mcpServers"].items():
        for arg in spec.get("args", []):
            if arg.endswith(".py"):
                assert _tracked(arg), f"{name} runs {arg}, which git does not track"
