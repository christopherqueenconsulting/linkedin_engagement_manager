"""No-model triage scorer: open issues in, plain trigger-log lines out.

The score is a pure function of an issue's LABEL NAMES, so it costs nothing to run, cannot drift
with a prompt, and gives the same answer twice. It exists so the owner's attention queue is fed by
a deterministic rule instead of by whichever agent happened to read the issue last.

Rule table (first match wins; design in `docs/agent-pipeline-v2.md`, "Triage scorer (P1)"):

| Order | Labels                                                              | Result             |
|-------|---------------------------------------------------------------------|--------------------|
| a     | `blocked-on-chris`, or `risk:live-linkedin`, or `agent:blocked`+`needs-human` | needs-chris, 0 |
| b     | `risk:security` `priority:high` `feedback-loop` `outbound` `revenue` | work, 3            |
| c     | `bug`                                                               | work, 2            |
| d     | `infrastructure` `priority:medium`                                  | work, 1            |
| e     | anything else                                                       | none, 0            |

`priority:low` lowers a work score (b-d) by one, minimum 0; it never touches a needs-chris route.
A score of 0 that is not needs-chris writes no line.

Output is append-only, one line per event, in the ops-triggers.log shape:
`<UTC ts> | <KIND> | lem:<issue>:<score> | <title>`. `NEW` marks the first sight of an issue or any
change of score or route; `REMIND` repeats an unchanged, still-open line once per remind window.
What was last emitted is kept in `<BASE>/state/score-state.json`, so a restart repeats nothing.

The scorer is read-only toward GitHub (one `issue list` per interval, via the `github` helper) and
starts no work, which is why the daemon runs it before the PAUSED gate. It is off unless
`LEMD_SCORER` is 1, true or yes.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import tempfile
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from . import github
from .config import Config, load

LOG = logging.getLogger("lemd.score")

#: Longest title kept on a line, before the `NEEDS-CHRIS: ` prefix.
TITLE_MAX = 160
#: `gh issue list --limit`; reaching it means the list may be truncated.
ISSUE_LIMIT = 200

_NEEDS_CHRIS_ANY = frozenset({"blocked-on-chris", "risk:live-linkedin"})
_NEEDS_CHRIS_ALL = frozenset({"agent:blocked", "needs-human"})
_SCORE_3 = frozenset({"risk:security", "priority:high", "feedback-loop", "outbound", "revenue"})
_SCORE_2 = frozenset({"bug"})
_SCORE_1 = frozenset({"infrastructure", "priority:medium"})

_WS = re.compile(r"\s+")


@dataclass(frozen=True)
class Score:
    """The result of scoring one issue.

    Attributes:
        value: 0-3. Always 0 for `needs-chris` and `none`.
        route: `work`, `needs-chris` or `none`. Only the first two produce a line.
        reason: The label rule that decided it, for the dry run and for debugging.
    """

    value: int
    route: str
    reason: str


def score_issue(labels: Iterable[str]) -> Score:
    """Score one open issue from its label names alone.

    Args:
        labels: Label names.

    Returns:
        The `Score`; the first matching rule wins, then `priority:low` lowers a work score by one.
    """
    ls = frozenset(labels)
    hit = ls & _NEEDS_CHRIS_ANY
    if hit:
        return Score(0, "needs-chris", sorted(hit)[0])
    if _NEEDS_CHRIS_ALL <= ls:
        return Score(0, "needs-chris", "agent:blocked+needs-human")
    for base, group in ((3, _SCORE_3), (2, _SCORE_2), (1, _SCORE_1)):
        hit = ls & group
        if hit:
            reason = sorted(hit)[0]
            if "priority:low" in ls:
                base, reason = max(0, base - 1), f"{reason}, priority:low"
            return Score(base, "work", reason) if base else Score(0, "none", reason)
    return Score(0, "none", "no scoring label")


def sanitise_title(title: object) -> str:
    """Make a title safe for one log field: single line, no `|`, no control characters, capped."""
    text = _WS.sub(" ", str(title if title is not None else ""))
    text = "".join(ch for ch in text if not unicodedata.category(ch).startswith("C"))
    return text.replace("|", "/").strip()[:TITLE_MAX]


@dataclass(frozen=True)
class Line:
    """One trigger-log line."""

    ts: float
    kind: str
    number: int
    score: int
    title: str

    def render(self) -> str:
        """Return the line without its trailing newline."""
        stamp = datetime.fromtimestamp(self.ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return f"{stamp} | {self.kind} | lem:{self.number}:{self.score} | {self.title}"


def plan(issues: list[dict[str, Any]], state: dict[str, Any], now: float,
         remind_seconds: int) -> tuple[list[Line], dict[str, Any]]:
    """Decide which lines to write and what the dedupe state becomes. Pure; touches nothing.

    Args:
        issues: Open issues as `{"number": int, "title": str, "labels": [str]}`.
        state: The previous dedupe state (issue number as str -> score, route, last_emit_ts).
        now: Current epoch seconds.
        remind_seconds: Minimum gap between two lines for an unchanged issue.

    Returns:
        The lines and the new state. An issue absent from `issues` (closed) or scoring `none` is
        absent from the new state, so it starts again as `NEW` if it ever scores.
    """
    lines: list[Line] = []
    new_state: dict[str, Any] = {}
    for issue in issues:
        number = issue.get("number")
        if not isinstance(number, int) or isinstance(number, bool):
            continue
        sc = score_issue(issue.get("labels") or [])
        if sc.route == "none":
            continue
        key = str(number)
        prior = state.get(key) if isinstance(state.get(key), dict) else None
        if prior and prior.get("score") == sc.value and prior.get("route") == sc.route:
            last = prior.get("last_emit_ts")
            last = float(last) if isinstance(last, (int, float)) and not isinstance(last, bool) else 0.0
            if now - last < remind_seconds:
                new_state[key] = prior
                continue
            kind = "REMIND"
        else:
            kind = "NEW"
        title = sanitise_title(issue.get("title"))
        if sc.route == "needs-chris":
            title = f"NEEDS-CHRIS: {title}"
        lines.append(Line(now, kind, number, sc.value, title))
        new_state[key] = {"score": sc.value, "route": sc.route, "last_emit_ts": now}
    return lines, new_state


def append_lines(path: Path, lines: list[str]) -> None:
    """Append lines to the trigger log: 0600 file, 0700 parent, never a symlink, never truncated.

    Raises:
        OSError: If the path is a symlink or cannot be opened for append.
    """
    # The symlink check precedes the empty-lines return on purpose: a misconfigured path should warn.
    # Symlinks in PARENT directories are out of scope (same-user ops box).
    if path.is_symlink():
        raise OSError(f"refusing to write through a symlink: {path}")
    if not lines:
        return
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.fchmod(fd, 0o600)   # `os.open` sets the mode only on creation: tighten a pre-existing file
        for line in lines:
            os.write(fd, (line + "\n").encode("utf-8"))
    finally:
        os.close(fd)


def load_state(path: Path) -> dict[str, Any]:
    """Read the dedupe state; a missing or unreadable file is an empty state."""
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        LOG.warning("score state unreadable, starting empty: %s", exc)
        return {}
    return data if isinstance(data, dict) else {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    """Write the dedupe state atomically (tmp file in the same directory, then `os.replace`)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".score-state.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(state, fh, sort_keys=True)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError as exc:
            LOG.debug("score state temp file not removed: %s", exc)
        raise


