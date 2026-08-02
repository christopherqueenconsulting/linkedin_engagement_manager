#!/usr/bin/env python3
"""Weekly LiteLLM model-health check.

Two halves, both driven by scripts/weekly_model_check.sh:

REACTIVE (the original): probe every Ollama Cloud model the LiteLLM proxy is configured to use,
detect any the provider has RETIRED (HTTP 410), and — for retirements with a verified-working
replacement in .litellm/model_upgrades.yaml — rewrite the config to swap them. Comment-preserving:
swaps are a single `model:` line replacement, so the rest of the YAML is untouched.

PROACTIVE (issue #716): a 410 is the LAST moment to find out. Ollama Cloud publishes its retirement
schedule in advance and ships new models roughly monthly, so the check also reads
docs.ollama.com/cloud.md and ollama.com/api/tags and reports what is about to change:
  * a CONFIGURED model with a FUTURE retirement date -> alert now + pre-append the recommended
    replacement to model_upgrades.yaml (the reactive half then swaps it before the sunset bites);
  * new/removed catalog tags vs the committed snapshot (.litellm/ollama_catalog_snapshot.json) ->
    ONE `agent:ready` evaluation issue for the new ones, snapshot refreshed in the same PR;
  * a CONFIGURED tag whose BUILD moved under an unchanged name (issue #925) -> ONE `agent:ready`
    evaluation issue. An unversioned id like `deepseek-v4-flash` follows whatever the catalog's
    moving tag points at, so the build serving a live tier can change with no repo change at all —
    and a tag-NAME diff cannot see that;
  * a configured model trailing a newer version of its OWN family (minimax-m2.7 while minimax-m3
    ships) -> ONE consolidated `agent:ready` upgrade issue carrying each candidate's usage level,
    because a version bump that jumps usage tiers costs more quota and is an evaluation, never an
    auto-swap.
Every fetch degrades gracefully: an unreachable doc/catalog logs and skips, and the 410 probe stays
the backstop. Repo-visible changes (map, snapshot) land via the existing open-PR path, never as
silent edits on the box.

This module is intentionally split into PURE logic (parsing, action planning, line rewriting,
issue rendering — unit-tested) and I/O (provider probes, HTTP, `gh` — mocked in tests).

CLI:
  --report [--json]      Probe + report model health and planned actions. No writes.
  --plan-json            Emit ONLY the machine-readable plan (for the shell orchestrator).
  --apply OUT            Write the swapped config to OUT (reads --config). Prints applied swaps.
  --catalog-scan         Advance-retirement / new-model / re-pointed-tag / family-version scan.
                         No writes (dry run).
  --catalog-json         Emit ONLY the machine-readable catalog plan (for the shell orchestrator).
  --catalog-apply        Write the planned map additions + snapshot refresh to --map / --snapshot.
  --file-issues          File (or append to) the GitHub issues the catalog scan planned.
Options:
  --plan-file PATH       Act on a plan already emitted by --catalog-json ("-" = stdin) instead of
                         re-scanning. Use it with --file-issues so the filing acts on the SAME
                         reading the decision to file was made from.
  --config PATH          LiteLLM config to read (default .litellm/config.yaml).
  --map PATH             Upgrade map (default .litellm/model_upgrades.yaml).
  --snapshot PATH        Committed catalog snapshot (default .litellm/ollama_catalog_snapshot.json).
  --today YYYY-MM-DD     Override today's date (dry-run proofs / tests).
  --catalog-fixture DIR  Read cloud.md + tags.json from DIR instead of the network (offline proof).
  --no-usage-levels      Skip the per-model usage-level fetch (one page request per candidate).
Exit: 0 healthy/no-op, 2 swaps/actions planned or applied, 3 manual alert needed, 1 error.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import date, datetime, timezone
from typing import Callable, Optional

_OLLAMA_MARKER = "OLLAMA_CLOUD_URL"  # api_base env ref that marks an Ollama Cloud deployment

CLOUD_DOCS_URL = "https://docs.ollama.com/cloud.md"  # plain-markdown mirror; index at /llms.txt
CATALOG_URL = "https://ollama.com/api/tags"
MODEL_PAGE_URL = "https://ollama.com/library/{name}"
DEFAULT_SNAPSHOT = ".litellm/ollama_catalog_snapshot.json"
HTTP_TIMEOUT_SECONDS = 30
GH_TIMEOUT_SECONDS = 60
DEFAULT_REPO = "christopherqueenconsulting/linkedin_engagement_manager"

# Title prefixes ARE the dedup key: an open issue whose title starts with one of these and whose
# text already names a marker is never re-filed (label match would collide with every other
# agent:ready issue — see #598).
UPGRADE_TITLE_PREFIX = "Ollama model family upgrades available:"
EVAL_TITLE_PREFIX = "Evaluate new Ollama Cloud models:"
REPOINT_TITLE_PREFIX = "Re-pointed Ollama tag serving a live tier:"
ISSUE_LABELS = ("agent:ready", "priority:medium", "infrastructure")
MAX_TITLE_CHARS = 120


# ─────────────────────────── pure logic (unit-tested) ────────────────────────────

def parse_deployments(config: dict) -> list[dict]:
    """Flatten a LiteLLM config into [{group, model, bare, is_ollama}] rows. `bare` strips the
    "openai/" (or other provider) prefix; `is_ollama` is True when the deployment routes to
    Ollama Cloud (api_base references OLLAMA_CLOUD_URL)."""
    rows: list[dict] = []
    for entry in config.get("model_list", []) or []:
        group = entry.get("model_name")
        params = entry.get("litellm_params", {}) or {}
        model = params.get("model", "")
        api_base = str(params.get("api_base", "") or "")
        bare = model.split("/", 1)[1] if "/" in model else model
        rows.append({"group": group, "model": model, "bare": bare,
                     "is_ollama": _OLLAMA_MARKER in api_base})
    return rows


def load_map(map_doc: dict) -> dict:
    """Normalize the upgrade-map document into {bare_model: replacement_or_REMOVE}."""
    return dict((map_doc or {}).get("upgrades", {}) or {})


def _unquote(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] in "'\"" and s[-1] == s[0]:
        return s[1:-1]
    return s


def load_config_text(text: str) -> dict:
    """Minimal, dependency-free reader for the bits of a LiteLLM config this tool needs:
    each `- model_name:` entry with its first `model:` and `api_base:`. Avoids a PyYAML runtime
    dependency (this runs as a host ops cron). Sections without `- model_name` (router/litellm
    settings, fallbacks) carry no `model:` lines, so they're naturally ignored."""
    entries: list[dict] = []
    cur: Optional[dict] = None
    for raw in text.splitlines():
        m = re.match(r"\s*-\s*model_name:\s*(.+?)\s*$", raw)
        if m:
            cur = {"model_name": _unquote(m.group(1)), "litellm_params": {}}
            entries.append(cur)
            continue
        if cur is None:
            continue
        mm = re.match(r"\s+model:\s*(.+?)\s*$", raw)
        if mm and "model" not in cur["litellm_params"]:
            cur["litellm_params"]["model"] = _unquote(mm.group(1))
            continue
        mb = re.match(r"\s+api_base:\s*(.+?)\s*$", raw)
        if mb:
            cur["litellm_params"]["api_base"] = _unquote(mb.group(1))
    return {"model_list": entries}


