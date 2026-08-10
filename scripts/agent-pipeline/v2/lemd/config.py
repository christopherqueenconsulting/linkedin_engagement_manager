"""Configuration for the v2 daemon, read from the SAME `config.env` v1 uses.

One file, one set of knobs, one `PAUSED` switch. During migration both runners are installed and
the owner must not have to remember which world a setting belongs to — and `PAUSED` in particular
has to keep meaning "stop everything" for both, or a pause would silently apply to only half the
pipeline.

`config.env` is shell, not INI: it is `source`d by v1 and by every bash action, so this parser
accepts exactly the subset that file uses (`KEY=value`, optional `export`, optional quotes,
`#` comments) and ignores anything else rather than trying to be a shell.
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

BASE = Path(os.environ.get("BASE", "/home/lem/agent-pipeline"))

_ASSIGN = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def parse_env_file(path: str | Path) -> dict[str, str]:
    """Parse the `KEY=value` subset of a shell env file.

    Values are unquoted with `shlex` so `FOO="a b"` yields `a b`. A line that does not parse is
    skipped: this file also contains functions and conditionals in some deployments, and a config
    reader that raises on them would take the daemon down over a comment.
    """
    out: dict[str, str] = {}
    p = Path(path)
    if not p.is_file():
        return out
    for raw in p.read_text(errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _ASSIGN.match(line)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        val = val.split(" #", 1)[0].strip() if not val.startswith(("'", '"')) else val
        try:
            parts = shlex.split(val)
        except ValueError:
            continue
        out[key] = parts[0] if parts else ""
    return out


def _int(src: dict[str, str], key: str, default: int) -> int:
    """Read an int knob, falling back when unset or non-numeric."""
    raw = os.environ.get(key, src.get(key, ""))
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def _str(src: dict[str, str], key: str, default: str) -> str:
    """Read a string knob, treating empty as unset (`CLAUDE_CAPACITY_PCT=` is the idiom here)."""
    raw = os.environ.get(key, src.get(key, ""))
    raw = str(raw).strip()
    return raw or default


@dataclass(frozen=True)
class Config:
    """Resolved daemon configuration."""

    base: Path
    repo: Path
    slug: str
    db_path: Path
    #: Concurrency. `agent_slots` bounds `claude -p` children; `gh_slots` bounds cheap gh-only
    #: mutations so a merge-enable never queues behind a 20-minute implementation run.
    max_agents: int
    gh_slots: int
    scale_per_issues: int
    busy_hours: str
    busy_tz: str
    busy_days: str
    busy_max_agents: int
    #: Wait-state TTLs (seconds). Sized from measured latencies: PR CI ~3 min, merge queue ~3.8 min.
    ttl_ci: int
    ttl_review: int
    ttl_queue: int
    ttl_parked: int
    reconcile_interval: int
    reconcile_interval_degraded: int
    #: How stale `last_webhook_at` may get before the daemon assumes the event path is broken and
    #: polls harder. Generous on purpose: a quiet repo produces no deliveries, and treating quiet as
    #: broken would keep the pipeline permanently degraded on a weekend.
    webhook_stale_seconds: int
    #: How long a SIGTERM waits for in-flight children before giving up on watching them. Rollback
    #: is measured in minutes, so this bounds it — the children keep running either way.
    drain_seconds: int
    #: Shadow mode: observe and log decisions, mutate nothing. The migration runs here for >=3 days.
    shadow: bool
    raw: dict[str, str] = field(default_factory=dict, repr=False)

    @property
    def paused_file(self) -> Path:
        """v1's pause switch, shared on purpose."""
        return self.base / "PAUSED"

    @property
    def heartbeat_file(self) -> Path:
        """Freshness beacon the failsafe cron reads to decide whether v1 should take over."""
        return self.base / "state" / "lemd.heartbeat"

    def is_paused(self) -> bool:
        """True when the owner has stopped the whole pipeline."""
        return self.paused_file.exists()


def load(base: str | Path | None = None) -> Config:
    """Build a `Config` from `config.env` plus environment overrides."""
    b = Path(base or BASE)
    env = parse_env_file(b / "config.env")
    return Config(
        base=b,
        repo=Path(_str(env, "REPO", "/home/lem/linkedin_engagement_manager")),
        slug=_str(env, "SLUG", "christopherqueenconsulting/linkedin_engagement_manager"),
        db_path=Path(_str(env, "LEMD_DB", str(b / "v2" / "state" / "queue.db"))),
        # LEMD_MAX_AGENTS, not MAX_AGENTS: v1's knob is 5 on this box, and reading it here made the
        # documented "cap v2 at 3 until the CPU envelope is measured" a fallback that never applied.
        # v2 runs several agents genuinely concurrently, sharing one cgroup with the prod stack.
        max_agents=_int(env, "LEMD_MAX_AGENTS", 3),
        gh_slots=_int(env, "MAX_GH_ACTIONS", 2),
        scale_per_issues=_int(env, "SCALE_PER_ISSUES", 8),
        busy_hours=_str(env, "BUSY_HOURS", ""),
        busy_tz=_str(env, "BUSY_TZ", "UTC"),
        busy_days=_str(env, "BUSY_DAYS", ""),
        busy_max_agents=_int(env, "BUSY_MAX_AGENTS", 2),
        ttl_ci=_int(env, "LEMD_TTL_CI", 1800),
        ttl_review=_int(env, "FIRST_REVIEW_TIMEOUT_SECONDS", 3600),
        ttl_queue=_int(env, "LEMD_TTL_QUEUE", 900),
        ttl_parked=_int(env, "LEMD_TTL_PARKED", 21600),
        reconcile_interval=_int(env, "LEMD_RECONCILE_INTERVAL", 600),
        reconcile_interval_degraded=_int(env, "LEMD_RECONCILE_INTERVAL_DEGRADED", 120),
        webhook_stale_seconds=_int(env, "LEMD_WEBHOOK_STALE_SECONDS", 1800),
        drain_seconds=_int(env, "LEMD_DRAIN_SECONDS", 20),
        shadow=_str(env, "LEMD_SHADOW", "1") not in ("0", "false", "no"),
        raw=env,
    )
