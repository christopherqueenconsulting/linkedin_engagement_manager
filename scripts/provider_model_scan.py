#!/usr/bin/env python3
"""Provider model scan beyond Ollama Cloud: OpenAI and Perplexity (issue #2251).

`model_health_check.py` (#716) watches Ollama Cloud's catalog and retirement schedule. Nothing
watched the OTHER two providers the LiteLLM proxy routes to, so a sunset there was found the day the
calls started failing. Two were already published when this was written and nothing in the repo knew:
`gpt-image-1` (the `lem-image` fallback) shuts down 2026-10-23, and Perplexity's Sonar Chat
Completions (which `lem-research` uses) ended 2026-09-27.

What this module reads, per provider:

* **OpenAI** - the live `/v1/models` list when `OPENAI_API_KEY` is present (authoritative: it is what
  OUR key can call), otherwise the public LiteLLM cost map's `openai` ids (existence only, so it can
  never report a model as gone). Sunsets come from the published deprecations page
  (developers.openai.com/api/docs/deprecations) plus the cost map's own `deprecation_date`.
* **Perplexity** - `/v1/models` (Agent API models, OpenAI-compatible). Perplexity publishes no
  machine-readable deprecation feed, so its sunsets are the curated `PERPLEXITY_NOTICES` below, each
  with the URL it was read from. Sonar ids carry no version number, so there is no family-upgrade
  scan for them.

What it reports - same posture as #716, alerts plus `agent:ready` issues, NEVER a swap:

* a configured model with a published sunset date (``sunsets``);
* a configured model that trails a newer model in its OWN family (``upgrades``) - gpt-4o -> gpt-4.1 /
  gpt-5-class, gpt-4o-mini -> a newer mini, gpt-image-2 -> gpt-image-2.5-*;
* a configured model an AUTHORITATIVE provider list no longer carries (``vanished``).

The scan result is committed as `.litellm/provider_models_snapshot.json`, which is what the
generated model registry (`scripts/model_registry.py`) reads - so the registry is a function of
committed files and never of whatever the network said this morning.

PURE logic over a thin I/O layer, like the other ops scripts; the tests mock every fetch.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import date
from typing import Callable, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

OPENAI_MODELS_URL = "https://api.openai.com/v1/models"
OPENAI_DEPRECATIONS_URL = "https://developers.openai.com/api/docs/deprecations"
PERPLEXITY_MODELS_URL = "https://api.perplexity.ai/v1/models"
LITELLM_MAP_URL = ("https://raw.githubusercontent.com/BerriAI/litellm/main/"
                   "model_prices_and_context_window.json")
DEFAULT_PROVIDER_SNAPSHOT = ".litellm/provider_models_snapshot.json"

SOURCE_OPENAI_API = "openai /v1/models"
SOURCE_LITELLM_MAP = "litellm cost map"
SOURCE_PERPLEXITY_API = "perplexity /v1/models"
SOURCE_DEPRECATIONS_PAGE = "openai deprecations page"
SOURCE_CURATED = "curated"

SUNSET_TITLE_PREFIX = "Provider model sunset scheduled for a live tier:"
UPGRADE_TITLE_PREFIX = "Newer provider model in the family of a live tier:"
VANISHED_TITLE_PREFIX = "Configured provider model no longer listed:"
PROVIDER_ISSUE_KINDS = (("provider-sunset", SUNSET_TITLE_PREFIX),
                        ("provider-upgrade", UPGRADE_TITLE_PREFIX),
                        ("provider-vanished", VANISHED_TITLE_PREFIX))

# A sunset this close (or already past) also pages a human; further out it is an issue only.
SUNSET_ALERT_DAYS = 45

# Perplexity has no deprecation API. Each entry is read off a page by a human and carries its URL,
# so the next reader can re-check it rather than trust it. Keyed by the bare model id LiteLLM uses.
PERPLEXITY_NOTICES = (
    {"provider": "perplexity", "model": "sonar", "date": "2026-09-27",
     "replacement": ["Agent API (/v1/responses) `fast` preset"],
     "detail": ("Sonar Chat Completions support ended on September 27, 2026. Synchronous requests "
                "keep working while they are reformulated as Agent API requests, rolling out "
                "gradually by model; async Sonar requests are no longer supported."),
     "source": "https://docs.perplexity.ai/docs/sonar/models"},
    {"provider": "perplexity", "model": "sonar-pro", "date": "2026-09-27",
     "replacement": ["Agent API (/v1/responses)"],
     "detail": "Same Sonar Chat Completions sunset as `sonar`.",
     "source": "https://docs.perplexity.ai/docs/sonar/models"},
    {"provider": "perplexity", "model": "sonar-reasoning", "date": "2025-12-15",
     "replacement": ["sonar-reasoning-pro"],
     "detail": "Deprecated and removed from the API on December 15, 2025.",
     "source": "https://docs.perplexity.ai/docs/resources/changelog"},
)

# ───────────────────────────── OpenAI model families (pure) ─────────────────────────────

_DATED_RE = re.compile(r"-(?:\d{4}-\d{2}-\d{2}|\d{4})$")
_CHAT_RE = re.compile(r"^gpt-(\d+)(?:\.(\d+))?(o?)(?:-([a-z]+))?$")
_IMAGE_RE = re.compile(r"^gpt-image-(\d+)(?:\.(\d+))?(?:-([a-z]+))?$")
_EMBED_RE = re.compile(r"^text-embedding-(\d+)-(small|large)$")
_TTS_RE = re.compile(r"^tts-(\d+)(?:-(hd))?$")

# Size tiers: a mini is never an upgrade for a full model, nor the other way round.
SIZE_VARIANTS = frozenset({"mini", "nano"})
# Different products that happen to share the `gpt-N` stem: a different price class (`pro`), a
# specialised model (`cyber`, `codex`), or a different modality. Never a drop-in for a chat tier.
EXCLUDED_VARIANTS = frozenset({"pro", "cyber", "codex", "chat", "audio", "realtime", "search",
                               "transcribe", "tts", "instruct", "preview", "latest", "oss"})


def openai_family(model_id: str) -> Optional[dict]:
    """Parse an OpenAI model id into its family, version and variant.

    Args:
        model_id: A bare OpenAI id (``gpt-4o-mini``), with or without an ``openai/`` prefix.

    Returns:
        ``{"family", "version", "variant"}`` or None for an id outside the families compared here:
        a dated snapshot, an excluded variant, or a family with no version number.
    """
    text = str(model_id or "").strip().lower()
    if text.startswith("openai/"):
        text = text.split("/", 1)[1]
    if not text or _DATED_RE.search(text):
        return None
    match = _IMAGE_RE.match(text)
    if match:
        variant = match.group(3) or ""
        if variant in EXCLUDED_VARIANTS:
            return None
        return {"family": "gpt-image",
                "version": (int(match.group(1)), int(match.group(2) or 0)), "variant": variant}
    match = _CHAT_RE.match(text)
    if match:
        variant = match.group(4) or ""
        if variant in EXCLUDED_VARIANTS:
            return None
        # `4o` is the omni successor to `4`, and both precede `4.1`: (4,0,1) sits between them.
        version = (int(match.group(1)), int(match.group(2) or 0), 1 if match.group(3) else 0)
        return {"family": "gpt", "version": version, "variant": variant}
    match = _EMBED_RE.match(text)
    if match:
        return {"family": "text-embedding", "version": (int(match.group(1)),),
                "variant": match.group(2)}
    match = _TTS_RE.match(text)
    if match:
        return {"family": "tts", "version": (int(match.group(1)),), "variant": match.group(2) or ""}
    return None


def is_family_successor(current: dict, candidate: dict) -> bool:
    """Whether ``candidate`` is a newer model in ``current``'s own family.

    Same family, strictly newer version, and the same size variant. A base (unsuffixed) model also
    accepts a codename variant (``gpt-image-2.5-flare``, ``gpt-5.6-sol``), because that is how the
    provider now names its full-size successors; a mini or nano never does.

    Args:
        current: ``openai_family`` of the configured model.
        candidate: ``openai_family`` of a listed model.

    Returns:
        True when the candidate is a successor worth evaluating.
    """
    if not current or not candidate or current["family"] != candidate["family"]:
        return False
    if candidate["version"] <= current["version"]:
        return False
    if candidate["variant"] == current["variant"]:
        return True
    return (current["variant"] == "" and candidate["variant"] not in SIZE_VARIANTS
            and current["family"] in ("gpt", "gpt-image"))


def provider_of(model: str) -> str:
    """The provider prefix of a LiteLLM ``model:`` value (``openai/gpt-4o`` -> ``openai``)."""
    text = str(model or "")
    return text.split("/", 1)[0] if "/" in text else "openai"


def is_preset(provider: str, bare: str) -> bool:
    """Is this a Perplexity Agent API PRESET (``perplexity/preset/fast``) rather than a model id?

    A preset names a configuration (model + tools + budget) that Perplexity re-points as it ships
    improvements (#2255). ``/v1/models`` lists models, never presets, so a preset's absence from
    that list is no evidence it is gone - the live research probe (``scripts/probe_research.py``)
    is what proves a preset still answers.
    """
    return provider == "perplexity" and str(bare or "").startswith("preset/")


def configured_provider_models(deployments: list) -> dict:
    """Non-Ollama deployments grouped as ``{(provider, bare): [tier, ...]}``.

    Args:
        deployments: Rows from ``model_health_check.parse_deployments``.

    Returns:
        Every OpenAI/Perplexity model the proxy serves, with the tiers it serves. An ``openai/``
        prefix on an Ollama Cloud deployment is the OpenAI-compatible transport, not the provider,
        so those rows are excluded.
    """
    out: dict = {}
    for row in deployments or []:
        if row.get("is_ollama"):
            continue
        key = (provider_of(row.get("model")), row.get("bare"))
        groups = out.setdefault(key, [])
        if row.get("group") not in groups:
            groups.append(row.get("group"))
    return out


def plan_provider_upgrades(deployments: list, models_by_provider: dict,
                           deprecations: Optional[list] = None) -> list:
    """Configured OpenAI models that trail a newer model in their own family.

    Args:
        deployments: Rows from ``model_health_check.parse_deployments``.
        models_by_provider: ``{provider: [model ids]}``; a provider mapped to None was unreadable.
        deprecations: Sunset rows; a candidate that itself carries a sunset is never recommended.

    Returns:
        One entry per configured model with at least one successor, newest candidate first.
    """
    sunsetting = {(d.get("provider"), d.get("model")) for d in deprecations or []}
    out: list = []
    for (provider, bare), groups in sorted(configured_provider_models(deployments).items()):
        if provider != "openai":
            continue
        current = openai_family(bare)
        listed = (models_by_provider or {}).get(provider)
        if not current or not listed:
            continue
        candidates = []
        for name in sorted(set(listed)):
            if name == bare or ("openai", name) in sunsetting:
                continue
            parsed = openai_family(name)
            if is_family_successor(current, parsed):
                exact = parsed["variant"] == current["variant"]
                candidates.append((parsed["version"], exact, name))
        if not candidates:
            continue
        candidates.sort(reverse=True)
        out.append({"provider": provider, "current": bare, "groups": sorted(groups),
                    "candidate": candidates[0][2],
                    "candidates": [c[2] for c in candidates][:6]})
    return out


def _days_until(when: str, today: str) -> Optional[int]:
    try:
        return (date.fromisoformat(when) - date.fromisoformat(today)).days
    except (TypeError, ValueError):
        return None


def plan_provider_sunsets(deployments: list, deprecations: list, today: str) -> list:
    """Configured provider models with a published sunset date.

    Args:
        deployments: Rows from ``model_health_check.parse_deployments``.
        deprecations: ``{provider, model, date, replacement, source}`` rows.
        today: ISO date the countdown is measured from.

    Returns:
        One entry per configured model with a sunset (earliest date wins), soonest first.
        ``days_until`` is negative once the date has passed.
    """
    best: dict = {}
    for row in deprecations or []:
        key = (row.get("provider"), row.get("model"))
        if not row.get("date"):
            continue
        if key not in best or str(row["date"]) < str(best[key]["date"]):
            best[key] = dict(row, replacement=list(row.get("replacement") or []))
        elif str(row["date"]) == str(best[key]["date"]):
            # Two sources agreeing on the date: keep whichever names a replacement, and a detail.
            kept = best[key]
            for name in row.get("replacement") or []:
                if name not in kept["replacement"]:
                    kept["replacement"].append(name)
            if row.get("source") == SOURCE_DEPRECATIONS_PAGE:
                kept["source"] = row["source"]
            for field in ("detail", "url"):
                if row.get(field) and not kept.get(field):
                    kept[field] = row[field]
    out = []
    for key, groups in configured_provider_models(deployments).items():
        row = best.get(key)
        if not row:
            continue
        out.append({"provider": key[0], "model": key[1], "groups": sorted(groups),
                    "date": row["date"], "days_until": _days_until(row["date"], today),
                    "replacement": list(row.get("replacement") or []),
                    "detail": row.get("detail") or "", "source": row.get("source") or "",
                    "url": row.get("url") or ""})
    out.sort(key=lambda r: (r["date"], r["provider"], r["model"]))
    return out


def plan_provider_vanished(deployments: list, models_by_provider: dict,
                           authoritative: dict) -> list:
    """Configured models an AUTHORITATIVE provider list no longer carries.

    Only a live API list (what our key can call) is evidence. The LiteLLM cost map is a list of what
    LiteLLM prices, so a model missing from it proves nothing, and an empty or unreadable list is
    no evidence of a vanish either - this finding files an ``agent:ready`` issue.

    Args:
        deployments: Rows from ``model_health_check.parse_deployments``.
        models_by_provider: ``{provider: [ids] or None}``.
        authoritative: ``{provider: bool}`` - whether that list came from the provider's own API.

    Returns:
        ``[{provider, model, groups}]``.
    """
    out = []
    for (provider, bare), groups in sorted(configured_provider_models(deployments).items()):
        listed = (models_by_provider or {}).get(provider)
        if not listed or not (authoritative or {}).get(provider):
            continue
        if is_preset(provider, bare):
            continue
        if bare not in set(listed):
            out.append({"provider": provider, "model": bare, "groups": sorted(groups)})
    return out


# ───────────────────────────── source parsing (pure) ─────────────────────────────

_MONTHS = {m: i for i, m in enumerate(
    ("january", "february", "march", "april", "may", "june", "july", "august", "september",
     "october", "november", "december"), start=1)}
_TEXT_DATE_RE = re.compile(r"^([A-Za-z]+)\.?\s+(\d{1,2}),\s*(\d{4})$")
_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_TR_RE = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.S | re.I)
_TD_RE = re.compile(r"<td\b[^>]*>(.*?)</td>", re.S | re.I)
_CODE_RE = re.compile(r"<code\b[^>]*>(.*?)</code>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_MODEL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]*$")


def parse_date_cell(text: str) -> Optional[str]:
    """``Jan 6, 2027`` / ``October 23, 2026`` / ``2026-05-12`` -> ISO date, else None."""
    cell = " ".join(_TAG_RE.sub(" ", str(text or "")).split())
    iso = _ISO_DATE_RE.match(cell)
    if iso:
        return cell
    match = _TEXT_DATE_RE.match(cell)
    if not match:
        return None
    word = match.group(1).lower()
    month = next((n for name, n in _MONTHS.items() if name.startswith(word[:3])), None)
    if not month:
        return None
    try:
        return date(int(match.group(3)), month, int(match.group(2))).isoformat()
    except ValueError:
        return None


def _code_ids(cell: str) -> list:
    ids = []
    for raw in _CODE_RE.findall(cell or ""):
        text = " ".join(_TAG_RE.sub("", raw).split()).strip().lower()
        if _MODEL_ID_RE.match(text):
            ids.append(text)
    return ids


def parse_openai_deprecations(html: str) -> list:
    """Sunset rows off OpenAI's deprecations page.

    Every table on the page is ``Shutdown date | model(s) | replacement(s)``; a model cell can name
    several ids (``gpt-4.1-nano | gpt-4.1-nano-2025-04-14``). Only ``<code>`` tokens are read as
    ids, so prose like "New fine-tuning training on" is never mistaken for a model.

    Args:
        html: The page's HTML.

    Returns:
        ``[{provider, model, date, replacement, source}]`` - one row per model id.
    """
    out = []
    for row in _TR_RE.findall(str(html or "")):
        cells = _TD_RE.findall(row)
        if len(cells) < 3:
            continue
        when = parse_date_cell(cells[0])
        if not when:
            continue
        replacement = _code_ids(cells[-1])
        for cell in cells[1:-1]:
            for model in _code_ids(cell):
                out.append({"provider": "openai", "model": model, "date": when,
                            "replacement": replacement, "source": SOURCE_DEPRECATIONS_PAGE})
    return out


def litellm_openai_models(cost_map: dict) -> list:
    """OpenAI ids the LiteLLM cost map prices (chat/image/embedding/speech, undated, unprefixed)."""
    out = set()
    for key, spec in (cost_map or {}).items():
        if not isinstance(spec, dict) or "/" in key or spec.get("litellm_provider") != "openai":
            continue
        if spec.get("mode") not in ("chat", "image_generation", "embedding", "audio_speech",
                                    "responses"):
            continue
        if _DATED_RE.search(key):
            continue
        out.add(key)
    return sorted(out)


def litellm_deprecations(cost_map: dict, models: list) -> list:
    """``deprecation_date`` rows from the cost map for the given provider-prefixed models.

    Args:
        cost_map: LiteLLM's ``model_prices_and_context_window.json`` (or the pinned snapshot's
            ``models``).
        models: ``[(provider, bare)]`` to look up.

    Returns:
        ``[{provider, model, date, replacement, source}]`` for every model the map dates.
    """
    out = []
    for provider, bare in models or []:
        for key in (f"{provider}/{bare}", bare):
            spec = (cost_map or {}).get(key)
            if isinstance(spec, dict) and spec.get("deprecation_date"):
                out.append({"provider": provider, "model": bare,
                            "date": str(spec["deprecation_date"]), "replacement": [],
                            "source": SOURCE_LITELLM_MAP})
                break
    return out


def parse_models_payload(payload: Optional[dict]) -> Optional[list]:
    """An OpenAI-compatible ``/v1/models`` payload -> sorted ids, or None if unreadable."""
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return None
    ids = {str(m.get("id")) for m in payload["data"] if isinstance(m, dict) and m.get("id")}
    return sorted(ids)


# ───────────────────────────── plan + snapshot (pure) ─────────────────────────────

def build_provider_plan(deployments: list, *, today: str, openai_models: Optional[list],
                        openai_source: Optional[str], perplexity_models: Optional[list],
                        deprecations: list, previous: Optional[dict] = None) -> dict:
    """The whole provider scan as data: findings, issue specs and the snapshot to commit.

    Args:
        deployments: Rows from ``model_health_check.parse_deployments``.
        today: ISO date of the scan.
        openai_models: OpenAI ids read this run, or None when no source answered.
        openai_source: Which source produced them (``SOURCE_OPENAI_API`` / ``SOURCE_LITELLM_MAP``).
        perplexity_models: Perplexity ids, or None when unreadable.
        deprecations: Every sunset row gathered this run (page, cost map, curated).
        previous: The committed snapshot; a provider that could not be read this run keeps its
            previous section rather than being blanked.

    Returns:
        ``{today, sources, sunsets, upgrades, vanished, snapshot, snapshot_changed, issues, alert}``.
    """
    previous = previous or {}
    prev_openai = previous.get("openai") or {}
    prev_pplx = previous.get("perplexity") or {}
    openai_section = ({"source": openai_source, "models": list(openai_models)}
                      if openai_models else
                      {"source": prev_openai.get("source"), "models": prev_openai.get("models") or []})
    pplx_section = ({"source": SOURCE_PERPLEXITY_API, "models": list(perplexity_models)}
                    if perplexity_models else
                    {"source": prev_pplx.get("source"), "models": prev_pplx.get("models") or []})
    # Deprecations are the union of THIS run's sources; when the page could not be read, the
    # previous run's page rows carry forward so a sunset never silently drops off the registry.
    page_rows = [d for d in deprecations if d.get("source") == SOURCE_DEPRECATIONS_PAGE]
    if not page_rows:
        page_rows = [d for d in previous.get("deprecations") or []
                     if d.get("source") == SOURCE_DEPRECATIONS_PAGE]
    merged = _dedupe_deprecations(
        page_rows + [d for d in deprecations if d.get("source") != SOURCE_DEPRECATIONS_PAGE])
    models_by_provider = {"openai": openai_section["models"], "perplexity": pplx_section["models"]}
    authoritative = {"openai": openai_section["source"] == SOURCE_OPENAI_API and bool(openai_models),
                     "perplexity": bool(perplexity_models)}
    sunsets = plan_provider_sunsets(deployments, merged, today)
    upgrades = plan_provider_upgrades(deployments, models_by_provider, merged)
    vanished = plan_provider_vanished(deployments, models_by_provider, authoritative)
    snapshot = {"openai": openai_section, "perplexity": pplx_section,
                "deprecations": _relevant_deprecations(merged, models_by_provider, deployments)}
    comparable_prev = {k: previous.get(k) for k in ("openai", "perplexity", "deprecations")}
    plan = {
        "today": today,
        "sources": {"openai": openai_source if openai_models else None,
                    "perplexity": SOURCE_PERPLEXITY_API if perplexity_models else None,
                    "deprecations_page": any(d.get("source") == SOURCE_DEPRECATIONS_PAGE
                                             for d in deprecations)},
        "sunsets": sunsets, "upgrades": upgrades, "vanished": vanished,
        "snapshot": snapshot, "snapshot_changed": snapshot != comparable_prev,
        "alert": [s for s in sunsets
                  if s["days_until"] is not None and s["days_until"] <= SUNSET_ALERT_DAYS]
                 + [dict(v, kind="vanished") for v in vanished],
    }
    plan["issues"] = build_issue_specs(plan)
    plan["alert_text"] = alert_text(plan)
    return plan


def _dedupe_deprecations(rows: list) -> list:
    seen = set()
    out = []
    for row in rows:
        key = (row.get("provider"), row.get("model"), row.get("date"), row.get("source"))
        if key in seen:
            continue
        seen.add(key)
        out.append({"provider": row.get("provider"), "model": row.get("model"),
                    "date": row.get("date"), "replacement": list(row.get("replacement") or []),
                    "source": row.get("source"), **({"detail": row["detail"]}
                                                    if row.get("detail") else {}),
                    **({"url": row["url"]} if row.get("url") else {})})
    out.sort(key=lambda r: (str(r["provider"]), str(r["model"]), str(r["date"]), str(r["source"])))
    return out


def _relevant_deprecations(rows: list, models_by_provider: dict, deployments: list) -> list:
    """Sunset rows worth committing: anything about a model a provider lists or we configure."""
    keep = {(p, m) for p, ids in (models_by_provider or {}).items() for m in (ids or [])}
    keep |= set(configured_provider_models(deployments))
    return [r for r in rows if (r["provider"], r["model"]) in keep]


def render_provider_snapshot(snapshot: dict, updated: str) -> str:
    """The committed snapshot file's text (stable key order, trailing newline)."""
    doc = {"_comment": ("Written by scripts/model_health_check.py --provider-scan --provider-apply "
                        "(issue #2251). Read by scripts/model_registry.py. Do not hand-edit: "
                        "curated Perplexity notices live in scripts/provider_model_scan.py."),
           "updated": updated, **snapshot}
    return json.dumps(doc, indent=2, sort_keys=False) + "\n"


