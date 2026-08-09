#!/usr/bin/env python3
"""Carve one path prefix out of `api/main.py` into `api/routers/<area>.py` (#1154).

The mechanic #1178 established, made repeatable. What it automates is the part that is mechanical
and easy to get subtly wrong by hand:

  * the DEPENDENCY SPLIT. Every symbol a handler reads is either kernel (stays in `main`, reached as
    `_main.<name>` at request time) or group-specific (moves). Getting that wrong in the moving
    direction breaks the ~596 patches aimed at `cqc_lem.api.main.get_session_user_id`; getting it
    wrong the other way leaves an undefined name.
  * the PREFIX. The `APIRouter` must carry the FULL prefix and `include_router` none, because
    `route.path` is what `_scope_path`, `_hide_admin_routes_from_schema` and the session-scope
    guards read. An include-time prefix serves the right URL and is invisible to all of them.
  * the IMPORT ORDER. `from cqc_lem.api import main as _main` goes LAST in the router module, so
    whichever side imports first sees a complete router. Above the routes, `main` includes a
    half-built one — silently, with the missing routes simply absent.

It does NOT touch tests. Re-pointing patches is a per-test judgement (a symbol can stay in `main`
for handlers that did not move while also being read by one that did), and
`tests/unit/api/test_router_patch_seam.py` is what finds the ones that need it.

Deleted with the rest of the restructure tooling.
"""

from __future__ import annotations

import argparse
import ast
import builtins
import pathlib
import subprocess
import sys

MAIN = pathlib.Path("src/cqc_lem/api/main.py")

# Seeds of the part that STAYS. Everything reachable from these is reached as `_main.<name>`.
KERNEL_SEEDS = {
    "get_session_user_id", "require_session_user_id", "_require_admin",
    "_require_api_and_admin", "_require_user_admin", "ResponseModel", "error_responses",
}


def _name(node: ast.AST) -> str | None:
    name = getattr(node, "name", None)
    if name is None and isinstance(node, (ast.Assign, ast.AnnAssign)):
        targets = [node.target] if isinstance(node, ast.AnnAssign) else node.targets
        ids = [t.id for t in targets if isinstance(t, ast.Name)]
        name = ids[0] if ids else None
    return name


