#!/usr/bin/env python3
"""Rewrite every `myprint()` call to the structured logger it already delegates to (issue #1154).

`myprint` is a two-line shim: `debug=True` reaches `logger.debug`, everything else `logger.info`.
So the mapping is not a judgement call, it is an identity:

    myprint(x)              ->  log_info(x)
    myprint(x, debug=True)  ->  log_debug(x)
    myprint(x, True)        ->  log_debug(x)

That matters because of the escalation contract in CLAUDE.md: a repeated `log_warning` re-emits at
ERROR and files a grouped `$exception`. A sweep that let an LLM PICK the level would quietly turn
hot-path chatter into paged alerts. This script cannot do that -- it has no level to choose.

Anything it cannot resolve statically (a `debug=` argument that is not a literal) is REPORTED and
left alone, because a wrong guess there is exactly the silent level change the shim's retirement is
supposed to avoid.

Deleted along with the shim once the last call site is gone.
"""

from __future__ import annotations

import argparse
import ast
import pathlib
import sys
from dataclasses import dataclass

LOGGER_MODULE = "cqc_lem.utilities.logger"
SHIM = "myprint"


@dataclass
class Edit:
    """A byte-range replacement in one file, applied back-to-front so offsets stay valid."""

    start: int
    end: int
    text: str


@dataclass
class FileReport:
    path: pathlib.Path
    rewritten: int = 0
    unresolved: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.unresolved is None:
            self.unresolved = []


def _offsets(source: str) -> list[int]:
    """Byte-independent line-start index, so ast (line, col) can address the raw text."""
    starts = [0]
    for line in source.splitlines(keepends=True):
        starts.append(starts[-1] + len(line))
    return starts


def _pos(starts: list[int], line: int, col: int) -> int:
    return starts[line - 1] + col


def _debug_flag(call: ast.Call) -> bool | None:
    """True/False if the call statically selects a level, None if it cannot be resolved.

    None is the important return. `myprint(msg, debug=verbose)` picks its level at runtime; there is
    no single log_* function that preserves that, so the script refuses rather than guessing.
    """
    if len(call.args) >= 2:
        node = call.args[1]
        return node.value if isinstance(node, ast.Constant) and isinstance(node.value, bool) else None
    for kw in call.keywords:
        if kw.arg == "debug":
            node = kw.value
            return (
                node.value
                if isinstance(node, ast.Constant) and isinstance(node.value, bool)
                else None
            )
        if kw.arg is None:  # **kwargs -- opaque
            return None
    return False


def _call_edits(tree: ast.AST, source: str, starts: list[int], report: FileReport) -> tuple[list[Edit], set[str]]:
    edits: list[Edit] = []
    needed: set[str] = set()

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Name) and func.id == SHIM):
            continue

        flag = _debug_flag(node)
        if flag is None:
            report.unresolved.append(f"{report.path}:{node.lineno}: non-literal debug= argument")
            continue

        target = "log_debug" if flag else "log_info"
        needed.add(target)

        # Replace the callee identifier only; the argument text is untouched.
        edits.append(
            Edit(
                _pos(starts, func.lineno, func.col_offset),
                _pos(starts, func.end_lineno, func.end_col_offset),
                target,
            )
        )

        # Drop the level selector: it is now encoded in the function name.
        if len(node.args) >= 2:
            first_end = _pos(starts, node.args[0].end_lineno, node.args[0].end_col_offset)
            second_end = _pos(starts, node.args[1].end_lineno, node.args[1].end_col_offset)
            edits.append(Edit(first_end, second_end, ""))
        else:
            for kw in node.keywords:
                if kw.arg == "debug":
                    # A keyword's own col_offset covers `debug=value`, but not the comma before it.
                    kw_start = _pos(starts, kw.value.lineno, kw.value.col_offset)
                    kw_start = source.rindex("debug", 0, kw_start)
                    prev_end = _pos(
                        starts, node.args[-1].end_lineno, node.args[-1].end_col_offset
                    ) if node.args else kw_start
                    kw_end = _pos(starts, kw.value.end_lineno, kw.value.end_col_offset)
                    edits.append(Edit(prev_end, kw_end, ""))

        report.rewritten += 1

    return edits, needed


def _import_edits(
    tree: ast.AST, starts: list[int], needed: set[str], source: str
) -> list[Edit]:
    """Swap `myprint` out of the logger import for whichever log_* names the file now uses."""
    edits: list[Edit] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.module != LOGGER_MODULE:
            continue
        names = [a.name for a in node.names]
        if SHIM not in names:
            continue
        kept = sorted(set(names) - {SHIM} | needed)
        if not kept:
            continue
        start = _pos(starts, node.lineno, node.col_offset)
        end = _pos(starts, node.end_lineno, node.end_col_offset)
        rendered = f"from {LOGGER_MODULE} import {', '.join(kept)}"
        # Re-wrap only if the one-liner would exceed the line-length ruff enforces.
        if len(rendered) > 100:
            joined = ",\n    ".join(kept)
            rendered = f"from {LOGGER_MODULE} import (\n    {joined},\n)"
        edits.append(Edit(start, end, rendered))
    return edits


def rewrite(path: pathlib.Path, apply: bool) -> FileReport:
    source = path.read_text(encoding="utf-8")
    report = FileReport(path=path)
    if SHIM not in source:
        return report

    tree = ast.parse(source)
    starts = _offsets(source)

    edits, needed = _call_edits(tree, source, starts, report)
    if not edits:
        return report
    edits += _import_edits(tree, starts, needed, source)

    out = source
    for edit in sorted(edits, key=lambda e: e.start, reverse=True):
        out = out[: edit.start] + edit.text + out[edit.end :]

    # Parse the RESULT. A codemod that emits code the interpreter rejects must not reach a branch.
    ast.parse(out)

    if apply:
        path.write_text(out, encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", help="files or directories to rewrite")
    parser.add_argument("--apply", action="store_true", help="write changes (default: report only)")
    args = parser.parse_args()

    targets: list[pathlib.Path] = []
    for raw in args.paths:
        p = pathlib.Path(raw)
        targets.extend(sorted(p.rglob("*.py")) if p.is_dir() else [p])

    total, unresolved = 0, []
    for path in targets:
        report = rewrite(path, args.apply)
        if report.rewritten:
            print(f"{path}: {report.rewritten}")
            total += report.rewritten
        unresolved.extend(report.unresolved)

    print(f"\nrewrote {total} call(s) across {len(targets)} file(s)")
    if unresolved:
        print(f"\nLEFT ALONE ({len(unresolved)}) -- level not statically resolvable:")
        for line in unresolved:
            print(f"  {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