def load_provider_snapshot(text: Optional[str]) -> dict:
    """Parse the committed snapshot; a missing or unreadable file is an empty baseline."""
    try:
        doc = json.loads(text) if text else {}
    except ValueError:
        return {}
    return doc if isinstance(doc, dict) else {}


# ───────────────────────────── issue rendering (pure) ─────────────────────────────

def sunset_marker(row: dict) -> str:
    """Dedup marker for one sunset finding; a moved date is a new marker."""
    return f"{row['provider']}/{row['model']} sunset {row['date']}"


def upgrade_marker(row: dict) -> str:
    """Dedup marker for one family-upgrade finding; a newer candidate is a new marker."""
    return f"{row['provider']}/{row['current']} -> {row['candidate']}"


def vanished_marker(row: dict) -> str:
    """Dedup marker for one unlisted-model finding."""
    return f"{row['provider']}/{row['model']} unlisted"


def _titled(prefix: str, items: list) -> str:
    from model_health_check import _titled as titled  # noqa: WPS433 - one title rule for both scans
    return titled(prefix, items)


_FOOTER_SCOPE = [
    "## Scope",
    ("- Decide per model: keep, swap, or remove. A swap is a deliberate `.litellm/config.yaml` "
     "change - never an entry in `.litellm/model_upgrades.yaml`, which is the Ollama RETIREMENT "
     "map and auto-swaps whatever lands in it."),
    ("- For `lem-vision` / `lem-image`, measure the candidate first: "
     "`poetry run python scripts/benchmark_models.py --run --tiers lem-vision,lem-image` "
     "(spend-capped, see docs/model-benchmarks/README.md)."),
    "- Regenerate the registry in the same PR: `poetry run python scripts/model_registry.py --write`.",
    "",
    "## Acceptance",
    "- A decision per model above (adopt, decline or remove, with the reason).",
    "- `poetry run pytest tests/unit -q` passes (the registry freshness test included).",
    "",
]


