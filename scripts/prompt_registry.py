#!/usr/bin/env python3
"""Prompt inventory: every LLM call site in LEM, its registry entry, and its version.

The ONE place the prompt-eval workflow learns which prompts exist (docs/prompt-evals.md). Three
committed files under tests/benchmarks/prompts/ are read here and nowhere else:

* ``registry.yaml`` — one entry per call site: a stable prompt id, its family, and a status
  (``evaluated`` / ``planned`` / ``exempt:<reason>``). Hand-edited.
* ``prompts.lock.json`` — the version and content hash of every captured prompt. Written by
  ``scripts/prompt_capture.py --write``; a hash change without a version bump fails the unit lane.
* ``eval_state.json`` — what has been measured, per prompt@version × model. Written only by the
  scheduled eval workflow's results PR.

A call site is found by an AST scan, never a hand-kept list, so a new LLM call without a registry
entry fails ``tests/unit/benchmarks/test_prompt_registry.py``. It is keyed on
``(module, enclosing function, call index)`` rather than the function alone: two calls in one
function are two prompts (``image_gen._staged_inspect``), and a prompt built in one function and
sent through a shared wrapper (``ai_helper._call_llm``) is attributed to the CALLER — the wrapper
names are listed in ``WRAPPERS`` and the wrapper's own transport call is exempt.

Pure: reads files, imports nothing from ``cqc_lem``, opens no network.

Usage:
    python scripts/prompt_registry.py --scan          # print every call site found
    python scripts/prompt_registry.py --check         # registry vs scan (exit 1 on drift)
    python scripts/prompt_registry.py --inventory     # rewrite docs/prompt-evals/inventory.md
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import pathlib
import sys
from dataclasses import dataclass, field
from typing import Any

import yaml

REPO = pathlib.Path(__file__).resolve().parent.parent
PROMPTS_DIR = REPO / "tests" / "benchmarks" / "prompts"
REGISTRY_PATH = PROMPTS_DIR / "registry.yaml"
LOCK_PATH = PROMPTS_DIR / "prompts.lock.json"
STATE_PATH = PROMPTS_DIR / "eval_state.json"
INVENTORY_PATH = REPO / "docs" / "prompt-evals" / "inventory.md"

#: Trees scanned for call sites. ``scripts/`` is included because operator CLIs carry prompts too
#: (``triage_issues.py``, the benchmark judges), and an unscanned tree is an untracked prompt.
SCAN_ROOTS: tuple[str, ...] = ("src/cqc_lem", "scripts")

#: SDK endpoints that send a prompt. Embeddings and TTS are deliberately absent: their input is
#: content to encode or speak, not an instruction a model follows, so there is nothing to grade.
ENDPOINT_SUFFIXES: tuple[str, ...] = (
    "chat.completions.create",
    "responses.create",
    "images.generate",
    "images.edit",
)

#: In-repo helpers that forward a caller-built prompt to an endpoint, mapped to the modules the name
#: is a wrapper in (``None`` = every module, for a helper imported by name across the tree). A call
#: to one is a call site; the endpoint call inside the helper itself is registered ``exempt:wrapper``.
WRAPPERS: dict[str, frozenset[str] | None] = {
    "_call_llm": None,
    "_complete": frozenset({"cqc_lem.utilities.ai.curated_commentary"}),
}

STATUSES = ("evaluated", "planned")
EXEMPT_PREFIX = "exempt:"


@dataclass(frozen=True, order=True)
class CallSite:
    """One place in the code that sends a prompt to a model."""

    module: str
    function: str
    index: int
    line: int = field(compare=False)
    kind: str = field(compare=False)

    @property
    def key(self) -> str:
        """The registry key: ``module:function#index``."""
        return f"{self.module}:{self.function}#{self.index}"


def _dotted(node: ast.AST) -> str:
    """Return the dotted text of an attribute chain (``a.b.c``), or ``""`` when it is not one."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    elif isinstance(node, ast.Call):
        parts.append("()")
    else:
        return ".".join(reversed(parts))
    return ".".join(reversed(parts))


def _call_kind(call: ast.Call, module: str) -> str | None:
    """Classify a call in ``module`` as an endpoint, a wrapper call, or neither (``None``)."""
    name = _dotted(call.func)
    for suffix in ENDPOINT_SUFFIXES:
        if name == suffix or name.endswith("." + suffix):
            return suffix
    leaf = name.rsplit(".", 1)[-1]
    if leaf in WRAPPERS:
        scope = WRAPPERS[leaf]
        if scope is None or module in scope:
            return leaf
    return None


class _SiteVisitor(ast.NodeVisitor):
    """Collect call sites with their enclosing qualified function name."""

    def __init__(self, module: str) -> None:
        self.module = module
        self.stack: list[str] = []
        self.counts: dict[str, int] = {}
        self.sites: list[CallSite] = []

    def _scoped(self, node: ast.AST, name: str) -> None:
        self.stack.append(name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802 - ast API
        """Enter a function scope."""
        self._scoped(node, node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802 - ast API
        """Enter an async function scope."""
        self._scoped(node, node.name)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802 - ast API
        """Enter a class scope."""
        self._scoped(node, node.name)

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802 - ast API
        """Record the call when it sends a prompt, then keep walking its arguments."""
        kind = _call_kind(node, self.module)
        if kind is not None:
            function = ".".join(self.stack) or "<module>"
            index = self.counts.get(function, 0)
            self.counts[function] = index + 1
            self.sites.append(CallSite(self.module, function, index, node.lineno, kind))
        self.generic_visit(node)


def module_name(path: pathlib.Path, repo: pathlib.Path = REPO) -> str:
    """Return the dotted module name a scanned file is registered under."""
    rel = path.resolve().relative_to(repo)
    parts = list(rel.with_suffix("").parts)
    if parts[:1] == ["src"]:
        parts = parts[1:]
    return ".".join(parts)


def scan_file(path: pathlib.Path, repo: pathlib.Path = REPO) -> list[CallSite]:
    """Return every call site in one Python file, in source order."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    visitor = _SiteVisitor(module_name(path, repo))
    visitor.visit(tree)
    return visitor.sites