def load_map_text(text: str) -> dict:
    """Parse the quoted `"key": "value"` pairs under `upgrades:` in model_upgrades.yaml without a
    YAML lib. Keys can contain colons (e.g. "kimi-k2:1t"), so both sides must be quoted."""
    upgrades: dict[str, str] = {}
    in_block = False
    for raw in text.splitlines():
        if re.match(r"\s*upgrades:\s*$", raw):
            in_block = True
            continue
        if not in_block:
            continue
        if raw.strip() == "" or raw.lstrip().startswith("#"):
            continue
        m = re.match(r'\s+"([^"]+)"\s*:\s*"([^"]+)"', raw)
        if m:
            upgrades[m.group(1)] = m.group(2)
        elif not raw.startswith((" ", "\t")):
            break  # dedented past the upgrades block
    return {"upgrades": upgrades}


def plan_actions(deployments: list[dict], retired: set[str], upgrades: dict,
                 replacement_ok: Callable[[str], bool]) -> list[dict]:
    """Decide what to do for each Ollama deployment. Pure — `retired` is the set of bare model
    names the provider rejected, and `replacement_ok(bare)` reports whether a candidate works.

    Returns action dicts, each with `kind`:
      SWAP   - retired (or map-scheduled) model has a verified replacement -> rewrite the line
      ALERT  - retired with REMOVE, no mapping, or the mapped replacement also fails -> human
    Non-retired models with a map entry are swapped PROACTIVELY (e.g. a model flagged for
    retirement before it starts 410-ing), as long as the replacement verifies.
    """
    actions: list[dict] = []
    # dedup: a model name can appear in several groups; act on each (group, model) occurrence.
    for d in deployments:
        if not d["is_ollama"]:
            continue
        bare = d["bare"]
        scheduled = bare in upgrades
        is_retired = bare in retired
        if not (is_retired or scheduled):
            continue
        repl = upgrades.get(bare)
        if repl and repl != "REMOVE":
            if replacement_ok(repl):
                actions.append({"kind": "SWAP", "group": d["group"], "old": d["model"],
                                "new": _swap_prefix(d["model"], repl),
                                "old_bare": bare, "new_bare": repl,
                                "reason": "retired" if is_retired else "scheduled"})
            else:
                actions.append({"kind": "ALERT", "group": d["group"], "model": d["model"],
                                "reason": f"mapped replacement {repl!r} also failed to verify"})
        elif repl == "REMOVE":
            if is_retired:
                actions.append({"kind": "ALERT", "group": d["group"], "model": d["model"],
                                "reason": "retired with no replacement (map=REMOVE) — remove the "
                                          "deployment block manually and confirm the tier still "
                                          "has a working model"})
        elif is_retired:
            actions.append({"kind": "ALERT", "group": d["group"], "model": d["model"],
                            "reason": "RETIRED with no mapping — add one to model_upgrades.yaml"})
    return actions


def _swap_prefix(old_model: str, new_bare: str) -> str:
    """Preserve the provider prefix when swapping the bare name (openai/foo -> openai/bar)."""
    prefix = old_model.split("/", 1)[0] + "/" if "/" in old_model else ""
    return f"{prefix}{new_bare}"


def apply_swaps(config_text: str, swaps: list[dict]) -> tuple[str, int]:
    """Rewrite `model: <old>` lines to `model: <new>` in the raw YAML text (comment-preserving).
    Returns (new_text, count). Each swap replaces every exact `model: <old>` occurrence."""
    text = config_text
    count = 0
    for s in swaps:
        old, new = s["old"], s["new"]
        # Match the value on a `model:` line, tolerant of surrounding quotes/whitespace.
        pat = re.compile(r"(^\s*model:\s*['\"]?)" + re.escape(old) + r"(['\"]?\s*$)", re.MULTILINE)
        text, n = pat.subn(lambda m: m.group(1) + new + m.group(2), text)
        count += n
    return text, count


# ───────────── advance retirement scan — docs.ollama.com/cloud.md (pure) ─────────

_MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August", "September",
     "October", "November", "December"], start=1)}

_DOC_DATE_RE = re.compile(r"([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})")
_ACCORDION_RE = re.compile(r'<Accordion\b[^>]*title="([^"]+)"')
_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*$")
_MODEL_TAG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*(?::[A-Za-z0-9._-]+)?")


def is_model_tag(name: str) -> bool:
    """Does this cell actually name a model? cloud.md is a REMOTE document whose cells end up as
    values in model_upgrades.yaml — a file whose entries auto-swap the live LiteLLM config — and
    that writer emits `"key": "value"` lines, so a cell carrying a quote or a newline would inject
    extra mappings of its own. Anything that isn't a plain tag is dropped at the parse boundary."""
    return bool(_MODEL_TAG_RE.fullmatch(str(name or "").strip()))


def parse_doc_date(text: str) -> Optional[str]:
    """"July 31, 2026" -> "2026-07-31". Month names are matched against an explicit table rather
    than strptime("%B"), which is locale-dependent and would silently stop parsing on a box with a
    non-English locale."""
    m = _DOC_DATE_RE.search(str(text or ""))
    if not m:
        return None
    month = _MONTHS.get(m.group(1).lower())
    if not month:
        return None
    try:
        return date(int(m.group(3)), month, int(m.group(2))).isoformat()
    except ValueError:
        return None


def _table_cells(line: str) -> Optional[list[str]]:
    """Split one markdown table row into cells, or None when the line isn't a table row."""
    stripped = line.strip()
    if not stripped.startswith("|"):
        return None
    parts = stripped.split("|")
    if parts and parts[0].strip() == "":
        parts = parts[1:]
    if parts and parts[-1].strip() == "":
        parts = parts[:-1]
    return [p.strip() for p in parts]


def _cell_model(cell: str) -> str:
    return str(cell or "").strip().strip("`").strip()


def parse_retirements(md_text: str) -> list[dict]:
    """Parse the `## Retirements` section of cloud.md into
    [{model, alternative, date, upcoming}] rows.

    Two shapes live under it: a 3-column "Upcoming retirements" table carrying its own date per
    row, and "Past retirements" accordions whose *title* carries the date for a 2-column table.
    Only the Retirements section is read — the page's other tables (API params, capabilities) must
    never be mistaken for a retirement schedule."""
    rows: list[dict] = []
    in_section = False
    upcoming = False
    accordion_date: Optional[str] = None
    for raw in str(md_text or "").splitlines():
        heading = _HEADING_RE.match(raw)
        if heading:
            title = heading.group(2).strip().lower()
            if len(heading.group(1)) <= 2:  # "# …" / "## …" — a new top-level section
                in_section = title.startswith("retirement")
                upcoming = False
                accordion_date = None
            elif in_section:
                upcoming = title.startswith("upcoming")
                accordion_date = None
            continue
        if not in_section:
            continue
        acc = _ACCORDION_RE.search(raw)
        if acc:
            accordion_date = parse_doc_date(acc.group(1))
            continue
        if "</Accordion>" in raw:
            accordion_date = None
            continue
        cells = _table_cells(raw)
        if not cells or len(cells) < 2:
            continue
        if all(re.fullmatch(r":?-{2,}:?", c) for c in cells):  # separator row
            continue
        lowered = [c.lower() for c in cells]
        if "model" in lowered or "retirement date" in lowered:  # header row
            continue
        if len(cells) >= 3:
            when, model, alternative = parse_doc_date(cells[0]), _cell_model(cells[1]), _cell_model(cells[2])
        else:
            when, model, alternative = accordion_date, _cell_model(cells[0]), _cell_model(cells[1])
        if not is_model_tag(model):
            continue
        if not is_model_tag(alternative):
            alternative = ""
        rows.append({"model": model, "alternative": alternative or None, "date": when,
                     "upcoming": bool(upcoming)})
    return rows