def build_sunset_body(rows: list, today: str) -> str:
    """Issue body for configured models with a published sunset."""
    lines = ["## Context",
             f"The weekly provider scan ({today}) found published sunset dates for models a live "
             "LiteLLM tier still serves. Nothing is swapped automatically.", ""]
    for r in rows:
        when = (f"in {r['days_until']} days" if (r["days_until"] or 0) >= 0
                else f"{-r['days_until']} days AGO")
        repl = ", ".join(f"`{x}`" for x in r["replacement"]) or "none published"
        lines.append(f"- **{r['provider']}/{r['model']}** - sunset **{r['date']}** ({when}); tiers: "
                     f"{', '.join(r['groups'])}; recommended replacement: {repl}; source: "
                     f"{r['source'] or 'n/a'}"
                     + (f" ({r['url']})" if r.get("url") else ""))
        if r.get("detail"):
            lines.append(f"  - {r['detail']}")
    lines += [""] + _FOOTER_SCOPE + [
        "Auto-filed by `scripts/model_health_check.py --provider-scan --file-provider-issues` "
        "(issue #2251). Dedup markers (do not remove): "
        + ", ".join(f"`{sunset_marker(r)}`" for r in rows)]
    return "\n".join(lines)


def build_upgrade_body(rows: list, today: str) -> str:
    """Issue body for configured models trailing a newer family member."""
    lines = ["## Context",
             f"The weekly provider scan ({today}) found configured models that trail a newer model "
             "in their own family. Newer is not automatically better *for us* - a newer model can "
             "cost more per token or behave differently on our prompts - so this is an evaluation, "
             "never an auto-swap.", ""]
    for r in rows:
        others = ", ".join(f"`{c}`" for c in r["candidates"][1:])
        lines.append(f"- **{upgrade_marker(r)}** - tiers: {', '.join(r['groups'])}"
                     + (f"; also in the family: {others}" if others else ""))
    lines += [""] + _FOOTER_SCOPE + [
        "Auto-filed by `scripts/model_health_check.py --provider-scan --file-provider-issues` "
        "(issue #2251). Dedup markers (do not remove): "
        + ", ".join(f"`{upgrade_marker(r)}`" for r in rows)]
    return "\n".join(lines)


