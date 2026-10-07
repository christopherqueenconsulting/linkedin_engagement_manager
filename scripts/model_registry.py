#!/usr/bin/env python3
"""The ONE model registry: every LiteLLM tier, generated, never hand-edited (issue #2251).

What a tier runs, what it costs, how it last measured, when it is due to disappear and whether its
own family has moved on used to live in five places - `.litellm/config.yaml`, two snapshots, the
benchmark leaderboard and whoever last read a provider's deprecation page. This script joins them
into one table between marker comments in `docs/model-benchmarks/README.md`.

The table is a pure function of COMMITTED files, so the same commit always renders the same table
(and `tests/unit/test_model_registry.py` fails the build when the committed table drifts):

* `.litellm/config.yaml` - tiers and deployment order;
* `.litellm/model_prices_snapshot.json` - the pinned LiteLLM cost map. OpenAI/Perplexity entries are
  refreshed from BerriAI/litellm's `model_prices_and_context_window.json` by `--refresh-prices`,
  which records the source URL and fetch date in the file's `_pinned` block;
* `.litellm/ollama_catalog_snapshot.json` - Ollama Cloud's catalog (newer-in-family for Ollama tags);
* `.litellm/provider_models_snapshot.json` - the OpenAI/Perplexity scan (sunsets, newer-in-family);
* the text and media leaderboards in the README itself - last verdict and date.

CLI:
  --write             Regenerate the registry block in --readme.
  --check             Exit 1 when the committed block differs from what --write would produce.
  --print             Print the table only.
  --dry-run           Print the table AND the planned media-benchmark spend. No network, no writes,
                      no paid API.
  --refresh-prices    Re-pin configured + candidate OpenAI/Perplexity prices from LiteLLM's cost map
                      (--upstream-file PATH to read a saved copy instead of the network).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import benchmark_media as media  # noqa: E402 - sibling script
import model_health_check as mhc  # noqa: E402 - sibling script
import provider_model_scan as pms  # noqa: E402 - sibling script

DEFAULT_CONFIG = ".litellm/config.yaml"
DEFAULT_PRICES = ".litellm/model_prices_snapshot.json"
DEFAULT_CATALOG = ".litellm/ollama_catalog_snapshot.json"
DEFAULT_PROVIDER = pms.DEFAULT_PROVIDER_SNAPSHOT
DEFAULT_README = "docs/model-benchmarks/README.md"

REGISTRY_BEGIN = "<!-- MODEL-REGISTRY:BEGIN -->"
REGISTRY_END = "<!-- MODEL-REGISTRY:END -->"
REGISTRY_COLUMNS = ("Tier", "Order", "Provider", "Model", "Price", "Last verdict", "Sunset",
                    "Newer candidate?")
_LEADERBOARDS = (("<!-- LEADERBOARD:BEGIN -->", "<!-- LEADERBOARD:END -->"),
                 (media.MEDIA_LEADERBOARD_BEGIN, media.MEDIA_LEADERBOARD_END))
# Fields copied when a price is re-pinned. Anything else in LiteLLM's entry (batch/priority/cache
# rates, context windows) is not something the registry or the spend estimate reads.
_PRICE_FIELDS = ("input_cost_per_token", "output_cost_per_token", "input_cost_per_image_token",
                 "output_cost_per_image_token", "input_cost_per_character", "deprecation_date",
                 "mode")


# ─────────────────────────────── pure ───────────────────────────────

def provider_label(row: dict) -> str:
    """``ollama-cloud`` for an Ollama deployment, else the LiteLLM provider prefix."""
    return "ollama-cloud" if row.get("is_ollama") else pms.provider_of(row.get("model"))


def _per_million(value: float) -> str:
    return f"${value * 1_000_000:.2f}"


def price_text(row: dict, prices: dict) -> str:
    """The price cell for one deployment, read off the pinned cost map.

    Args:
        row: A ``parse_deployments`` row.
        prices: The snapshot's ``models``.

    Returns:
        ``$in / $out per 1M tokens``, a per-image ceiling, a per-character rate, the Ollama shadow
        reference, or ``unpriced``.
    """
    spec = prices.get(row.get("model")) or prices.get(row.get("bare")) or {}
    if row.get("is_ollama"):
        ref = spec.get("shadow_reference")
        return f"plan-metered (shadow `{ref.split('/', 1)[-1]}`)" if ref else "plan-metered"
    if spec.get("output_cost_per_image_token") is not None:
        per = media.image_render_cost(row["bare"], {row["bare"]: spec}, media.DEFAULT_IMAGE_QUALITY)
        return (f"≤${per:.3f}/image ({media.DEFAULT_IMAGE_QUALITY}, 1024²)" if per is not None
                else "unpriced")
    if spec.get("input_cost_per_character") is not None:
        return f"{_per_million(float(spec['input_cost_per_character']))} per 1M chars"
    cin, cout = spec.get("input_cost_per_token"), spec.get("output_cost_per_token")
    if cin is None:
        return "unpriced"
    if not cout:
        return f"{_per_million(float(cin))} per 1M in"
    text = f"{_per_million(float(cin))} / {_per_million(float(cout))} per 1M in/out"
    # A Perplexity preset also bills each web search it runs, a fee no token rate can express.
    per_call = spec.get("cost_per_request")
    if isinstance(per_call, (int, float)) and per_call > 0:
        text += f" + ${float(per_call):.4f}/call"
    return text


def last_verdicts(readme_text: str) -> dict:
    """``{(tier, model): (date, verdict)}`` - the newest row per pair across both leaderboards."""
    out: dict = {}
    text = str(readme_text or "")
    for begin, end in _LEADERBOARDS:
        if begin not in text or end not in text:
            continue
        inner = text.split(begin, 1)[1].split(end, 1)[0]
        for line in inner.splitlines():
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) < 6 or not cells[0][:4].isdigit():
                continue
            key = (cells[2], cells[3].strip("`"))
            if key not in out or cells[0] > out[key][0]:
                out[key] = (cells[0], cells[-1])
    return out


def sunsets_by_model(provider_snapshot: dict, prices: dict, deployments: list) -> dict:
    """``{(provider, bare): (date, source)}`` - earliest published sunset per configured model."""
    rows = list(provider_snapshot.get("deprecations") or [])
    rows += pms.litellm_deprecations(prices, list(pms.configured_provider_models(deployments)))
    out: dict = {}
    for row in rows:
        key = (row.get("provider"), row.get("model"))
        if not row.get("date"):
            continue
        when, source = str(row["date"]), row.get("source") or ""
        # On a tie the provider's own page wins the label: it is the primary source, the cost map
        # and the curated notes are read off it.
        if (key not in out or when < out[key][0]
                or (when == out[key][0] and source == pms.SOURCE_DEPRECATIONS_PAGE)):
            out[key] = (when, source)
    return out


def newer_candidates(deployments: list, catalog: dict, provider_snapshot: dict) -> dict:
    """``{(provider_label, bare): candidate}`` from the committed catalog and provider snapshot."""
    out: dict = {}
    for up in mhc.plan_family_upgrades(deployments, catalog or {}):
        out[("ollama-cloud", up["current"])] = up["candidate"]
    models = {"openai": (provider_snapshot.get("openai") or {}).get("models") or [],
              "perplexity": (provider_snapshot.get("perplexity") or {}).get("models") or []}
    for up in pms.plan_provider_upgrades(deployments, models,
                                         provider_snapshot.get("deprecations") or []):
        out[(up["provider"], up["current"])] = up["candidate"]
    return out


def build_registry(deployments: list, *, prices: dict, catalog: dict, provider_snapshot: dict,
                   readme_text: str) -> list:
    """One registry row per deployment, in config order.

    Args:
        deployments: ``model_health_check.parse_deployments`` rows.
        prices: The pinned cost map's ``models``.
        catalog: The committed Ollama catalog (``load_snapshot_text`` output).
        provider_snapshot: The committed provider snapshot document.
        readme_text: The README holding the leaderboards.

    Returns:
        Row dicts keyed by the lower-cased column names.
    """
    verdicts = last_verdicts(readme_text)
    sunsets = sunsets_by_model(provider_snapshot, prices, deployments)
    newer = newer_candidates(deployments, catalog, provider_snapshot)
    scanned = {"openai": bool((provider_snapshot.get("openai") or {}).get("models")),
               "perplexity": bool((provider_snapshot.get("perplexity") or {}).get("models")),
               "ollama-cloud": bool(catalog)}
    order: dict = {}
    rows = []
    for d in deployments:
        order[d["group"]] = order.get(d["group"], 0) + 1
        provider = provider_label(d)
        # A metered model is measured under its provider-qualified id (#2256), an Ollama tag bare.
        found = [v for v in (verdicts.get((d["group"], d["bare"])),
                             None if d["is_ollama"] else verdicts.get((d["group"], d["model"])))
                 if v]
        verdict = max(found) if found else None
        sunset = sunsets.get((pms.provider_of(d["model"]), d["bare"])) if not d["is_ollama"] else None
        candidate = newer.get((provider, d["bare"]))
        if candidate:
            newer_text = f"yes: `{candidate}`"
        elif pms.is_preset(provider, d["bare"]):
            newer_text = "n/a (preset: Perplexity re-points it)"
        elif provider == "perplexity":
            newer_text = "n/a (unversioned ids)"
        elif not scanned.get(provider):
            newer_text = "not scanned"
        else:
            newer_text = "no"
        rows.append({
            "tier": d["group"], "order": order[d["group"]], "provider": provider,
            "model": d["bare"], "price": price_text(d, prices),
            "last verdict": f"{verdict[1]} · {verdict[0]}" if verdict else "never measured",
            "sunset": (f"**{sunset[0]}**" + (f" ({sunset[1]})" if sunset[1] else "")) if sunset
            else "none published",
            "newer candidate?": newer_text,
        })
    return rows


def sources_note(prices_doc: dict, catalog_updated: Optional[str], provider_snapshot: dict) -> str:
    """The provenance line rendered above the table."""
    pinned = prices_doc.get("_pinned") or {}
    openai = provider_snapshot.get("openai") or {}
    return ("Generated by `scripts/model_registry.py --write` - **do not hand-edit**; rerun it "
            "(the unit suite fails when this block is stale). Prices: "
            f"`.litellm/model_prices_snapshot.json`, LiteLLM cost map pinned "
            f"{pinned.get('fetched') or 'n/a'} ({pinned.get('source') or 'see _comment'}); image "
            "prices are a per-render CEILING at medium quality. Ollama catalog snapshot "
            f"{catalog_updated or 'n/a'}; provider snapshot {provider_snapshot.get('updated') or 'n/a'}"
            f" (OpenAI ids from {openai.get('source') or 'n/a'}). Verdicts are the newest leaderboard "
            "row for that tier and model.")


def render_registry(rows: list, note: str) -> str:
    """The markdown block between the registry markers."""
    lines = [note, "", "| " + " | ".join(REGISTRY_COLUMNS) + " |",
             "|" + "---|" * len(REGISTRY_COLUMNS)]
    for r in rows:
        lines.append(f"| {r['tier']} | {r['order']} | {r['provider']} | `{r['model']}` | "
                     f"{r['price']} | {r['last verdict']} | {r['sunset']} | "
                     f"{r['newer candidate?']} |")
    return "\n".join(lines)


def update_registry_block(text: str, block: str) -> str:
    """Replace the registry block; with no markers yet, insert it before ``## Leaderboard``."""
    body = str(text or "")
    if REGISTRY_BEGIN in body and REGISTRY_END in body:
        before, rest = body.split(REGISTRY_BEGIN, 1)
        after = rest.split(REGISTRY_END, 1)[1]
        return f"{before}{REGISTRY_BEGIN}\n{block}\n{REGISTRY_END}{after}"
    section = f"## Model registry\n\n{REGISTRY_BEGIN}\n{block}\n{REGISTRY_END}\n\n"
    if "## Leaderboard" in body:
        head, tail = body.split("## Leaderboard", 1)
        return f"{head}{section}## Leaderboard{tail}"
    return body.rstrip() + "\n\n" + section


