#!/usr/bin/env python3
"""Cut named top-level symbols out of a module, verbatim, with their decorators and comments (#1154).

The `db.py` split proved the rule this exists to enforce: a move is only safe when the moved body is
BYTE-identical to its merge base. An LLM edit pass cannot promise that, and a line-range `sed` loses
the comment block above a function — which in this repo is usually where the invariant is written.

So: pick the symbols by NAME, let the AST find them, and walk backwards over the contiguous comment
lines above each one so the reasoning travels with the code.

Prints the extracted text to stdout and (with --rewrite) removes it from the source.
"""

from __future__ import annotations

import argparse
import ast
import sys


def _toplevel_name(node: ast.AST) -> str | None:
    name = getattr(node, "name", None)
    if name is None and isinstance(node, (ast.Assign, ast.AnnAssign)):
        targets = [node.target] if isinstance(node, ast.AnnAssign) else node.targets
        ids = [t.id for t in targets if isinstance(t, ast.Name)]
        name = ids[0] if ids else None
    return name


def _start_line(node: ast.AST) -> int:
    """The `@decorator` line, not the `def`: ast.lineno points at `def` for a decorated function."""
    decorators = getattr(node, "decorator_list", None) or []
    return min([node.lineno] + [d.lineno for d in decorators])


def _with_leading_comments(lines: list[str], start: int, claimed: set[int]) -> int:
    """Walk up over contiguous comment lines. In this codebase the comment above a function is
    usually the WHY -- the SDUI ordering rule, the escalation reasoning -- so leaving it behind
    strands the explanation in the module the code just left.

    Stops at a blank line or at a line already claimed by an earlier symbol.
    """
    i = start - 1  # 1-indexed -> the line above
    while i >= 1 and i not in claimed and lines[i - 1].lstrip().startswith("#"):
        i -= 1
    return i + 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path")
    parser.add_argument("symbols", nargs="+")
    parser.add_argument("--rewrite", action="store_true", help="delete the blocks from the source")
    args = parser.parse_args()

    source = open(args.path, encoding="utf-8").read()
    lines = source.splitlines(keepends=True)
    tree = ast.parse(source)

    wanted = set(args.symbols)
    nodes = [n for n in tree.body if _toplevel_name(n) in wanted]
    missing = wanted - {_toplevel_name(n) for n in nodes}
    if missing:
        print(f"NOT FOUND: {sorted(missing)}", file=sys.stderr)
        return 2

    nodes.sort(key=_start_line)
    claimed: set[int] = set()
    spans: list[tuple[int, int, str]] = []
    for node in nodes:
        start = _with_leading_comments(lines, _start_line(node), claimed)
        end = node.end_lineno
        claimed.update(range(start, end + 1))
        spans.append((start, end, _toplevel_name(node)))

    # Merge spans that are adjacent or separated only by blank lines, so a contiguous run of the
    # module comes out as one block with its internal spacing intact rather than as N fragments.
    merged: list[list] = []
    for start, end, name in spans:
        if merged and all(not lines[i - 1].strip() for i in range(merged[-1][1] + 1, start)):
            merged[-1][1] = end
            merged[-1][2].append(name)
        else:
            merged.append([start, end, [name]])

    out = []
    for start, end, names in merged:
        print(f"# block {start}-{end}: {', '.join(names)}", file=sys.stderr)
        out.append("".join(lines[start - 1 : end]))
    sys.stdout.write("\n\n".join(b.rstrip("\n") + "\n" for b in out))

    if args.rewrite:
        drop: set[int] = set()
        for start, end, _ in merged:
            # Take the two blank separator lines above the block as well, so removing a block from
            # between two others leaves the PEP 8 two-blank gap rather than four.
            top = start
            while top > 1 and not lines[top - 2].strip() and (start - top) < 2:
                top -= 1
            drop.update(range(top, end + 1))
        kept = [ln for n, ln in enumerate(lines, 1) if n not in drop]
        text = "".join(kept)
        ast.parse(text)  # never write a module the interpreter would reject
        open(args.path, "w", encoding="utf-8").write(text)
        print(f"removed {len(drop)} line(s) from {args.path}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
