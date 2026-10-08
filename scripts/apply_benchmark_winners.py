#!/usr/bin/env python3
"""Turn a benchmark's `recommend` verdicts into a reviewable `.litellm/config.yaml` change (#2256).

`benchmark_models.py` only ever RECOMMENDS: a win is evidence for a swap, not authority to make one,
and nothing in the benchmark writes the live config. This script is the next, still deliberate
step. It reads a results file and, for every recommendation it may act on, proposes the config edit
and the regenerated model registry as a unified diff. It writes only with `--write`, and the result
is meant to become its own PR that cites the benchmark report.

What a promotion looks like:

* The candidate is inserted as a new deployment immediately BEFORE the champion it beat, in that
  champion's tier, and the champion stays as its fallback. Comments above the champion stay with
  the champion.
* **Ordered groups.** When the tier holds only the champion, or every deployment in it carries
  `order`, the candidate takes the champion's priority and everything from there down moves one
  step: LiteLLM routes to the lowest `order` and reaches the next only when it fails.
* **Latency-routed groups.** When other deployments in the tier carry no `order` (an Ollama primary
  beside an OpenAI fallback), no `order` is written. LiteLLM drops every unordered deployment from a
  group as soon as one is ordered, so ordering the pair would silently take the Ollama primary out
  of service. The candidate joins as a latency peer instead, and the script says so.

What it refuses, named per recommendation:

* a `quota_policy` of `hold` (the standing spend policy hands those to the owner; `--include-held`
  overrides);
* a candidate with no first-party production route - an OpenRouter-only id such as
  `anthropic/...`. Production routing stays on LiteLLM with LEM's own keys, so OpenRouter never
  enters the config;
* a champion that is no longer deployed in that tier, and a candidate that already is.

Usage:
  python scripts/apply_benchmark_winners.py RESULTS.json [--write] [--include-held]
RESULTS is `benchmark_models.py --results-out` (text or media) or `--recommendations-out`.
Exit: 0 nothing to apply, 2 a change was proposed (or written with --write), 1 error.
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys
import tempfile
from typing import Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

DEFAULT_CONFIG = ".litellm/config.yaml"
DEFAULT_README = "docs/model-benchmarks/README.md"
DEFAULT_PRICES = ".litellm/model_prices_snapshot.json"
DEFAULT_CATALOG = ".litellm/ollama_catalog_snapshot.json"
DEFAULT_PROVIDER = ".litellm/provider_models_snapshot.json"
_OLLAMA_MARKER = "OLLAMA_CLOUD_URL"

_BLOCK_RE = re.compile(r"^  - model_name:\s*(\S+)\s*$")
_PARAM_RE = re.compile(r"^      (\w+):\s*(.*?)\s*$")
# Champion settings worth carrying onto a candidate that takes over its traffic. Credentials and
# api_base are decided by the candidate's own provider, never copied.
_CARRIED = {"openai": ("request_timeout",),
            "ollama": ("request_timeout", "max_parallel_requests", "rpm")}


# ─────────────────────────────── pure ───────────────────────────────

def load_recommendations(doc) -> dict:
    """Normalise any benchmark output into ``{"kind", "run_id", "date", "recommendations"}``.

    Args:
        doc: A text results document, a media results document (``kind == "media"``), or the bare
            list ``--recommendations-out`` writes.

    Returns:
        The normalised document; each recommendation is ``{tier, model, champion, policy}``.
    """
    if isinstance(doc, list):
        recs, kind, run_id, date = doc, "text", None, None
    elif isinstance(doc, dict) and doc.get("kind") == "media":
        recs = [g for g in doc.get("gates") or [] if g.get("verdict") == "recommend"]
        kind, run_id, date = "media", doc.get("run_id"), doc.get("date")
    elif isinstance(doc, dict):
        recs, kind = doc.get("recommendations") or [], "text"
        run_id, date = doc.get("run_id"), doc.get("date")
    else:
        raise ValueError("not a benchmark results or recommendations file")
    out = []
    for rec in recs:
        if not isinstance(rec, dict) or not rec.get("tier") or not rec.get("model"):
            continue
        policy = (rec.get("quota_policy") or {}).get("decision") if kind == "text" else "adopt"
        out.append({"tier": str(rec["tier"]), "model": str(rec["model"]),
                    "champion": str(rec.get("champion") or ""), "policy": policy or "hold"})
    return {"kind": kind, "run_id": run_id, "date": date, "recommendations": out}


def parse_blocks(text: str) -> list:
    """Every ``- model_name`` deployment in the raw config, with its line span and params.

    Returns:
        ``[{tier, start, end, params: {name: (line_index, value)}}]``; ``end`` is the index of the
        block's last content line.
    """
    lines = text.splitlines()
    blocks: list = []
    current: Optional[dict] = None
    for i, line in enumerate(lines):
        match = _BLOCK_RE.match(line)
        if match:
            current = {"tier": match.group(1), "start": i, "end": i, "params": {}}
            blocks.append(current)
            continue
        if current is None:
            continue
        if line.startswith("    ") and line.strip() and not line.lstrip().startswith("#"):
            current["end"] = i
            param = _PARAM_RE.match(line)
            if param:
                current["params"][param.group(1)] = (i, param.group(2))
        elif line.strip() and not line.startswith(" "):
            current = None  # a top-level key (router_settings:) ends the model list
    return blocks


def _model(block: dict) -> str:
    return (block["params"].get("model") or (None, ""))[1]


def _is_ollama(block: dict) -> bool:
    return _OLLAMA_MARKER in (block["params"].get("api_base") or (None, ""))[1]


def _order(block: dict) -> Optional[int]:
    raw = (block["params"].get("order") or (None, None))[1]
    try:
        return int(raw) if raw is not None else None
    except ValueError:
        return None


def find_champion(blocks: list, tier: str, champion: str, kind: str) -> Optional[dict]:
    """The deployment ``champion`` names in ``tier``.

    A provider-qualified champion (``openai/gpt-4o``) is matched verbatim against ``model:``. A bare
    one is an Ollama tag on a text run and a bare OpenAI id on a media run.
    """
    for block in blocks:
        if block["tier"] != tier:
            continue
        model = _model(block)
        if "/" in champion:
            # Metered: an Ollama deployment's `openai/` prefix is transport, never a match.
            if model == champion and not _is_ollama(block):
                return block
        elif model.split("/", 1)[-1] == champion and _is_ollama(block) == (kind == "text"):
            return block
    return None


def candidate_params(model: str, kind: str, champion: dict) -> tuple:
    """``(params, refusal)`` - the deployment lines for a promoted candidate.

    Args:
        model: The candidate id as the benchmark named it.
        kind: ``text`` or ``media``.
        champion: The champion's block (settings to carry over).

    Returns:
        ``([(name, value)], None)`` or ``(None, reason)``.
    """
    if "/" in model:
        provider, bare = model.split("/", 1)
        if provider != "openai":
            return None, (f"`{model}` has no first-party production route (only OpenRouter "
                          "reaches it) - production stays on LiteLLM with LEM's own keys, so add "
                          "it by hand with its own provider key or not at all")
        params = [("model", f"openai/{bare}"), ("api_key", "os.environ/OPENAI_API_KEY")]
        carried = _CARRIED["openai"]
    elif kind == "media":
        params = [("model", f"openai/{model}"), ("api_key", "os.environ/OPENAI_API_KEY")]
        carried = _CARRIED["openai"]
    else:
        params = [("model", f"openai/{model}"), ("api_base", "os.environ/OLLAMA_CLOUD_URL"),
                  ("api_key", "os.environ/OLLAMA_CLOUD_API_KEY")]
        carried = _CARRIED["ollama"] if _is_ollama(champion) else ()
    for name in carried:
        if name in champion["params"]:
            params.append((name, champion["params"][name][1]))
    return params, None


def promote(text: str, rec: dict, kind: str, *, run_id: Optional[str],
            date: Optional[str]) -> tuple:
    """Apply ONE recommendation to the raw config text.

    Returns:
        ``(new_text, note, refusal)``: the updated text and a one-line note, or the unchanged text
        and why nothing was done.
    """
    blocks = parse_blocks(text)
    tier, model, champion_id = rec["tier"], rec["model"], rec["champion"]
    if not champion_id:
        return text, None, "no champion named - nothing to displace"
    champion = find_champion(blocks, tier, champion_id, kind)
    if champion is None:
        return text, None, f"champion `{champion_id}` is no longer deployed in {tier}"
    params, refusal = candidate_params(model, kind, champion)
    if refusal:
        return text, None, refusal
    group = [b for b in blocks if b["tier"] == tier]
    if any(_model(b) == params[0][1] for b in group):
        return text, None, f"`{params[0][1]}` is already deployed in {tier}"

    lines = text.splitlines()
    edits: dict = {}  # line index -> replacement line, for order bumps
    appends: dict = {}  # block end index -> extra line to add after it
    ordered = len(group) == 1 or all(_order(b) is not None for b in group)
    if ordered:
        new_order = _order(champion) or 1
        for block in group:
            current = _order(block)
            if block is champion and current is None:
                appends[block["end"]] = f"      order: {new_order + 1}"
            elif current is not None and current >= new_order:
                edits[block["params"]["order"][0]] = f"      order: {current + 1}"
        params.append(("order", str(new_order)))
        note = (f"{tier}: `{params[0][1]}` takes order {new_order}; `{champion_id}` stays as its "
                "fallback")
    else:
        note = (f"{tier}: `{params[0][1]}` joins beside `{champion_id}` as a latency peer - the "
                "group has unordered deployments, and ordering any one of them would drop the rest "
                "from service")

    source = f"benchmark {run_id}" if run_id else "a benchmark"
    when = f" ({date})" if date else ""
    new_block = [f"  # Promoted by scripts/apply_benchmark_winners.py from {source}{when}:",
                 f"  # `{model}` earned `recommend` over `{champion_id}` on {tier}.",
                 "  # The champion stays below as its fallback.",
                 f"  - model_name: {tier}", "    litellm_params:"]
    new_block += [f"      {name}: {value}" for name, value in params]
    new_block.append("")

    # Comments directly above the champion describe the champion; insert above them.
    insert_at = champion["start"]
    while insert_at > 0 and lines[insert_at - 1].lstrip().startswith("#") \
            and lines[insert_at - 1].startswith("  "):
        insert_at -= 1

    out: list = []
    for i, line in enumerate(lines):
        if i == insert_at:
            out.extend(new_block)
        out.append(edits.get(i, line))
        if i in appends:
            out.append(appends[i])
    trailer = "\n" if text.endswith("\n") else ""
    return "\n".join(out) + trailer, note, None


def unified(before: str, after: str, path: str) -> str:
    """A ``git diff``-style unified diff of one file."""
    return "".join(difflib.unified_diff(before.splitlines(keepends=True),
                                        after.splitlines(keepends=True),
                                        fromfile=f"a/{path}", tofile=f"b/{path}"))


# ─────────────────────────────── I/O ───────────────────────────────

def _read(path: str) -> str:
    with open(path) as fh:
        return fh.read()


def registry_after(config_text: str, args: argparse.Namespace) -> tuple:
    """``(readme_before, readme_after)`` with the registry regenerated against ``config_text``."""
    import model_registry  # noqa: WPS433 - sibling script
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        fh.write(config_text)
        tmp = fh.name
    try:
        inputs = model_registry.load_inputs(tmp, args.prices, args.catalog,
                                            args.provider_snapshot, args.readme)
        before = inputs["readme"]
        return before, model_registry.update_registry_block(before,
                                                            model_registry.generate(inputs))
    finally:
        os.unlink(tmp)


def main(argv: Optional[list] = None) -> int:
    """CLI entry point; see the module docstring."""
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("results")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--include-held", action="store_true")
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--readme", default=DEFAULT_README)
    ap.add_argument("--prices", default=DEFAULT_PRICES)
    ap.add_argument("--catalog", default=DEFAULT_CATALOG)
    ap.add_argument("--provider-snapshot", default=DEFAULT_PROVIDER)
    args = ap.parse_args(argv)

    try:
        doc = load_recommendations(json.loads(_read(args.results)))
        original = _read(args.config)
    except (OSError, ValueError) as exc:
        print(f"could not read the inputs: {exc}", file=sys.stderr)
        return 1
    if not doc["recommendations"]:
        print("no `recommend` verdicts in this run - nothing to apply")
        return 0

    text = original
    applied = 0
    for rec in doc["recommendations"]:
        label = f"[{rec['tier']}] {rec['champion'] or '?'} -> {rec['model']}"
        if rec["policy"] != "adopt" and not args.include_held:
            print(f"SKIP {label}: the standing spend policy says `{rec['policy']}` - the owner "
                  "decides (re-run with --include-held to propose it anyway)")
            continue
        text, note, refusal = promote(text, rec, doc["kind"], run_id=doc["run_id"],
                                      date=doc["date"])
        if refusal:
            print(f"SKIP {label}: {refusal}")
            continue
        applied += 1
        print(f"PROPOSE {label}: {note}")
    if not applied:
        print("nothing to apply")
        return 0

    readme_before, readme_after = registry_after(text, args)
    print(unified(original, text, args.config), end="")
    print(unified(readme_before, readme_after, args.readme), end="")
    if not args.write:
        print(f"\n{applied} change(s) proposed - nothing written (re-run with --write)")
        return 2
    with open(args.config, "w") as fh:
        fh.write(text)
    with open(args.readme, "w") as fh:
        fh.write(readme_after)
    print(f"\nwrote {args.config} and {args.readme} - open it as its own PR citing the report")
    return 2


if __name__ == "__main__":
    sys.exit(main())