def build_vanished_body(rows: list, today: str) -> str:
    """Issue body for configured models the provider's own API no longer lists."""
    lines = ["## Context",
             f"The weekly provider scan ({today}) read each provider's own `/v1/models` list and "
             "did not find these configured models on it. Under latency-based routing a deployment "
             "that 404s answers fastest and takes the traffic, so this is worth a look before it "
             "becomes an outage.", ""]
    for r in rows:
        lines.append(f"- **{r['provider']}/{r['model']}** - tiers: {', '.join(r['groups'])}")
    lines += [""] + _FOOTER_SCOPE + [
        "Auto-filed by `scripts/model_health_check.py --provider-scan --file-provider-issues` "
        "(issue #2251). Dedup markers (do not remove): "
        + ", ".join(f"`{vanished_marker(r)}`" for r in rows)]
    return "\n".join(lines)


def build_issue_specs(plan: dict) -> dict:
    """``{kind: {markers, title, body}}`` for the three provider issue kinds."""
    today = plan["today"]
    sunsets, upgrades, vanished = plan["sunsets"], plan["upgrades"], plan["vanished"]
    return {
        "provider-sunset": {
            "markers": [sunset_marker(r) for r in sunsets],
            "title": _titled(SUNSET_TITLE_PREFIX, [f"{r['model']} {r['date']}" for r in sunsets])
            if sunsets else "",
            "body": build_sunset_body(sunsets, today) if sunsets else ""},
        "provider-upgrade": {
            "markers": [upgrade_marker(r) for r in upgrades],
            "title": _titled(UPGRADE_TITLE_PREFIX,
                             [f"{r['current']} -> {r['candidate']}" for r in upgrades])
            if upgrades else "",
            "body": build_upgrade_body(upgrades, today) if upgrades else ""},
        "provider-vanished": {
            "markers": [vanished_marker(r) for r in vanished],
            "title": _titled(VANISHED_TITLE_PREFIX, [r["model"] for r in vanished])
            if vanished else "",
            "body": build_vanished_body(vanished, today) if vanished else ""},
    }