def fetch_open_issues(slug: str) -> list[dict[str, Any]]:
    """List open issues (not PRs) with their labels in ONE read-only `gh` call."""
    raw = github.gh_json(
        ["issue", "list", "--repo", slug, "--state", "open", "--limit", str(ISSUE_LIMIT),
         "--json", "number,title,labels"],
        timeout=60,
    )
    # `gh_json` raises on a failed call but returns None for empty stdout: that is a failure to
    # read, not "no open issues", and treating it as the latter would wipe the dedupe state.
    if not isinstance(raw, list):
        raise github.GitHubUnavailable("gh issue list returned no JSON list")
    if len(raw) >= ISSUE_LIMIT:
        LOG.warning("triage scorer: open-issue list hit the %d cap; later issues are not scored", ISSUE_LIMIT)
    return [{"number": r.get("number"), "title": r.get("title"),
             "labels": sorted(github.label_names(r))} for r in raw if isinstance(r, dict)]


class Scorer:
    """Runs the scorer at most once per interval; the daemon owns one instance."""

    def __init__(self, cfg: Config, *, clock: Callable[[], float] = time.time,
                 fetch: Callable[[str], list[dict[str, Any]]] = fetch_open_issues) -> None:
        """Bind to a config; no I/O happens here."""
        self.cfg = cfg
        self._clock = clock
        self._fetch = fetch
        self._last_run: float | None = None

    @property
    def state_path(self) -> Path:
        """Where the dedupe state lives."""
        return self.cfg.base / "state" / "score-state.json"

    def run(self) -> list[str]:
        """Fetch, score and append once the interval has elapsed.

        The interval is charged before the fetch, so a failing `gh` is retried at the next
        interval rather than on every tick.

        Returns:
            The lines appended (empty when throttled or nothing changed).
        """
        now = self._clock()
        if self._last_run is not None and now - self._last_run < self.cfg.scorer_interval:
            return []
        self._last_run = now
        issues = self._fetch(self.cfg.slug)
        state = load_state(self.state_path)
        lines, new_state = plan(issues, state, now, self.cfg.scorer_remind_seconds)
        rendered = [ln.render() for ln in lines]
        # Lines first, state second: a crash between the two can duplicate a line, never lose one.
        append_lines(self.cfg.trigger_log, rendered)
        if new_state != state:
            save_state(self.state_path, new_state)
        LOG.info("triage scorer: %d open issues, %d lines appended", len(issues), len(rendered))
        return rendered