def plan_retirement_notices(deployments: list[dict], retirements: list[dict], upgrades: dict,
                            today: str, catalog: Optional[dict] = None) -> tuple[list[dict], dict]:
    """Which CONFIGURED models are scheduled to die, and what to pre-append to the upgrade map.

    Only FUTURE dates produce a notice — a date already past is a live 410 the reactive probe owns,
    and re-reporting it every week would bury the one thing that is still actionable. A mapping is
    only proposed when the docs name an alternative that the live catalog still carries (when the
    catalog is available); "retired with nothing to move to" is a human call, so it alerts and
    leaves the map alone rather than writing REMOVE and silencing itself."""
    configured: dict[str, dict] = {}
    for d in deployments:
        if d["is_ollama"]:
            configured.setdefault(d["bare"], d)
    notices: list[dict] = []
    additions: dict[str, str] = {}
    seen: set[str] = set()
    for r in sorted(retirements, key=lambda x: (x.get("date") or "", x.get("model") or "")):
        bare = r.get("model") or ""
        when = r.get("date")
        if bare not in configured or bare in seen or not when or when <= today:
            continue
        seen.add(bare)
        d = configured[bare]
        alternative = r.get("alternative")
        in_catalog = None if catalog is None else (alternative in catalog if alternative else False)
        notices.append({"kind": "RETIREMENT_NOTICE", "group": d["group"], "model": d["model"],
                        "bare": bare, "date": when, "days_until": _days_until(when, today),
                        "alternative": alternative, "alternative_in_catalog": in_catalog,
                        "mapped": bare in upgrades})
        if alternative and bare not in upgrades and in_catalog is not False:
            additions[bare] = alternative
    return notices, additions


def _days_until(when: str, today: str) -> Optional[int]:
    try:
        return (date.fromisoformat(when) - date.fromisoformat(today)).days
    except (TypeError, ValueError):
        return None


def append_upgrade_mappings(map_text: str, additions: dict, today: str) -> tuple[str, list[str]]:
    """Append `"model": "replacement"` lines to the `upgrades:` block, comment-preserving.

    Keys already mapped are skipped, so re-running is a no-op and the file never grows duplicates.
    Anything that isn't a plain model tag is refused here too (not only at the parse boundary):
    this writer is the last step before a remote-sourced string lands in a file the reactive half
    applies to the live config, so it never emits a value it didn't check.
    Returns (new_text, keys_added)."""
    existing = load_map(load_map_text(map_text))
    fresh = {k: v for k, v in sorted((additions or {}).items())
             if k not in existing and is_model_tag(k) and is_model_tag(v)}
    if not fresh:
        return map_text, []
    lines = map_text.splitlines()
    start = next((i for i, line in enumerate(lines) if re.match(r"\s*upgrades:\s*$", line)), None)
    if start is None:  # no block to extend — create one rather than corrupting the file
        lines += ["", "upgrades:"]
        start = len(lines) - 1
    insert_at = start + 1
    for i in range(start + 1, len(lines)):
        line = lines[i]
        if line.strip() == "" or line.startswith((" ", "\t")):
            if line.strip():
                insert_at = i + 1
            continue
        break
    block = [f"  # --- added by the advance-retirement scan ({today}) ---"]
    block += [f'  "{k}": "{v}"' for k, v in fresh.items()]
    lines[insert_at:insert_at] = block
    return "\n".join(lines) + "\n", list(fresh)


# ──────────────── catalog snapshot — ollama.com/api/tags (pure) ──────────────────

def parse_catalog(payload: dict) -> dict:
    """Normalize /api/tags into {tag: {modified_at, size, parameter_size, family}} — the fields the
    snapshot diff and the evaluation issue body are built from."""
    models: dict[str, dict] = {}
    for entry in (payload or {}).get("models") or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or entry.get("model") or "").strip()
        if not name:
            continue
        details = entry.get("details") or {}
        models[name] = {
            "modified_at": str(entry.get("modified_at") or ""),
            "size": int(entry.get("size") or 0),
            "parameter_size": str((details or {}).get("parameter_size") or ""),
            "family": str((details or {}).get("family") or ""),
        }
    return models


def load_snapshot_text(text: str) -> dict:
    try:
        doc = json.loads(text or "{}")
    except ValueError:
        return {}
    return dict((doc or {}).get("models") or {})


def render_snapshot(models: dict, updated: str) -> str:
    doc = {
        "_comment": "Committed snapshot of ollama.com/api/tags, refreshed by "
                    "scripts/model_health_check.py --catalog-apply. New tags here vs the live "
                    "catalog become ONE agent:ready evaluation issue; do not hand-edit.",
        "updated": updated,
        "models": {k: models[k] for k in sorted(models or {})},
    }
    return json.dumps(doc, indent=2, sort_keys=False) + "\n"


def diff_catalog(snapshot: dict, current: dict) -> dict:
    """Tag NAMES that appeared or disappeared. A tag whose BUILD moved under an unchanged name is
    invisible here by construction — that is `plan_repoints`' job (issue #925)."""
    old, new = set(snapshot or {}), set(current or {})
    return {"added": sorted(new - old), "removed": sorted(old - new)}


_BUILD_FIELDS = ("size", "modified_at")


def build_fingerprint(info: dict) -> str:
    """Identify the BUILD behind a tag. A moving tag keeps its name across a re-point and changes
    these, so they are what a dedup marker has to carry — otherwise a SECOND re-point of the same
    tag reads as already filed."""
    info = info or {}
    return f"{int(info.get('size') or 0)}/{str(info.get('modified_at') or '')}"


def _build_changes(old: dict, new: dict) -> list[dict]:
    """Which build fields moved. A field only counts when BOTH sides carry a value: a snapshot
    written before that field was captured is a MISSING baseline, not a build swap, and treating it
    as one would file an issue for every configured tag at once the day the shape changes."""
    changes: list[dict] = []
    for field in _BUILD_FIELDS:
        before, after = (old or {}).get(field), (new or {}).get(field)
        if not before or not after or before == after:
            continue
        changes.append({"field": field, "old": before, "new": after})
    return changes


