#!/usr/bin/env python3
"""Weekly LiteLLM model-health check.

Probes every Ollama Cloud model the LiteLLM proxy is configured to use, detects any the
provider has RETIRED (HTTP 410), and — for retirements with a verified-working replacement in
.litellm/model_upgrades.yaml — rewrites the config to swap them. Comment-preserving: swaps are a
single `model:` line replacement, so the rest of the YAML (comments, ordering) is untouched.

This module is intentionally split into PURE logic (parsing, action planning, line rewriting —
unit-tested) and I/O (provider/proxy probes — mocked in tests). The orchestration that applies to
the live box, restarts litellm, smoke-tests and rolls back lives in scripts/weekly_model_check.sh.

CLI:
  --report [--json]      Probe + report model health and planned actions. No writes.
  --plan-json            Emit ONLY the machine-readable plan (for the shell orchestrator).
  --apply OUT            Write the swapped config to OUT (reads --config). Prints applied swaps.
  --smoke-test           Call every tier via the proxy; exit non-zero if any tier fails.
Options:
  --config PATH          LiteLLM config to read (default .litellm/config.yaml).
  --map PATH             Upgrade map (default .litellm/model_upgrades.yaml).
Exit: 0 healthy/no-op, 2 swaps planned/applied, 3 manual alert needed, 1 error.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Callable, Optional

_OLLAMA_MARKER = "OLLAMA_CLOUD_URL"  # api_base env ref that marks an Ollama Cloud deployment


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


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=".litellm/config.yaml")
    ap.add_argument("--map", default=".litellm/model_upgrades.yaml")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--plan-json", action="store_true")
    ap.add_argument("--apply", metavar="OUT")
    args = ap.parse_args(argv)

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