def _bound_in(fn: ast.AST) -> set[str]:
    """Names bound inside a function: params, assignments, loop/except/with/comprehension targets.

    Kept separate from module scope on purpose — flattening the two produced two NameError-class
    bugs during the db.py split, one on the error path only and one on the happy path.
    """
    if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return set()
    out = {a.arg for a in fn.args.args + fn.args.kwonlyargs + fn.args.posonlyargs}
    if fn.args.vararg:
        out.add(fn.args.vararg.arg)
    if fn.args.kwarg:
        out.add(fn.args.kwarg.arg)
    for sub in ast.walk(fn):
        if isinstance(sub, ast.Assign):
            for t in sub.targets:
                out |= {y.id for y in ast.walk(t) if isinstance(y, ast.Name)}
        elif isinstance(sub, (ast.AnnAssign, ast.AugAssign)) and isinstance(sub.target, ast.Name):
            out.add(sub.target.id)
        elif isinstance(sub, ast.For):
            out |= {y.id for y in ast.walk(sub.target) if isinstance(y, ast.Name)}
        elif isinstance(sub, (ast.Import, ast.ImportFrom)):
            out |= {(a.asname or a.name).split(".")[0] for a in sub.names}
        elif isinstance(sub, ast.ExceptHandler) and sub.name:
            out.add(sub.name)
        elif isinstance(sub, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            for gen in sub.generators:
                out |= {y.id for y in ast.walk(gen.target) if isinstance(y, ast.Name)}
        elif isinstance(sub, (ast.With, ast.AsyncWith)):
            for item in sub.items:
                if item.optional_vars:
                    out |= {y.id for y in ast.walk(item.optional_vars) if isinstance(y, ast.Name)}
        elif isinstance(sub, ast.Lambda):
            out |= {a.arg for a in sub.args.args + sub.args.kwonlyargs + sub.args.posonlyargs}
    return out


class Analysis:
    def __init__(self, source: str) -> None:
        self.tree = ast.parse(source)
        self.imported: set[str] = set()
        self.nodes: dict[str, ast.AST] = {}
        for node in self.tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                self.imported |= {(a.asname or a.name).split(".")[0] for a in node.names}
            name = _name(node)
            if name and name not in self.nodes:
                self.nodes[name] = node
        self.defined = set(self.nodes) - self.imported
        self.builtins = set(dir(builtins))

    def deps(self, name: str) -> set[str]:
        node = self.nodes.get(name)
        if node is None:
            return set()
        scope = self.builtins | _bound_in(node)
        return {s.id for s in ast.walk(node)
                if isinstance(s, ast.Name) and isinstance(s.ctx, ast.Load)
                and s.id not in scope and s.id in self.defined and s.id != name}

    def closure(self, seeds: set[str]) -> set[str]:
        seen, frontier = set(seeds), set(seeds)
        while frontier:
            nxt: set[str] = set()
            for item in frontier:
                nxt |= self.deps(item) - seen
            seen |= nxt
            frontier = nxt
        return seen

    def handlers_for(self, prefix: str) -> list[str]:
        found = []
        for node in self.tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                        and isinstance(dec.func.value, ast.Name) and dec.func.value.id == "router"
                        and dec.args and isinstance(dec.args[0], ast.Constant)
                        and str(dec.args[0].value).startswith(f"/{prefix}")):
                    found.append(node.name)
        return found

    def free_imports(self, names: set[str]) -> dict[str, str]:
        """Imported names the moved code reads, mapped to the module they came FROM.

        The source module matters as much as the name. Reporting only the name invites a plausible
        guess at the module, and a wrong one fails at import time rather than review time —
        `FUNNEL_CHURNED` reads like it belongs to `marketing.attribution` and actually lives in
        `observability`.
        """
        source: dict[str, str] = {}
        for node in self.tree.body:
            if isinstance(node, ast.ImportFrom) and node.module:
                for alias in node.names:
                    source[alias.asname or alias.name] = node.module
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    source[(alias.asname or alias.name).split(".")[0]] = alias.name

        out: dict[str, str] = {}
        for name in names:
            node = self.nodes.get(name)
            if node is None:
                continue
            scope = self.builtins | _bound_in(node)
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load)
                        and sub.id not in scope and sub.id in self.imported):
                    out[sub.id] = source.get(sub.id, "?")
        return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prefix", help="path segment, e.g. 'billing' for /api/billing/*")
    parser.add_argument("--report", action="store_true", help="analyse only; write nothing")
    args = parser.parse_args()

    analysis = Analysis(MAIN.read_text(encoding="utf-8"))
    handlers = analysis.handlers_for(args.prefix)
    if not handlers:
        print(f"no routes under /{args.prefix}", file=sys.stderr)
        return 2

    kernel = analysis.closure(KERNEL_SEEDS)
    moving = (analysis.closure(set(handlers)) - kernel - {"router"})
    reached = sorted((analysis.closure(set(handlers)) & kernel) - {"router"})
    imports = analysis.free_imports(moving)

    print(f"/{args.prefix}: {len(handlers)} routes")
    print(f"  MOVE ({len(moving)}): {sorted(moving)}")
    print(f"  via _main. ({len(reached)}): {reached}")
    print(f"  imports needed ({len(imports)}), grouped by source module:")
    by_module: dict[str, list[str]] = {}
    for name, module in imports.items():
        by_module.setdefault(module, []).append(name)
    for module in sorted(by_module):
        print(f"    from {module} import {', '.join(sorted(by_module[module]))}")

    if args.report:
        return 0

    ordered = sorted(moving, key=lambda s: analysis.nodes[s].lineno)
    subprocess.run([sys.executable, "scripts/restructure/extract_block.py", str(MAIN),
                    *ordered, "--rewrite"], check=True)
    print(f"\nextracted {len(ordered)} symbol(s); now write the router module header and wire it up")
    return 0


if __name__ == "__main__":
    sys.exit(main())