def plan_repoints(deployments: list[dict], snapshot: dict, catalog: dict) -> list[dict]:
    """Tags present in BOTH the snapshot and the live catalog whose build changed underneath.

    Restricted to tags `.litellm/config.yaml` actually runs on Ollama Cloud: the catalog carries
    hundreds of tags LEM never serves, and a re-point of one of those is noise. For the ones it does
    serve this is the whole point — an unversioned id follows the vendor's moving tag, so a live
    tier can adopt an unbenchmarked build with no repo change to review."""
    groups: dict[str, list[str]] = {}
    models: dict[str, str] = {}
    for d in deployments:
        if not d["is_ollama"]:
            continue
        groups.setdefault(d["bare"], []).append(d["group"])
        models.setdefault(d["bare"], d["model"])
    out: list[dict] = []
    for bare in sorted(groups):
        before, after = (snapshot or {}).get(bare), (catalog or {}).get(bare)
        if not before or not after:
            continue
        changes = _build_changes(before, after)
        if not changes:
            continue
        out.append({"tag": bare, "model": models[bare], "groups": sorted(set(groups[bare])),
                    "changes": changes, "old": dict(before), "new": dict(after),
                    "old_fingerprint": build_fingerprint(before),
                    "new_fingerprint": build_fingerprint(after)})
    return out


# ───────────────── family-version scan — "are we a major behind?" (pure) ─────────

_FUSED_RE = re.compile(r"^([a-z][a-z0-9]*?)(\d+(?:\.\d+)*)$")
_NUM_RE = re.compile(r"^(\d+(?:\.\d+)*)$")


def parse_model_version(name: str) -> tuple[str, Optional[tuple], str]:
    """Split an Ollama tag into (family, version, size_tag).

    The catalog spells versions three ways — fused into the name (`qwen3.5`, `gemma4`), as their own
    dash-part (`glm-5.1`, `nemotron-3-nano`), or behind a one-letter marker (`minimax-m2.7`,
    `deepseek-v4-flash`). The FIRST version-bearing part wins; everything around it is the family,
    so a variant suffix stays part of the family key (`kimi-k2.7-code` -> `kimi-code`, never an
    "upgrade" for plain `kimi-k2.6`). A size tag (`:397b`) is not a version — `gpt-oss:20b` comes
    back unversioned, which is what keeps size-tagged families out of the comparison."""
    text = str(name or "").strip().lower()
    base, _, size_tag = text.partition(":")
    if not base:
        return "", None, size_tag
    parts = base.split("-")
    for i, part in enumerate(parts):
        prefix = ""
        num = _NUM_RE.match(part)
        if num:
            version_text = num.group(1)
        else:
            fused = _FUSED_RE.match(part)
            if not fused:
                continue
            prefix, version_text = fused.group(1), fused.group(2)
            if len(prefix) == 1:  # m2.7 / k2.6 / v4 — a version marker, not part of the name
                prefix = ""
        family_parts = parts[:i] + ([prefix] if prefix else []) + parts[i + 1:]
        version = tuple(int(x) for x in version_text.split("."))
        return "-".join(p for p in family_parts if p), version, size_tag
    return base, None, size_tag


def version_str(version: Optional[tuple]) -> str:
    return ".".join(str(x) for x in version) if version else "unversioned"


def plan_family_upgrades(deployments: list[dict], catalog: dict,
                         retiring: Optional[set] = None,
                         usage_lookup: Optional[Callable[[str], dict]] = None) -> list[dict]:
    """Configured models that trail a newer version of their OWN family in the live catalog.

    Candidates that the docs already list as retiring are excluded — recommending a model with a
    sunset date on it is worse than saying nothing. `urgency` is "major" when the leading version
    component moves (or when the configured tag is unversioned and a versioned sibling exists);
    minor/patch bumps ride the same issue at lower urgency. Never a swap: `usage` levels are
    attached precisely because a jump in tier burns more Ollama quota, which is a human call."""
    retiring = set(retiring or ())
    by_family: dict[str, list[tuple]] = {}
    for name in sorted(catalog or {}):
        if name in retiring:
            continue
        family, version, _ = parse_model_version(name)
        if version is None:
            continue
        by_family.setdefault(family, []).append((version, name))

    def usage(model: str) -> dict:
        if not usage_lookup:
            return {"level": None, "label": None}
        try:
            return usage_lookup(model) or {"level": None, "label": None}
        except Exception:  # noqa: BLE001 - a usage lookup must never fail the scan
            return {"level": None, "label": None}

    out: list[dict] = []
    seen: set[str] = set()
    groups: dict[str, list[str]] = {}
    for d in deployments:
        if d["is_ollama"]:
            groups.setdefault(d["bare"], []).append(d["group"])
    for d in deployments:
        if not d["is_ollama"] or d["bare"] in seen:
            continue
        bare = d["bare"]
        family, version, _ = parse_model_version(bare)
        candidates = by_family.get(family) or []
        if not candidates:
            continue
        best_version, best_name = max(candidates)
        current = version or ()
        if best_name == bare or best_version <= current:
            continue
        seen.add(bare)
        current_usage, candidate_usage = usage(bare), usage(best_name)
        out.append({
            "family": family, "groups": sorted(set(groups.get(bare) or [])),
            "current": bare, "current_version": version_str(version),
            "candidate": best_name, "candidate_version": version_str(best_version),
            "urgency": "major" if (not version or best_version[0] > version[0]) else "minor",
            "current_usage": current_usage, "candidate_usage": candidate_usage,
            "usage_jump": _usage_jump(current_usage, candidate_usage),
        })
    return out


def _usage_jump(current: dict, candidate: dict) -> Optional[bool]:
    a, b = (current or {}).get("level"), (candidate or {}).get("level")
    if a is None or b is None:
        return None  # unread is never "no jump" — the issue says so in words
    return b > a


# ──────────────────── issue rendering + dedup (pure) ─────────────────────────────

def upgrade_marker(upgrade: dict) -> str:
    return f"{upgrade.get('current')} -> {upgrade.get('candidate')}"


def _usage_text(usage: dict) -> str:
    label, level = (usage or {}).get("label"), (usage or {}).get("level")
    if label and level:
        return f"{label} (level {level})"
    return label or (f"level {level}" if level else "unknown")


def _titled(prefix: str, items: list[str]) -> str:
    title = f"{prefix} {', '.join(items)}"
    if len(title) <= MAX_TITLE_CHARS:
        return title
    kept: list[str] = []
    for item in items:
        candidate = f"{prefix} {', '.join(kept + [item])} (+{len(items) - len(kept) - 1} more)"
        if kept and len(candidate) > MAX_TITLE_CHARS:
            break
        kept.append(item)
    return f"{prefix} {', '.join(kept)} (+{len(items) - len(kept)} more)"


def build_upgrade_issue_title(upgrades: list[dict]) -> str:
    return _titled(UPGRADE_TITLE_PREFIX, [upgrade_marker(u) for u in upgrades])