def scan(repo: pathlib.Path = REPO, roots: tuple[str, ...] = SCAN_ROOTS) -> list[CallSite]:
    """Return every call site under the scanned roots, sorted by key."""
    sites: list[CallSite] = []
    for root in roots:
        for path in sorted((repo / root).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            sites.extend(scan_file(path, repo))
    return sorted(sites)


def load_registry(path: pathlib.Path = REGISTRY_PATH) -> dict[str, dict[str, Any]]:
    """Return the registry as ``{call-site key: entry}``, validating every entry's shape.

    Raises:
        ValueError: An entry is missing ``id``/``status``, carries an unknown status, or a prompt
            id is used twice for different call sites without being a declared ``alias``.
    """
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    sites = raw.get("sites") or {}
    seen_ids: dict[str, str] = {}
    for key, entry in sites.items():
        if not isinstance(entry, dict) or not entry.get("id") or not entry.get("status"):
            raise ValueError(f"registry entry {key!r} needs an `id` and a `status`")
        status = str(entry["status"])
        if status not in STATUSES and not (
            status.startswith(EXEMPT_PREFIX) and len(status) > len(EXEMPT_PREFIX)
        ):
            raise ValueError(f"registry entry {key!r} has unknown status {status!r}")
        pid = str(entry["id"])
        if pid in seen_ids and not entry.get("shares_prompt_with"):
            raise ValueError(f"prompt id {pid!r} is used by both {seen_ids[pid]!r} and {key!r}")
        seen_ids.setdefault(pid, key)
    return sites


def drift(sites: list[CallSite], registry: dict[str, dict[str, Any]]) -> tuple[list[str], list[str]]:
    """Return ``(unregistered, stale)`` call-site keys.

    ``unregistered`` are found in the code with no registry entry; ``stale`` are registered but
    no longer in the code (the function was renamed, or a call was added/removed before them).
    """
    found = {s.key for s in sites}
    known = set(registry)
    return sorted(found - known), sorted(known - found)


def _strip_docstrings(tree: ast.AST) -> ast.AST:
    """Drop every docstring in ``tree`` in place, so a doc-only edit never reads as a prompt change."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                node.body = body[1:] or [ast.Pass()]
    return tree


def _canon(node: Any) -> str:
    """Serialise an AST deterministically across Python versions.

    ``ast.dump`` is not stable across interpreters (3.13 stopped printing empty fields), and CI runs
    3.12 while a dev box may run 3.13 — so a hash built on it would differ by machine.
    """
    if isinstance(node, ast.AST):
        fields = ",".join(f"{name}={_canon(getattr(node, name, None))}" for name in node._fields
                          if name != "type_comment")
        return f"{type(node).__name__}({fields})"
    if isinstance(node, list):
        return "[" + ",".join(_canon(item) for item in node) + "]"
    return repr(node)


def _module_constants(tree: ast.Module) -> dict[str, ast.AST]:
    """Return ``{name: value node}`` for every simple module-level assignment."""
    constants: dict[str, ast.AST] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    constants[target.id] = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value:
            constants[node.target.id] = node.value
    return constants


def _scope_node(tree: ast.Module, function: str) -> ast.AST | None:
    """Return the node a prompt's source hash covers: the outermost function holding the call.

    A prompt assembled in ``generate_ai_response`` and sent from its nested ``_draft`` is one prompt,
    so the hash covers the outer function; for a method it is the method, not the whole class.
    """
    parts = function.split(".")
    nodes: list[ast.AST] = list(tree.body)
    for part in parts:
        match = next((n for n in nodes if getattr(n, "name", None) == part), None)
        if match is None:
            return None
        if isinstance(match, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return match
        nodes = list(getattr(match, "body", []))
    return None


def source_hash(site: CallSite, repo: pathlib.Path = REPO) -> str:
    """Return a hash of the code that builds one prompt.

    Covers the enclosing function (docstrings stripped, comments never in the AST) plus every
    module-level constant it names — so editing ``_LLM_SYSTEM`` changes the hash of each prompt that
    reads it. This is the COARSE version signal for a prompt that is not captured yet: a refactor
    that leaves the text unchanged still moves it. A captured prompt is versioned by its rendered
    ``content_hash`` instead (scripts/prompt_capture.py), which moves only when the text does.

    Raises:
        LookupError: The call site's function is no longer in its module.
    """
    rel = site.module.replace(".", "/") + ".py"
    path = repo / ("src/" + rel if site.module.startswith("cqc_lem") else rel)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    node = _scope_node(tree, site.function)
    if node is None:
        raise LookupError(f"{site.key}: function not found in {path}")
    constants = _module_constants(tree)
    names = sorted({n.id for n in ast.walk(node) if isinstance(n, ast.Name) and n.id in constants})
    _strip_docstrings(node)
    blob = _canon(node)
    for name in names:
        blob += f"\n{name}=" + _canon(_strip_docstrings(constants[name]))
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def tracked_prompts(
    sites: list[CallSite], registry: dict[str, dict[str, Any]]
) -> dict[str, tuple[CallSite, dict[str, Any]]]:
    """Return ``{prompt id: (call site, entry)}`` for every prompt that carries a version.

    Exempt entries are not versioned, and a site that only re-sends another site's prompt
    (``shares_prompt_with``) is versioned under that other site.
    """
    out: dict[str, tuple[CallSite, dict[str, Any]]] = {}
    for site in sites:
        entry = registry.get(site.key)
        if not entry or str(entry["status"]).startswith(EXEMPT_PREFIX):
            continue
        if entry.get("shares_prompt_with"):
            continue
        out[str(entry["id"])] = (site, entry)
    return out


def load_json(path: pathlib.Path) -> dict[str, Any]:
    """Return a committed JSON state file, or ``{}`` when it does not exist yet."""
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _latest_verdicts(state: dict[str, Any], prompt_id: str, version: int | None) -> str:
    """Summarise the measured verdicts for one prompt@version as ``model: verdict`` pairs."""
    if version is None:
        return "—"
    per_model = state.get(f"{prompt_id}@{version}") or {}
    if not per_model:
        return "not yet measured"
    return ", ".join(f"{model}: {row.get('verdict', '?')}" for model, row in sorted(per_model.items()))


def render_inventory(
    sites: list[CallSite],
    registry: dict[str, dict[str, Any]],
    lock: dict[str, Any],
    state: dict[str, Any],
) -> str:
    """Render the human-readable inventory: one row per call site, with version and verdicts.

    No line numbers: an unrelated edit above a call would otherwise make the committed file stale.
    """
    by_status: dict[str, int] = {}
    rows: list[str] = []
    for site in sites:
        entry = registry.get(site.key, {})
        status = str(entry.get("status", "UNREGISTERED"))
        bucket = status.split(":", 1)[0]
        by_status[bucket] = by_status.get(bucket, 0) + 1
        pid = str(entry.get("id", "—"))
        locked = lock.get(pid) or {}
        version = locked.get("version")
        versioned_by = ("—" if version is None
                        else "rendered text" if locked.get("content_hash") else "source")
        rows.append(
            f"| `{pid}` | `{site.module}` | `{site.function}` | "
            f"{entry.get('family', '—')} | {status} | {version if version is not None else '—'} | "
            f"{versioned_by} | {_latest_verdicts(state, pid, version)} |"
        )
    summary = ", ".join(f"{k} {v}" for k, v in sorted(by_status.items()))
    return "\n".join([
        "# Prompt inventory",
        "",
        "<!-- GENERATED by `python scripts/prompt_registry.py --inventory` — do not hand-edit. -->",
        "",
        f"{len(sites)} LLM call sites ({summary}). Posture: [`docs/prompt-evals.md`](../prompt-evals.md).",
        "",
        "| Prompt id | Module | Function | Family | Status | Version | Versioned by | Latest verdicts |",
        "|---|---|---|---|---|---|---|---|",
        *rows,
        "",
    ])


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; see the module docstring for the modes."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--scan", action="store_true", help="print every call site found")
    mode.add_argument("--check", action="store_true", help="fail on registry drift")
    mode.add_argument("--inventory", action="store_true", help="rewrite inventory.md")
    args = parser.parse_args(argv)

    sites = scan()
    if args.scan:
        for site in sites:
            sys.stdout.write(f"{site.key}\t{site.kind}\tline {site.line}\n")
        return 0
    registry = load_registry()
    if args.check:
        unregistered, stale = drift(sites, registry)
        for key in unregistered:
            sys.stdout.write(f"UNREGISTERED {key}\n")
        for key in stale:
            sys.stdout.write(f"STALE {key}\n")
        return 1 if unregistered or stale else 0
    INVENTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    INVENTORY_PATH.write_text(
        render_inventory(sites, registry, load_json(LOCK_PATH), load_json(STATE_PATH)),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