def alert_text(plan: dict) -> str:
    """One line per finding that should page a human (empty when there is none)."""
    lines = []
    for row in plan.get("alert") or []:
        if row.get("kind") == "vanished":
            lines.append(f"{row['provider']}/{row['model']}: no longer listed by the provider "
                         f"(tiers: {', '.join(row['groups'])})")
        else:
            when = (f"in {row['days_until']} days" if row["days_until"] >= 0
                    else f"PASSED {-row['days_until']} days ago")
            lines.append(f"{row['provider']}/{row['model']}: sunset {row['date']} ({when}) "
                         f"- tiers: {', '.join(row['groups'])}")
    return "\n".join(lines)


def print_plan(plan: dict, out: Callable[[str], None]) -> None:
    """Human-readable summary of a provider plan."""
    src = plan["sources"]
    out(f"Provider scan ({plan['today']}): openai={src['openai'] or 'UNAVAILABLE'} "
        f"perplexity={src['perplexity'] or 'UNAVAILABLE'} "
        f"deprecations-page={'ok' if src['deprecations_page'] else 'UNAVAILABLE'}")
    for r in plan["sunsets"]:
        out(f"  SUNSET [{', '.join(r['groups'])}] {r['provider']}/{r['model']} on {r['date']} "
            f"({r['days_until']}d) -> {', '.join(r['replacement']) or 'no replacement published'}")
    for r in plan["upgrades"]:
        out(f"  NEWER [{', '.join(r['groups'])}] {upgrade_marker(r)}")
    for r in plan["vanished"]:
        out(f"  UNLISTED [{', '.join(r['groups'])}] {r['provider']}/{r['model']}")
    if not (plan["sunsets"] or plan["upgrades"] or plan["vanished"]):
        out("  nothing to report")