def build_upgrade_issue_body(upgrades: list[dict], today: str) -> str:
    lines = ["## Context",
             f"The weekly model-health check ({today}) found configured Ollama Cloud models that "
             "trail a newer version of their own family in ollama.com/api/tags. A newer major is "
             "not automatically better *for us*: a bump that raises the model's usage level burns "
             "more Ollama Cloud quota per call, so this is an evaluation, never an auto-swap.",
             ""]
    major = [u for u in upgrades if u.get("urgency") == "major"]
    minor = [u for u in upgrades if u.get("urgency") != "major"]
    for heading, group in (("### Major version behind", major), ("### Minor/patch behind", minor)):
        if not group:
            continue
        lines += [heading, ""]
        for u in group:
            jump = u.get("usage_jump")
            note = ("⚠️ usage level goes UP" if jump else
                    "usage level unchanged or lower" if jump is False else
                    "usage level unknown (page unavailable)")
            lines += [
                f"- **{upgrade_marker(u)}** (family `{u['family']}`, "
                f"{u['current_version']} → {u['candidate_version']}) — "
                f"tiers: {', '.join(u.get('groups') or []) or 'n/a'}",
                f"  - usage: `{u['current']}` {_usage_text(u.get('current_usage'))} → "
                f"`{u['candidate']}` {_usage_text(u.get('candidate_usage'))} — {note}",
            ]
        lines.append("")
    lines += ["## Scope",
              ("- Evaluate each candidate against the tier it would serve (quality on the real " +
               "prompts, latency, and quota cost at the new usage level)."),
              ("- If a swap is worth it, change `.litellm/config.yaml` — do NOT add it to " +
               "`.litellm/model_upgrades.yaml`, which is the RETIREMENT map and auto-swaps."),
              ("- Leave anything that jumps a usage tier alone unless the quality win justifies the " +
               "extra quota burn."),
              "",
              "## Acceptance",
              "- A decision per candidate above (adopt or explicitly decline, with the reason).",
              "- `poetry run pytest tests/unit -q` still passes if the config changed.",
              "",
              "Auto-filed by `scripts/model_health_check.py --file-issues` (issue #716). Dedup "
              "markers (do not remove): " + ", ".join(f"`{upgrade_marker(u)}`" for u in upgrades)]
    return "\n".join(lines)


def build_eval_issue_title(new_tags: list[str]) -> str:
    return _titled(EVAL_TITLE_PREFIX, list(new_tags))


def build_eval_issue_body(new_tags: list[str], catalog: dict, today: str) -> str:
    lines = ["## Context",
             f"The weekly model-health check ({today}) found tags on ollama.com/api/tags that are "
             "not in `.litellm/ollama_catalog_snapshot.json`. New Ollama Cloud models ship roughly "
             "monthly and one of them may be a better fit (or a cheaper one) for a LiteLLM tier.",
             "",
             "## New tags", ""]
    for name in new_tags:
        info = (catalog or {}).get(name) or {}
        bits = []
        if info.get("parameter_size"):
            bits.append(str(info["parameter_size"]))
        if info.get("size"):
            bits.append(f"{int(info['size']) / 1e9:.0f}GB")
        if info.get("modified_at"):
            bits.append(f"updated {str(info['modified_at'])[:10]}")
        family, version, size_tag = parse_model_version(name)
        bits.append(f"family `{family}`, version {version_str(version)}")
        if size_tag:
            bits.append(f"size tag `{size_tag}`")
        lines.append(f"- `{name}` — {'; '.join(bits)}")
    lines += ["",
              "## Scope",
              ("- Read each model's page (https://ollama.com/library/<name>) for its usage level " +
               "and capabilities; a higher usage level costs more Ollama Cloud quota per call."),
              ("- Decide whether any belongs in a `lem-simple` / `lem-medium` / `lem-complex` " +
               "deployment in `.litellm/config.yaml`. Adopting one is a config change, not a " +
               "`.litellm/model_upgrades.yaml` entry (that map is retirement-only and auto-swaps)."),
              ("- Declining is a valid outcome — say so on the issue so the next scan's reader " +
               "knows it was considered."),
              "",
              "## Acceptance",
              "- A decision per tag above.",
              ("- If a model is adopted, `scripts/weekly_model_check.sh`'s tier smoke test still " +
               "passes and `poetry run pytest tests/unit -q` is green."),
              "",
              "Auto-filed by `scripts/model_health_check.py --file-issues` (issue #716). Dedup "
              "markers (do not remove): " + ", ".join(f"`{n}`" for n in new_tags)]
    return "\n".join(lines)


def repoint_marker(repoint: dict) -> str:
    """`tag @ size/modified_at` — the fingerprint is IN the marker on purpose, so a second re-point
    of the same tag is fresh rather than deduped away against the first one's issue."""
    return f"{repoint.get('tag')} @ {repoint.get('new_fingerprint')}"


def _fmt_build_value(field: str, value) -> str:
    if field == "size":
        try:
            return f"{int(value) / 1e9:.0f}GB"
        except (TypeError, ValueError):
            return str(value)
    return str(value)


def build_repoint_issue_title(repoints: list[dict]) -> str:
    return _titled(REPOINT_TITLE_PREFIX, [str(r.get("tag") or "") for r in repoints])


def build_repoint_issue_body(repoints: list[dict], today: str) -> str:
    lines = ["## Context",
             f"The weekly model-health check ({today}) found tags whose BUILD changed while the tag "
             "NAME stayed the same, and `.litellm/config.yaml` runs them. An unversioned Ollama id "
             "follows whatever the catalog's moving tag points at, so a live tier just adopted a "
             "different model with no repo change to review and no benchmark behind it — the one "
             "outcome the evaluation harness exists to prevent (issue #925).",
             "",
             "## Re-pointed tags", ""]
    for r in repoints:
        tiers = ", ".join(f"`{g}`" for g in (r.get("groups") or [])) or "n/a"
        lines.append(f"- **`{r.get('tag')}`** — serving {tiers} (config `model: {r.get('model')}`)")
        for change in r.get("changes") or []:
            field = str(change.get("field"))
            lines.append(f"  - {field}: `{_fmt_build_value(field, change.get('old'))}` → "
                         f"`{_fmt_build_value(field, change.get('new'))}`")
    lines += ["",
              "## Scope",
              ("- Benchmark the build now behind the tag against the tier contract it serves "
               "(`scripts/benchmark_models.py`), the way any candidate is measured."),
              ("- If it holds up, say so on this issue and nothing changes. If it does not, pin the "
               "tier to a versioned id in `.litellm/config.yaml` — that is a deliberate config "
               "change, never a `.litellm/model_upgrades.yaml` entry (that map is retirement-only "
               "and auto-swaps)."),
              ("- **Both sides have to be pinned, not just the candidate.** "
               "`scripts/benchmark_models.py` reads the champion off `.litellm/config.yaml`, which "
               "is this bare tag — and the tag now resolves to the NEW build, so a default run "
               "measures the new build against itself and every `beats champion` expectation ties "
               "for free. Run the new build as the candidate and pin the champion to the build "
               "already measured (the `old` values above): "
               "`--models <new-versioned-id> --champions <tier>=<previous-versioned-id>`."),
              "",
              "## Acceptance",
              "- A decision per tag above (accept the new build, or pin, with the reason).",
              ("- If a tier is pinned, `scripts/weekly_model_check.sh`'s tier smoke test still "
               "passes and `poetry run pytest tests/unit -q` is green."),
              "",
              "Auto-filed by `scripts/model_health_check.py --file-issues` (issue #925). Dedup "
              "markers (do not remove): " + ", ".join(f"`{repoint_marker(r)}`" for r in repoints)]
    return "\n".join(lines)