def refresh_price_snapshot(doc: dict, upstream: dict, models: list, *, fetched: str,
                           source: str) -> tuple:
    """Re-pin the given provider models' prices from LiteLLM's cost map.

    Only ``openai/`` and ``perplexity/`` keys are written - Ollama shadow references are LEM's own
    hand-picked mapping and are never touched. A model the upstream map does not carry is reported
    and left as it was.

    Args:
        doc: The snapshot document (``{"_comment", "models", ...}``).
        upstream: LiteLLM's full cost map.
        models: ``[(provider, bare)]`` to pin.
        fetched: ISO date of the fetch.
        source: Where ``upstream`` came from (URL or file).

    Returns:
        ``(new_doc, missing)`` where ``missing`` lists ``provider/bare`` the map lacked.
    """
    out = json.loads(json.dumps(doc or {}))
    table = out.setdefault("models", {})
    missing = []
    pinned = []
    for provider, bare in sorted(set(models)):
        spec = None
        for key in ((bare,) if provider == "openai" else (f"{provider}/{bare}",)):
            if isinstance((upstream or {}).get(key), dict):
                spec = upstream[key]
                break
        if spec is None:
            missing.append(f"{provider}/{bare}")
            continue
        entry = {field: spec.get(field) for field in _PRICE_FIELDS
                 if field in spec or field == "deprecation_date"}
        table[f"{provider}/{bare}"] = entry
        pinned.append(f"{provider}/{bare}")
    out["models"] = dict(sorted(table.items()))
    previous = (doc or {}).get("_pinned") or {}
    out["_pinned"] = {"source": source, "fetched": fetched,
                      "models": sorted(set(previous.get("models") or []) | set(pinned))}
    return out, missing