# ─────────────────────────────── I/O (mocked in tests) ───────────────────────────

def _get_json(url: str, api_key: str = "") -> Optional[dict]:
    from model_health_check import http_get  # noqa: WPS433 - the one stdlib GET for ops scripts
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        return json.loads(http_get(url, headers))
    except Exception as exc:  # noqa: BLE001 - an unreadable source degrades to "not scanned"
        print(f"  ! could not read {url}: {str(exc)[:160]}", file=sys.stderr)
        return None


def _get_text(url: str) -> Optional[str]:
    from model_health_check import http_get  # noqa: WPS433
    try:
        return http_get(url, {"User-Agent": "Mozilla/5.0 (LEM provider scan)"})
    except Exception as exc:  # noqa: BLE001
        print(f"  ! could not read {url}: {str(exc)[:160]}", file=sys.stderr)
        return None


def gather_sources(fixture: Optional[str] = None) -> dict:
    """Read every provider source once; each that fails is None, never an exception.

    Args:
        fixture: A directory holding ``openai_models.json``, ``deprecations.html``,
            ``perplexity_models.json`` and ``litellm_map.json`` to read instead of the network.

    Returns:
        ``{openai_payload, litellm_map, deprecations_html, perplexity_payload}``.
    """
    if fixture:
        def read(name: str, as_json: bool = True):
            path = os.path.join(fixture, name)
            if not os.path.exists(path):
                return None
            with open(path) as fh:
                return json.load(fh) if as_json else fh.read()
        return {"openai_payload": read("openai_models.json"),
                "litellm_map": read("litellm_map.json"),
                "deprecations_html": read("deprecations.html", as_json=False),
                "perplexity_payload": read("perplexity_models.json")}
    openai_key = os.environ.get("OPENAI_API_KEY", "")
    return {
        "openai_payload": _get_json(OPENAI_MODELS_URL, openai_key) if openai_key else None,
        "litellm_map": _get_json(LITELLM_MAP_URL),
        "deprecations_html": _get_text(OPENAI_DEPRECATIONS_URL),
        "perplexity_payload": _get_json(PERPLEXITY_MODELS_URL,
                                        os.environ.get("PERPLEXITY_API_KEY", "")),
    }