def build_append_comment(markers: list[str], body: str) -> str:
    return ("The weekly model-health check found more of these since this issue was filed:\n\n"
            + "\n".join(f"- `{m}`" for m in markers)
            + "\n\n<details><summary>Full detail</summary>\n\n" + body + "\n\n</details>")


def plan_issue(open_issues: list[dict], markers: list[str], *, title_prefix: str) -> dict:
    """create / append / none for one consolidated issue.

    Dedup is a literal marker search across the TITLE, BODY and COMMENTS of open issues carrying
    this title prefix — comments count because that is where an append lands, and a label match
    would collide with every other `agent:ready` issue (#598)."""
    if not markers:
        return {"action": "none", "markers": [], "number": None}
    relevant = [i for i in (open_issues or [])
                if str((i or {}).get("title") or "").startswith(title_prefix)]
    chunks: list[str] = []
    for issue in relevant:
        chunks.append(str(issue.get("title") or ""))
        chunks.append(str(issue.get("body") or ""))
        for comment in issue.get("comments") or []:
            chunks.append(str(comment.get("body") if isinstance(comment, dict) else comment or ""))
    haystack = "\n".join(chunks)
    fresh = [m for m in markers if m not in haystack]
    if not fresh:
        return {"action": "none", "markers": [], "number": relevant[0].get("number") if relevant else None}
    if relevant:
        return {"action": "append", "markers": fresh, "number": relevant[0].get("number")}
    return {"action": "create", "markers": fresh, "number": None}


_USAGE_LEVELS = {"low": 1, "medium": 2, "high": 3, "extra high": 4}


def parse_usage_level(html: str) -> dict:
    """Read the "Usage" stat off an ollama.com model page -> {level: 1-4|None, label: str|None}.

    The page renders the level twice — as filled pips and as a word — so the word is preferred and
    the pip count is the fallback when the wording changes. Reading stops at the next stat label so
    a "Context"/"Size" value can never be mistaken for the usage word."""
    text = str(html or "")
    label_at = re.search(r">\s*Usage\s*</\w+>", text)
    if not label_at:
        return {"level": None, "label": None}
    block = text[label_at.end():label_at.end() + 2000]
    stop = re.search(r">\s*(?:Context|Size|Input|Output)\s*</\w+>", block)
    if stop:
        block = block[:stop.start()]
    label = None
    for chunk in re.findall(r">([^<>]+)<", block):
        candidate = " ".join(chunk.split()).lower()
        if candidate in _USAGE_LEVELS:
            label = candidate
            break
    filled = len(re.findall(r"bg-neutral-900", block))
    level = _USAGE_LEVELS.get(label) if label else (filled or None)
    return {"level": level, "label": label}


# ─────────────────────────────── I/O (mocked in tests) ───────────────────────────

def probe_provider(bare_model: str, *, base_url: str, api_key: str, timeout: float = 30.0) -> str:
    """Return 'OK', 'RETIRED', or 'ERROR:<detail>' for a single Ollama Cloud model."""
    try:
        import openai
        client = openai.OpenAI(base_url=base_url, api_key=api_key, timeout=timeout)
        client.chat.completions.create(model=bare_model,
                                       messages=[{"role": "user", "content": "ping"}],
                                       max_tokens=1)
        return "OK"
    except Exception as e:  # noqa: BLE001 - classify any provider failure
        s = str(e)
        if "410" in s or "retired" in s.lower():
            return "RETIRED"
        return "ERROR:" + s[:120]


def smoke_test_tiers(tiers: list[str], call_tier: Callable[[str], bool]) -> dict:
    """Call each tier once via the proxy; return {tier: ok_bool}."""
    return {t: bool(call_tier(t)) for t in tiers}


def http_get(url: str, headers: Optional[dict] = None,
             timeout: float = HTTP_TIMEOUT_SECONDS) -> str:
    """Plain stdlib GET. This tool runs from a host ops cron with no app venv guaranteed, so it
    stays on urllib rather than taking a `requests` dependency."""
    request = urllib.request.Request(url, headers=dict(headers or {}))
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed hosts
        return response.read().decode("utf-8", "replace")


def fetch_cloud_docs(url: str = CLOUD_DOCS_URL) -> Optional[str]:
    try:
        return http_get(url)
    except Exception as e:  # noqa: BLE001 - a doc outage must not fail the health check
        print(f"  ! could not fetch {url}: {str(e)[:200]}", file=sys.stderr)
        return None


def fetch_catalog(api_key: str = "", url: str = CATALOG_URL) -> Optional[dict]:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        return json.loads(http_get(url, headers))
    except Exception as e:  # noqa: BLE001 - same: degrade, the 410 probe is the backstop
        print(f"  ! could not fetch {url}: {str(e)[:200]}", file=sys.stderr)
        return None


def fetch_usage_level(model: str) -> dict:
    """Usage level for one tag. The page is keyed by the model name without its size tag."""
    name = str(model or "").split(":", 1)[0]
    try:
        return parse_usage_level(http_get(MODEL_PAGE_URL.format(name=name)))
    except Exception as e:  # noqa: BLE001
        print(f"  ! could not read usage level for {name}: {str(e)[:120]}", file=sys.stderr)
        return {"level": None, "label": None}


class GitHubIssues:
    """GitHub via the `gh` CLI — the cron host is already authenticated with it, so this needs no
    token of its own (and never handles one)."""

    def __init__(self, repo: str = DEFAULT_REPO) -> None:
        self.repo = repo

    def _run(self, args: list) -> subprocess.CompletedProcess:
        return subprocess.run(args, capture_output=True, text=True, timeout=GH_TIMEOUT_SECONDS)

    def open_issues(self, title_prefix: str) -> list[dict]:
        """Open issues whose title starts with `title_prefix`, with bodies AND comments.

        Listed and filtered client-side rather than handed to `--search`: the search index
        tokenizes and lags, and a miss here means a DUPLICATE issue every week. The repo's open
        backlog is small enough that one full list is cheaper than that risk.

        Fails CLOSED (raises): an unreadable listing read as "nothing filed yet" would re-file the
        same upgrade issue every week."""
        result = self._run(["gh", "issue", "list", "--repo", self.repo, "--state", "open",
                            "--limit", "200", "--json", "number,title,body,comments"])
        if result.returncode != 0:
            raise RuntimeError(f"gh issue list failed: {(result.stderr or '').strip()[:200]}")
        try:
            found = json.loads(result.stdout or "[]")
        except ValueError:
            raise RuntimeError("gh issue list returned unparseable JSON")
        return [i for i in found
                if isinstance(i, dict) and str(i.get("title") or "").startswith(title_prefix)]

    def create(self, title: str, body: str, labels=ISSUE_LABELS) -> Optional[str]:
        args = ["gh", "issue", "create", "--repo", self.repo, "--title", title, "--body", body]
        for label in labels:
            args += ["--label", label]
        result = self._run(args)
        if result.returncode != 0:
            print(f"  ! gh issue create failed: {(result.stderr or '').strip()[:300]}",
                  file=sys.stderr)
            return None
        return (result.stdout or "").strip()

    def comment(self, number: int, body: str) -> bool:
        result = self._run(["gh", "issue", "comment", str(number), "--repo", self.repo,
                            "--body", body])
        if result.returncode != 0:
            print(f"  ! gh issue comment failed: {(result.stderr or '').strip()[:300]}",
                  file=sys.stderr)
            return False
        return True