def models_to_pin(deployments: list, provider_snapshot: dict) -> list:
    """Configured OpenAI/Perplexity models plus the newest family candidate of each."""
    wanted = set(pms.configured_provider_models(deployments))
    models = {"openai": (provider_snapshot.get("openai") or {}).get("models") or []}
    for up in pms.plan_provider_upgrades(deployments, models,
                                         provider_snapshot.get("deprecations") or []):
        wanted.add((up["provider"], up["candidate"]))
    return sorted(wanted)


# ─────────────────────────────── I/O ───────────────────────────────

def _read(path: str) -> str:
    with open(path) as fh:
        return fh.read()


def _read_json(path: str) -> dict:
    return json.loads(_read(path)) if os.path.exists(path) else {}


def load_inputs(config: str, prices: str, catalog: str, provider: str, readme: str) -> dict:
    """Read every committed input the registry is a function of."""
    catalog_doc = _read_json(catalog)
    return {"deployments": mhc.parse_deployments(mhc.load_config_text(_read(config))),
            "prices_doc": _read_json(prices),
            "catalog": mhc.load_snapshot_text(_read(catalog)) if os.path.exists(catalog) else {},
            "catalog_updated": catalog_doc.get("updated"),
            "provider": pms.load_provider_snapshot(_read(provider) if os.path.exists(provider)
                                                   else None),
            "readme": _read(readme) if os.path.exists(readme) else ""}