def scan(deployments: list, sources: dict, *, today: str, previous: Optional[dict] = None) -> dict:
    """Turn gathered sources into a provider plan (pure apart from what ``sources`` already holds).

    Args:
        deployments: Rows from ``model_health_check.parse_deployments``.
        sources: Output of ``gather_sources``.
        today: ISO date of the scan.
        previous: The committed snapshot.

    Returns:
        The plan from ``build_provider_plan``.
    """
    openai_models = parse_models_payload(sources.get("openai_payload"))
    openai_source = SOURCE_OPENAI_API if openai_models else None
    if not openai_models and sources.get("litellm_map"):
        openai_models = litellm_openai_models(sources["litellm_map"]) or None
        openai_source = SOURCE_LITELLM_MAP if openai_models else None
    deprecations = parse_openai_deprecations(sources.get("deprecations_html") or "")
    configured = list(configured_provider_models(deployments))
    deprecations += litellm_deprecations(sources.get("litellm_map") or {}, configured)
    deprecations += [dict(n, url=n["source"], source=SOURCE_CURATED) for n in PERPLEXITY_NOTICES]
    return build_provider_plan(
        deployments, today=today, openai_models=openai_models, openai_source=openai_source,
        perplexity_models=parse_models_payload(sources.get("perplexity_payload")),
        deprecations=deprecations, previous=previous)
