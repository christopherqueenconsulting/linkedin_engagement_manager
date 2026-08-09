#!/usr/bin/env python3
"""Re-point `patch("<old module>.X")` at the module the code now READS X from (#1154).

The trap this exists for, learned the hard way in the `db.py` split: a re-export is a SECOND
binding. Reading through a facade resolves the same object; REBINDING one does not. So after a
symbol moves, `patch("old.module.X")` rebinds a name nothing reads, the mock is never called, and
the test passes having tested nothing.

Scoping matters as much as the rewrite. A test file is organised by FEATURE, not by destination
module, so the same file legitimately patches `old.module.find_first` for a test whose code stayed
put and needs `new.module.find_first` for one whose code moved. Rewriting the whole file breaks the
first kind. So the rewrite is confined to named test functions -- in practice, exactly the ones that
just started failing.

Deleted with the rest of the restructure tooling.
"""

from __future__ import annotations

import argparse
import ast
import pathlib
import re
import sys


def _test_spans(source: str, names: set[str]) -> list[tuple[int, int]]:
    """Line spans of the named tests. A name may appear in several classes; take them all."""
    tree = ast.parse(source)
    spans = []

    def walk(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name in names:
                start = min([child.lineno] + [d.lineno for d in child.decorator_list])
                spans.append((start, child.end_lineno))
            elif isinstance(child, ast.ClassDef):
                walk(child)

    walk(tree)
    return spans


def retarget(path: pathlib.Path, tests: set[str], old_alias: str, new_alias: str,
             symbols: set[str], apply: bool) -> int:
    source = path.read_text(encoding="utf-8")
    spans = _test_spans(source, tests)
    if not spans:
        return 0

    lines = source.splitlines(keepends=True)
    pattern = re.compile(r"\{" + re.escape(old_alias) + r"\}\.(" + "|".join(
        sorted(map(re.escape, symbols), key=len, reverse=True)) + r")\b")
    changed = 0
    for start, end in spans:
        for i in range(start - 1, end):
            new_line, n = pattern.subn(lambda m: "{" + new_alias + "}." + m.group(1), lines[i])
            if n:
                lines[i] = new_line
                changed += n

    if changed and apply:
        text = "".join(lines)
        ast.parse(text)
        path.write_text(text, encoding="utf-8")
    return changed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", required=True)
    parser.add_argument("--tests", required=True, help="comma-separated test function names")
    parser.add_argument("--old-alias", default="_RA")
    parser.add_argument("--new-alias", default="_INV")
    parser.add_argument("--symbols", required=True, help="comma-separated symbol names")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    n = retarget(pathlib.Path(args.file), set(args.tests.split(",")), args.old_alias,
                 args.new_alias, set(args.symbols.split(",")), args.apply)
    print(f"{args.file}: {n} rewrite(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