def read_issues_json(path: Path) -> list[dict[str, Any]]:
    """Load issues for the dry run; labels may be names or raw `gh --json` `{"name": ...}` objects.

    Raises:
        OSError: If the file cannot be read.
        ValueError: If it is not a JSON list of objects.
    """
    data = json.loads(path.read_text())
    if not isinstance(data, list) or not all(isinstance(i, dict) for i in data):
        raise ValueError("expected a JSON list of objects")
    out = []
    for item in data:
        labels = item.get("labels") or []
        names = {x for x in labels if isinstance(x, str)}
        names |= github.label_names({"labels": [x for x in labels if isinstance(x, dict)]})
        out.append({**item, "labels": sorted(names)})
    return out


def main(argv: list[str] | None = None) -> int:
    """CLI: print what a run would append, without writing the log or the state."""
    ap = argparse.ArgumentParser(prog="python3 -m lemd.score", description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true", required=True,
                    help="print the lines that would be written; write nothing")
    ap.add_argument("--issues-json", metavar="FILE",
                    help='JSON list of {"number", "title", "labels": [str or {"name": str}]}; '
                         "default: live open issues")
    args = ap.parse_args(argv)
    cfg = load()
    issues: list[dict[str, Any]] = []
    if args.issues_json:
        try:
            issues = read_issues_json(Path(args.issues_json))
        except (OSError, ValueError) as exc:
            ap.error(f"--issues-json {args.issues_json}: {exc}")
    else:
        issues = fetch_open_issues(cfg.slug)
    lines, _ = plan(issues, load_state(Scorer(cfg).state_path), time.time(), cfg.scorer_remind_seconds)
    for ln in lines:
        sys.stdout.write(ln.render() + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