# ─────────────────────────────────── CLI ─────────────────────────────────────────

def _read_text(path: str) -> str:
    with open(path) as f:
        return f.read()


def _detect(config_path: str, map_path: str) -> dict:
    base_url = os.environ.get("OLLAMA_CLOUD_URL", "")
    api_key = os.environ.get("OLLAMA_CLOUD_API_KEY", "")
    if not base_url or not api_key:
        raise SystemExit("OLLAMA_CLOUD_URL / OLLAMA_CLOUD_API_KEY must be set to probe the provider")
    deployments = parse_deployments(load_config_text(_read_text(config_path)))
    upgrades = load_map(load_map_text(_read_text(map_path)))
    ollama = {d["bare"] for d in deployments if d["is_ollama"]}
    # Probe the current models and (lazily) any candidate replacements.
    status = {m: probe_provider(m, base_url=base_url, api_key=api_key) for m in sorted(ollama)}
    retired = {m for m, s in status.items() if s == "RETIRED"}
    repl_cache: dict[str, bool] = {}

    def replacement_ok(bare: str) -> bool:
        if bare not in repl_cache:
            repl_cache[bare] = probe_provider(bare, base_url=base_url, api_key=api_key) == "OK"
        return repl_cache[bare]

    actions = plan_actions(deployments, retired, upgrades, replacement_ok)
    return {"status": status, "retired": sorted(retired), "actions": actions,
            "swaps": [a for a in actions if a["kind"] == "SWAP"],
            "alerts": [a for a in actions if a["kind"] == "ALERT"]}


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _fixture_sources(directory: str) -> tuple[Optional[str], Optional[dict], Callable[[str], dict]]:
    """Offline stand-ins for the three network reads, so `--catalog-scan --catalog-fixture DIR`
    proves the whole plan (alert -> map PR -> issue bodies) without touching the network."""
    docs_path = os.path.join(directory, "cloud.md")
    tags_path = os.path.join(directory, "tags.json")
    docs = _read_text(docs_path) if os.path.exists(docs_path) else None
    catalog: Optional[dict] = None
    if os.path.exists(tags_path):
        catalog = json.loads(_read_text(tags_path))

    def usage(model: str) -> dict:
        name = str(model or "").split(":", 1)[0]
        page = os.path.join(directory, "usage", f"{name}.html")
        return parse_usage_level(_read_text(page)) if os.path.exists(page) else {"level": None,
                                                                                "label": None}

    return docs, catalog, usage


def _catalog_scan(config_path: str, map_path: str, snapshot_path: str, *, today: str,
                  fixture: Optional[str] = None, usage_levels: bool = True) -> dict:
    """The proactive half: what is about to retire, what is new, and what family we trail.

    Nothing here writes; `--catalog-apply` / `--file-issues` act on the plan this returns."""
    deployments = parse_deployments(load_config_text(_read_text(config_path)))
    upgrades = load_map(load_map_text(_read_text(map_path)))

    usage_fn: Optional[Callable[[str], dict]] = None
    if fixture:
        docs_text, catalog_payload, usage_fn = _fixture_sources(fixture)
    else:
        docs_text = fetch_cloud_docs()
        catalog_payload = fetch_catalog(os.environ.get("OLLAMA_CLOUD_API_KEY", ""))
        if usage_levels:
            cache: dict[str, dict] = {}

            def usage_fn(model: str) -> dict:  # noqa: F811 - only bound on the network path
                if model not in cache:
                    cache[model] = fetch_usage_level(model)
                return cache[model]

    retirements = parse_retirements(docs_text) if docs_text is not None else []
    catalog = parse_catalog(catalog_payload) if catalog_payload is not None else None

    notices, additions = plan_retirement_notices(deployments, retirements, upgrades, today, catalog)

    # A missing snapshot is SEEDED, not diffed — a first run must not file an evaluation issue for
    # the entire catalog.
    seeded = not os.path.exists(snapshot_path)
    snapshot = {} if seeded else load_snapshot_text(_read_text(snapshot_path))
    repoints: list[dict] = []
    if catalog is None:
        catalog_diff = {"added": [], "removed": []}
        family_upgrades: list[dict] = []
    else:
        catalog_diff = {"added": [], "removed": []} if seeded else diff_catalog(snapshot, catalog)
        if not seeded:
            repoints = plan_repoints(deployments, snapshot, catalog)
        family_upgrades = plan_family_upgrades(
            deployments, catalog, {r["model"] for r in retirements},
            usage_fn if usage_levels else None)

    snapshot_changed = catalog is not None and catalog != snapshot
    plan = {
        "today": today,
        "sources": {"docs": docs_text is not None, "catalog": catalog is not None,
                    "snapshot_seeded": seeded},
        "notices": notices,
        "map_additions": additions,
        "catalog": {**catalog_diff, "total": len(catalog or {})},
        "upgrades": family_upgrades,
        "repoints": repoints,
        "snapshot_changed": snapshot_changed,
        "repo_changes": bool(additions) or snapshot_changed,
    }
    plan["issues"] = {
        "upgrade": {"markers": [upgrade_marker(u) for u in family_upgrades],
                    "title": build_upgrade_issue_title(family_upgrades) if family_upgrades else "",
                    "body": build_upgrade_issue_body(family_upgrades, today) if family_upgrades else ""},
        "evaluation": {"markers": list(catalog_diff["added"]),
                       "title": build_eval_issue_title(catalog_diff["added"]) if catalog_diff["added"] else "",
                       "body": build_eval_issue_body(catalog_diff["added"], catalog or {}, today)
                       if catalog_diff["added"] else ""},
        "repoint": {"markers": [repoint_marker(r) for r in repoints],
                    "title": build_repoint_issue_title(repoints) if repoints else "",
                    "body": build_repoint_issue_body(repoints, today) if repoints else ""},
    }
    plan["_catalog"] = catalog  # consumed by --catalog-apply; stripped from --catalog-json output
    return plan