def generate(inputs: dict) -> str:
    """The registry block for already-loaded inputs."""
    rows = build_registry(inputs["deployments"], prices=inputs["prices_doc"].get("models") or {},
                          catalog=inputs["catalog"], provider_snapshot=inputs["provider"],
                          readme_text=inputs["readme"])
    return render_registry(rows, sources_note(inputs["prices_doc"], inputs["catalog_updated"],
                                              inputs["provider"]))


def regenerate_readme(readme: str, *, config: str = DEFAULT_CONFIG, prices: str = DEFAULT_PRICES,
                      catalog: str = DEFAULT_CATALOG, provider: str = DEFAULT_PROVIDER) -> bool:
    """Rewrite the registry block in ``readme``; True when the file changed."""
    inputs = load_inputs(config, prices, catalog, provider, readme)
    updated = update_registry_block(inputs["readme"], generate(inputs))
    if updated == inputs["readme"]:
        return False
    with open(readme, "w") as fh:
        fh.write(updated)
    return True


def main(argv: Optional[list] = None) -> int:
    """CLI entry point; see the module docstring."""
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--print", dest="print_only", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--refresh-prices", action="store_true")
    ap.add_argument("--upstream-file", default=None)
    ap.add_argument("--today", default=None)
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--prices", default=DEFAULT_PRICES)
    ap.add_argument("--catalog", default=DEFAULT_CATALOG)
    ap.add_argument("--provider-snapshot", default=DEFAULT_PROVIDER)
    ap.add_argument("--readme", default=DEFAULT_README)
    ap.add_argument("--max-spend-usd", type=float, default=None)
    ap.add_argument("--image-quality", default=media.DEFAULT_IMAGE_QUALITY,
                    choices=sorted(media.IMAGE_OUTPUT_TOKENS_CEILING))
    args = ap.parse_args(argv)

    if args.refresh_prices:
        inputs = load_inputs(args.config, args.prices, args.catalog, args.provider_snapshot,
                             args.readme)
        if args.upstream_file:
            upstream, source = json.loads(_read(args.upstream_file)), args.upstream_file
        else:
            upstream, source = pms._get_json(pms.LITELLM_MAP_URL), pms.LITELLM_MAP_URL
            if upstream is None:
                print("could not fetch the LiteLLM cost map - snapshot unchanged", file=sys.stderr)
                return 1
        doc, missing = refresh_price_snapshot(
            inputs["prices_doc"], upstream, models_to_pin(inputs["deployments"], inputs["provider"]),
            fetched=args.today or mhc._today(), source=source)
        with open(args.prices, "w") as fh:
            fh.write(json.dumps(doc, indent=2) + "\n")
        for name in missing:
            print(f"  ! {name} is not in the LiteLLM cost map - left unpriced", file=sys.stderr)
        print(f"prices pinned -> {args.prices}", file=sys.stderr)
        if not (args.write or args.check or args.print_only or args.dry_run):
            return 0

    inputs = load_inputs(args.config, args.prices, args.catalog, args.provider_snapshot, args.readme)
    block = generate(inputs)
    if args.check:
        current = inputs["readme"]
        if update_registry_block(current, block) != current:
            print("model registry is stale - run: poetry run python scripts/model_registry.py "
                  "--write", file=sys.stderr)
            return 1
        print("model registry is current")
        return 0
    if args.write:
        changed = regenerate_readme(args.readme, config=args.config, prices=args.prices,
                                    catalog=args.catalog, provider=args.provider_snapshot)
        print(f"registry {'updated' if changed else 'unchanged'} -> {args.readme}")
        return 0
    print(block)
    if args.dry_run:
        cap = args.max_spend_usd if args.max_spend_usd is not None else media.max_spend_usd()
        roster = media.resolve_roster(inputs["deployments"], list(media.MEDIA_TIERS),
                                      provider_snapshot=inputs["provider"])
        judge = roster["lem-vision"]["champion"]
        plan = media.plan_spend(roster, inputs["prices_doc"].get("models") or {},
                                judge_model=judge, quality=args.image_quality)
        print()
        print(media.render_spend_plan(plan, cap))
        print("\n(dry run: no provider was called and nothing was written)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