def _print_catalog_plan(plan: dict) -> None:
    sources = plan["sources"]
    print(f"Ollama catalog scan ({plan['today']}): docs={'ok' if sources['docs'] else 'UNAVAILABLE'}"
          f" catalog={'ok' if sources['catalog'] else 'UNAVAILABLE'}"
          f" tags={plan['catalog']['total']}")
    if sources["snapshot_seeded"]:
        print("  snapshot missing — seeding it this run (no evaluation issue filed)")
    for n in plan["notices"]:
        alt = n["alternative"] or "no alternative published"
        print(f"  RETIRING [{n['group']}] {n['bare']} on {n['date']} "
              f"({n['days_until']}d) -> {alt}"
              f"{'' if n['mapped'] else ' [not yet mapped]'}")
    for k, v in plan["map_additions"].items():
        print(f"  MAP + \"{k}\": \"{v}\"")
    for name in plan["catalog"]["added"]:
        print(f"  NEW TAG {name}")
    for name in plan["catalog"]["removed"]:
        print(f"  GONE TAG {name}")
    for u in plan["upgrades"]:
        print(f"  UPGRADE ({u['urgency']}) {upgrade_marker(u)} — usage "
              f"{_usage_text(u['current_usage'])} -> {_usage_text(u['candidate_usage'])}")
    for r in plan.get("repoints") or []:
        detail = ", ".join(f"{c['field']} {_fmt_build_value(c['field'], c['old'])} -> "
                           f"{_fmt_build_value(c['field'], c['new'])}" for c in r["changes"])
        print(f"  REPOINTED {r['tag']} [{', '.join(r['groups'])}] {detail}")
    for kind in ("upgrade", "evaluation", "repoint"):
        spec = _issue_spec(plan, kind)
        if spec["markers"]:
            print(f"\n--- {kind} issue ---\n{spec['title']}\n\n{spec['body']}\n")
    if not (plan["notices"] or plan["map_additions"] or plan["catalog"]["added"]
            or plan["catalog"]["removed"] or plan["upgrades"] or plan.get("repoints")):
        print("  nothing to do")


def _catalog_apply(plan: dict, map_path: str, snapshot_path: str) -> None:
    added = []
    if plan["map_additions"]:
        text, added = append_upgrade_mappings(_read_text(map_path), plan["map_additions"],
                                              plan["today"])
        if added:
            with open(map_path, "w") as f:
                f.write(text)
    for key in added:
        print(f"mapped {key} -> {plan['map_additions'][key]} in {map_path}")
    if plan["snapshot_changed"] and plan["_catalog"] is not None:
        with open(snapshot_path, "w") as f:
            f.write(render_snapshot(plan["_catalog"], plan["today"]))
        print(f"snapshot refreshed ({plan['catalog']['total']} tags) -> {snapshot_path}")


def _issue_spec(plan: dict, kind: str) -> dict:
    """One issue spec off a plan, tolerating a plan saved by an older version of this tool (a
    --plan-file written before a kind existed simply has nothing to file for it)."""
    spec = ((plan or {}).get("issues") or {}).get(kind) or {}
    return {"markers": list(spec.get("markers") or []), "title": spec.get("title") or "",
            "body": spec.get("body") or ""}


def _file_issues(plan: dict, github: GitHubIssues) -> int:
    """File/append the consolidated issues. Never raises: a GitHub failure leaves the work for next
    week rather than failing the weekly check."""
    filed = 0
    for kind, prefix in (("upgrade", UPGRADE_TITLE_PREFIX), ("evaluation", EVAL_TITLE_PREFIX),
                         ("repoint", REPOINT_TITLE_PREFIX)):
        spec = _issue_spec(plan, kind)
        if not spec["markers"]:
            continue
        try:
            existing = github.open_issues(prefix)
        except Exception as e:  # noqa: BLE001 - fail closed: skip rather than risk a duplicate
            print(f"  ! dedup lookup failed for {kind} issue: {e}", file=sys.stderr)
            continue
        decision = plan_issue(existing, spec["markers"], title_prefix=prefix)
        if decision["action"] == "none":
            print(f"  {kind} issue: already filed (#{decision['number']})")
        elif decision["action"] == "append":
            if github.comment(decision["number"], build_append_comment(decision["markers"],
                                                                      spec["body"])):
                print(f"  {kind} issue: appended {len(decision['markers'])} to "
                      f"#{decision['number']}")
                filed += 1
        else:
            url = github.create(spec["title"], spec["body"])
            if url:
                print(f"  {kind} issue: filed {url}")
                filed += 1
    return filed


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=".litellm/config.yaml")
    ap.add_argument("--map", default=".litellm/model_upgrades.yaml")
    ap.add_argument("--snapshot", default=DEFAULT_SNAPSHOT)
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--plan-json", action="store_true")
    ap.add_argument("--apply", metavar="OUT")
    ap.add_argument("--catalog-scan", action="store_true")
    ap.add_argument("--catalog-json", action="store_true")
    ap.add_argument("--catalog-apply", action="store_true")
    ap.add_argument("--file-issues", action="store_true")
    ap.add_argument("--today", default=None)
    ap.add_argument("--plan-file", metavar="PATH", default=None)
    ap.add_argument("--catalog-fixture", default=None)
    ap.add_argument("--no-usage-levels", action="store_true")
    ap.add_argument("--repo", default=DEFAULT_REPO)
    args = ap.parse_args(argv)

    if args.catalog_scan or args.catalog_json or args.catalog_apply or args.file_issues:
        if args.plan_file:
            # --catalog-json strips the raw catalog, so a reloaded plan can describe the snapshot
            # refresh but can never write it. Refuse loudly rather than silently skipping the write.
            if args.catalog_apply:
                raise SystemExit("--plan-file cannot drive --catalog-apply "
                                 "(the emitted plan omits the raw catalog)")
            plan = json.loads(sys.stdin.read() if args.plan_file == "-"
                              else _read_text(args.plan_file))
            plan.setdefault("_catalog", None)
        else:
            plan = _catalog_scan(args.config, args.map, args.snapshot,
                                 today=args.today or _today(), fixture=args.catalog_fixture,
                                 usage_levels=not args.no_usage_levels)
        if args.catalog_json:
            print(json.dumps({k: v for k, v in plan.items() if k != "_catalog"}))
        else:
            _print_catalog_plan(plan)
        if args.catalog_apply:
            _catalog_apply(plan, args.map, args.snapshot)
        if args.file_issues:
            _file_issues(plan, GitHubIssues(args.repo))
        if plan["notices"]:
            return 3
        if (plan["repo_changes"] or plan["upgrades"] or plan["catalog"]["added"]
                or plan.get("repoints")):
            return 2
        return 0

    result = _detect(args.config, args.map)

    if args.plan_json:
        print(json.dumps(result))
    elif args.apply:
        with open(args.config) as f:
            text = f.read()
        new_text, n = apply_swaps(text, result["swaps"])
        with open(args.apply, "w") as f:
            f.write(new_text)
        for s in result["swaps"]:
            print(f"SWAP [{s['group']}] {s['old']} -> {s['new']} ({s['reason']})")
        print(f"applied {n} line-swap(s) -> {args.apply}")
    else:  # --report (default)
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print("Model health:")
            for m, s in result["status"].items():
                print(f"  {m:<22} {s}")
            for a in result["actions"]:
                if a["kind"] == "SWAP":
                    print(f"  PLAN swap [{a['group']}] {a['old']} -> {a['new']} ({a['reason']})")
                else:
                    print(f"  ALERT [{a['group']}] {a.get('model','')}: {a['reason']}")

    if result["alerts"]:
        return 3
    if result["swaps"]:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
