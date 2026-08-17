#!/usr/bin/env python3
"""Keep CLAUDE.md a fixed-shape index — enforce its size AND its structure.

CLAUDE.md is the context window every Claude Code session loads. The 40k cap is a hard
ceiling on the SOURCE file (release-please does not rewrite it, so the rule is
enforceable). But a size cap alone only ever produces a sawtooth: the file grows a row per
merged feature until a human notices and hand-trims it, and each trim loses invariants
because most rows have nowhere to be moved to. Measured on main: 15,003 chars on
2026-07-06, 53,622 on 07-27, hand-trimmed to 39,806 on 07-31, 46,494 by 08-17.

So this script checks two things:

* **Size** — the original strict check, on the root file and on every tracked
  directory-scoped `CLAUDE.md` (a scoped file loads ON TOP of the root one, so it spends
  the same context budget; the root-only check used to miss them entirely).
* **Structure** — the file's shape against `.github/claude-md-schema.json`: a closed
  section set in a fixed order, a per-section char budget, a row contract on the index
  tables, and pointers that resolve. Rules are `CM000`-`CM020`; see CONTRIBUTING.md.

The ratchet: each section in the schema carries a `budget` (enforced now) and a `target`
(where it is going). `CM000` refuses the schema if its targets exceed the ceilings below,
or if any `budget` sits below its own `target` — a budget only ever moves DOWN. Those
ceilings live HERE, in code, and not in the JSON on purpose: relaxing the guard then means
editing this file and its unit test in the same PR, which is a shape review already looks
for, rather than nudging a number in a data file.

Invocation shapes:

* Default (no flags) — size + structure on every tracked CLAUDE.md; exit 1 on any error.
* `--size-only` — the pre-#1000 behaviour: size of the root file only.
* `--warn-at N` — soft mode for the `main`-push drift watch. Never fails the build;
  writes `status`/`size`/`violations` to `$GITHUB_OUTPUT` so the caller can file or update
  a tracking issue while there is still runway to trim deliberately.
* `--baseline-ref REF` — also compare the root file against another git ref (e.g. a PR's
  base branch), so an inherited overage reads differently from one this diff caused.
* `--files A B` / `--json` — targeted run and machine-readable output (the trim worklist).

Stdlib only — invoked from CI and can be run locally before pushing.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable, List, NamedTuple, Optional, Sequence, Tuple

# Cap is enforced on the FILE — not on the harness's input after slicing, not on a per-PR
# diff. The whole file goes into every session. Measured in CHARACTERS, not bytes.
MAX_CHARS = 40_000

# Runway before the hard cap, so drift is visible while there's still room to trim
# deliberately instead of only finding out when a PR goes red.
DEFAULT_WARN_CHARS = 38_000

# --- The ceilings the schema may never talk its way past (see the docstring) ------------
# Total of every section's `target`. The gap to MAX_CHARS is deliberate headroom, not
# spare space: it is what stops the next busy fortnight from re-running the sawtooth.
HARD_TOTAL_BUDGET = 34_000
# No single section may target more than this. Feature Areas is the one that tests it.
MAX_SECTION_BUDGET = 9_000

REPO_ROOT = Path(__file__).resolve().parent.parent
TARGET = REPO_ROOT / "CLAUDE.md"
SCHEMA_PATH = REPO_ROOT / ".github" / "claude-md-schema.json"

FOOTER = "see CONTRIBUTING.md § CLAUDE.md is a fixed-shape file"

# A pointer cell must name a doc. An em dash is a placeholder, not a pointer — a row that
# points nowhere is a row whose detail can never leave this file.
_DOC_RE = re.compile(r"docs/[A-Za-z0-9_/.\- ]*?\.md")
_TABLE_SEP_RE = re.compile(r"^\|[\s:|-]+\|$")
_BOLD_LEAD_RE = re.compile(r"^\*\*(.+?)\*\*")
_ISSUE_REF_RE = re.compile(r"\(#[\d\s,/#a-z]+\)")
_BACKTICK_RE = re.compile(r"`([^`]+)`")
_NOANCHOR_RE = re.compile(r"<!--\s*claude-md-lint:\s*no-anchor\b")


class Violation(NamedTuple):
    """One rule failure, addressed to whoever has to fix it."""

    code: str
    file: str
    line: int
    message: str
    level: str = "error"

    def render(self, github: bool) -> str:
        """Format for a human reading a CI log, or as a GitHub annotation."""
        where = f"{self.file}:{self.line}" if self.line else self.file
        body = f"{where} {self.code} {self.message} — {FOOTER}"
        if not github:
            return body
        kind = "error" if self.level == "error" else "warning"
        return f"::{kind} file={self.file},line={max(self.line, 1)}::{body}"


class Table(NamedTuple):
    """A parsed markdown table: its header, its data rows, and where they start."""

    header: List[str]
    rows: List[Tuple[int, List[str], str]]  # (1-indexed line, cells, raw line)
    line: int


class Section(NamedTuple):
    """One `##` block of a CLAUDE.md, with everything the rules need to judge it."""

    name: str
    line: int
    chars: int
    subsections: List[Tuple[int, str]]
    tables: List[Table]
    prose: str
    deep_headings: List[Tuple[int, str]]
    fenced: List[Tuple[int, List[str]]]


# --------------------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------------------


def _split_row(line: str) -> List[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def parse_markdown(text: str) -> Tuple[int, List[Section]]:
    """Split a CLAUDE.md into its `##` sections. Returns (preamble chars, sections).

    Fenced code blocks are tracked so the Directory Map's tree drawing and the AI Call
    Pattern's python block can never be mistaken for headings or tables.
    """
    lines = text.splitlines(keepends=True)
    preamble = 0
    sections: List[Section] = []

    name = None
    start = 0
    chars = 0
    subs: List[Tuple[int, str]] = []
    deep: List[Tuple[int, str]] = []
    tables: List[Table] = []
    prose_parts: List[str] = []
    fences: List[Tuple[int, List[str]]] = []

    in_fence = False
    fence_start = 0
    fence_body: List[str] = []
    i = 0

    def flush() -> None:
        if name is not None:
            sections.append(Section(name, start, chars, list(subs), list(tables),
                                    "".join(prose_parts), list(deep), list(fences)))

    while i < len(lines):
        raw = lines[i]
        stripped = raw.rstrip("\n")
        lineno = i + 1

        if stripped.startswith("```"):
            if in_fence:
                fences.append((fence_start, list(fence_body)))
                fence_body = []
            else:
                fence_start = lineno
            in_fence = not in_fence
            chars += len(raw) if name is not None else 0
            if name is None:
                preamble += len(raw)
            i += 1
            continue

        if in_fence:
            fence_body.append(stripped)
            if name is None:
                preamble += len(raw)
            else:
                chars += len(raw)
            i += 1
            continue

        if stripped.startswith("## "):
            flush()
            name = stripped[3:].strip()
            start = lineno
            chars = len(raw)
            subs, deep, tables, prose_parts, fences = [], [], [], [], []
            i += 1
            continue

        if name is None:
            preamble += len(raw)
            i += 1
            continue

        chars += len(raw)

        if stripped.startswith("### "):
            subs.append((lineno, stripped[4:].strip()))
        elif re.match(r"^#{4,}\s", stripped):
            deep.append((lineno, stripped))

        # A table is a header row followed by a separator row. Anything else starting
        # with "| " is a continuation row of the table we are already in.
        if stripped.startswith("| ") and i + 1 < len(lines) and _TABLE_SEP_RE.match(lines[i + 1].rstrip("\n")):
            header = _split_row(stripped)
            chars += len(lines[i + 1])
            j = i + 2
            rows: List[Tuple[int, List[str], str]] = []
            while j < len(lines) and lines[j].startswith("| "):
                rows.append((j + 1, _split_row(lines[j].rstrip("\n")), lines[j].rstrip("\n")))
                chars += len(lines[j])
                j += 1
            tables.append(Table(header, rows, lineno))
            i = j
            continue

        prose_parts.append(raw)
        i += 1

    flush()
    return preamble, sections


def _normalize_anchor(term: str) -> str:
    term = _ISSUE_REF_RE.sub("", term)
    return re.sub(r"[^a-z0-9]+", "", term.lower())


def _doc_anchors(path: Path) -> set:
    """Every heading and bolded term in a doc, normalized — what a row may anchor to."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return set()
    out = set()
    for m in re.finditer(r"^#{1,6}\s+(.*)$", text, re.MULTILINE):
        out.add(_normalize_anchor(m.group(1)))
    for m in re.finditer(r"\*\*(.+?)\*\*", text):
        out.add(_normalize_anchor(m.group(1)))
    return {a for a in out if a}


def _directory_map_paths(fenced: Sequence[Tuple[int, List[str]]]) -> List[Tuple[int, str]]:
    """Reconstruct full repo-relative paths from the Directory Map's tree drawing.

    Depth comes from the box-drawing prefix (4 chars per level), so `└── selenium_util.py`
    nested under `├── utilities/` under `src/cqc_lem/` resolves to
    `src/cqc_lem/utilities/selenium_util.py` — which is the whole point of checking it.
    A line with no box prefix is a new root and resets the stack.
    """
    out: List[Tuple[int, str]] = []
    for start, body in fenced:
        stack: List[str] = []
        for offset, line in enumerate(body):
            m = re.match(r"^([\s│├└─]*?)(?:├──\s|└──\s)(\S+)", line)
            lineno = start + offset + 1
            if m:
                depth = len(m.group(1)) // 4 + 1
                name = m.group(2)
            else:
                m2 = re.match(r"^(\S+)", line)
                if not m2 or not ("/" in m2.group(1) or "." in m2.group(1)):
                    continue
                depth, name = 0, m2.group(1)
            # Prose comment columns and glob-ish names are not paths to resolve.
            if name.startswith("#") or "*" in name or "{" in name:
                continue
            stack = stack[:depth]
            stack.append(name.rstrip("/"))
            out.append((lineno, "/".join(stack)))
    return out


# --------------------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------------------


def load_schema(path: Path = SCHEMA_PATH) -> dict:
    """Read the fixed-shape schema. Raises rather than guessing if it is unreadable."""
    return json.loads(path.read_text(encoding="utf-8"))


def check_schema_self(schema: dict) -> List[Violation]:
    """CM000 — the schema must not have talked its way past the ceilings in this file."""
    out: List[Violation] = []
    rel = str(SCHEMA_PATH.relative_to(REPO_ROOT))
    sections = schema.get("sections", [])
    targets = [s["target"] for s in sections] + [schema["preamble"]["target"]]
    total = sum(targets)
    if total > HARD_TOTAL_BUDGET:
        out.append(Violation(
            "CM000", rel, 0,
            f"section targets sum to {total:,} — over HARD_TOTAL_BUDGET ({HARD_TOTAL_BUDGET:,}) in "
            f"scripts/check_claude_md_size.py. Raising a budget is not the fix for a full section",
        ))
    for s in sections:
        if s["target"] > MAX_SECTION_BUDGET:
            out.append(Violation(
                "CM000", rel, 0,
                f'section "{s["name"]}" targets {s["target"]:,}, over MAX_SECTION_BUDGET '
                f"({MAX_SECTION_BUDGET:,}). A section that needs more than this is a section whose "
                f"detail belongs in a doc",
            ))
        if s["budget"] < s["target"]:
            out.append(Violation(
                "CM000", rel, 0,
                f'section "{s["name"]}" has budget {s["budget"]:,} below its target {s["target"]:,} — '
                f"the ratchet only moves DOWN, so a budget is never lower than what it is ratcheting to",
            ))
    return out


def check_structure(path: Path, text: str, schema: dict, *, doc_index_docs: Optional[set] = None) -> List[Violation]:
    """Every structural rule for the ROOT CLAUDE.md."""
    rel = str(path.relative_to(REPO_ROOT))
    out: List[Violation] = []
    preamble_chars, sections = parse_markdown(text)
    specs = {s["name"]: s for s in schema["sections"]}
    order = [s["name"] for s in schema["sections"]]
    cm019_level = schema.get("cm019_level", "warn")

    # CM001 / CM002 / CM003 — the section set is CLOSED and ORDERED.
    seen = [s.name for s in sections]
    for sec in sections:
        if sec.name not in specs:
            out.append(Violation(
                "CM001", rel, sec.line,
                f'unknown section "## {sec.name}" — CLAUDE.md has a CLOSED section set '
                f"(.github/claude-md-schema.json). Put this under an existing section, or in "
                f"docs/<topic>.md and point a row at it",
            ))
    for name in order:
        if name not in seen:
            out.append(Violation("CM002", rel, 0, f'required section "## {name}" is missing'))
    known = [n for n in seen if n in specs]
    expected = [n for n in order if n in seen]
    if known != expected:
        for pos, (got, want) in enumerate(zip(known, expected)):
            if got != want:
                line = next(s.line for s in sections if s.name == got)
                out.append(Violation(
                    "CM003", rel, line,
                    f'"## {got}" is at position {pos + 1}, schema says "## {want}" — a fixed order is '
                    f"what keeps this file's diffs reviewable",
                ))
                break

    if preamble_chars > schema["preamble"]["budget"]:
        out.append(Violation(
            "CM004", rel, 1,
            f"the preamble is {preamble_chars:,} / {schema['preamble']['budget']:,} chars",
        ))

    total = preamble_chars + sum(s.chars for s in sections)
    budget_total = schema["preamble"]["budget"] + sum(s["budget"] for s in schema["sections"])
    # ONE limit, always the binding one. During the ratchet the summed budgets are still above
    # the harness cap, so the cap binds; once they ratchet below it, the budget binds and the
    # remaining gap to MAX_CHARS is the headroom that stops the next sawtooth.
    limit = min(MAX_CHARS, budget_total)
    if total > limit:
        which = ("the harness cap" if limit == MAX_CHARS
                 else f"the {budget_total:,} total budget (harness cap {MAX_CHARS:,} — "
                      f"the gap is deliberate headroom, not spare space)")
        out.append(Violation(
            "CM005", rel, 1, f"{rel} is {total:,} chars, over {which} of {limit:,}"))

    referenced_docs: List[Tuple[int, str]] = []

    for sec in sections:
        spec = specs.get(sec.name)
        if spec is None:
            continue

        # CM004 — a section is FULL by budget, and the fix is never a bigger budget.
        if sec.chars > spec["budget"]:
            worst = sorted(
                ((len(raw), _row_name(cells)) for t in sec.tables for _, cells, raw in t.rows),
                reverse=True,
            )[:2]
            hint = ""
            if worst:
                hint = " Longest rows: " + ", ".join(f'"{n}" {c:,}' for c, n in worst) + "."
            out.append(Violation(
                "CM004", rel, sec.line,
                f'"## {sec.name}" is {sec.chars:,} / {spec["budget"]:,} chars '
                f"(+{sec.chars - spec['budget']:,}). This section is FULL.{hint} Move the prose to the "
                f"doc the row points at. Do NOT raise the budget",
            ))

        # CM010 / CM011 — heading depth and a closed `###` set.
        for line, heading in sec.deep_headings:
            out.append(Violation(
                "CM010", rel, line,
                f'"{heading.strip()}" — ## and ### only. A fourth level means the detail belongs in a doc',
            ))
        allowed_subs = spec.get("subsections")
        if allowed_subs is not None:
            for line, sub in sec.subsections:
                if not any(sub.startswith(a) for a in allowed_subs):
                    out.append(Violation(
                        "CM011", rel, line,
                        f'unknown subsection "### {sub}" under "## {sec.name}" — this section has a '
                        f"CLOSED subsection set. Put it in docs/<topic>.md and point a row at it",
                    ))

        out.extend(_check_tables(rel, sec, spec, schema, cm019_level, referenced_docs))

        if spec.get("directory_map"):
            for line, p in _directory_map_paths(sec.fenced):
                if not (REPO_ROOT / p).exists():
                    out.append(Violation(
                        "CM018", rel, line,
                        f"the Directory Map names `{p}`, which does not exist. A map that points at a "
                        f"moved module is worse than no map — an agent greps for it and invents the rest",
                    ))

    # CM008 — every docs/*.md named ANYWHERE in the file must resolve.
    for line, doc in _iter_doc_refs(text):
        referenced_docs.append((line, doc))
    for line, doc in referenced_docs:
        if not (REPO_ROOT / doc).exists():
            out.append(Violation(
                "CM008", rel, line,
                f"{doc} does not exist. Renames are the usual cause: "
                f"git log --diff-filter=D --name-only -- 'docs/*.md'",
            ))

    if doc_index_docs is not None:
        out.extend(_check_doc_index(schema, doc_index_docs))

    return out


def _row_name(cells: Sequence[str]) -> str:
    if not cells:
        return "?"
    m = _BOLD_LEAD_RE.match(cells[0])
    return (m.group(1) if m else cells[0])[:48]


def _iter_doc_refs(text: str) -> Iterable[Tuple[int, str]]:
    for n, line in enumerate(text.splitlines(), start=1):
        for m in _DOC_RE.finditer(line):
            yield n, m.group(0).strip()


def _check_tables(rel: str, sec: Section, spec: dict, schema: dict, cm019_level: str,
                  referenced_docs: List[Tuple[int, str]]) -> List[Violation]:
    out: List[Violation] = []
    table_specs = list(spec.get("tables", []))
    for table in sec.tables:
        match = next((ts for ts in table_specs if ts["header"] == table.header), None)
        if match is None:
            out.append(Violation(
                "CM020", rel, table.line,
                f'unknown table in "## {sec.name}" with columns '
                f"({' | '.join(table.header)}) — a new table is a schema change. "
                f"Add the row to an existing table, or the detail to a doc",
            ))
            continue
        table_specs.remove(match)
        kind = match.get("kind", "index")
        row_max = match.get(
            "row_max",
            schema["default_row_max"] if kind == "index" else schema["reference_row_max"],
        )
        max_rows = match["max_rows"]

        if len(table.rows) > max_rows:
            out.append(Violation(
                "CM009", rel, table.line,
                f'the "{table.header[0]}" table in "## {sec.name}" has {len(table.rows)} rows, '
                f"max {max_rows}. This table is an INDEX and is full by design. EDIT the row that "
                f"already owns this behaviour. A genuinely new entry is a schema change",
            ))

        pointer = match.get("pointer")
        section_pointer = None
        if isinstance(pointer, str) and pointer.startswith("section:"):
            section_pointer = pointer.split(":", 1)[1]
            if section_pointer not in sec.prose:
                out.append(Violation(
                    "CM007", rel, table.line,
                    f'this table inherits its pointer from the section, but "## {sec.name}" no longer '
                    f"names {section_pointer}. Restore it, or give every row its own Doc cell",
                ))

        for idx, (line, cells, raw) in enumerate(table.rows):
            if len(cells) != len(table.header):
                out.append(Violation(
                    "CM012", rel, line,
                    f"row has {len(cells)} cells, table declares {len(table.header)} "
                    f"({' | '.join(table.header)})",
                ))
                continue
            if len(raw) > row_max:
                out.append(Violation(
                    "CM006", rel, line,
                    f'row "{_row_name(cells)}" is {len(raw):,} / {row_max:,} chars. A row is an INDEX '
                    f"ENTRY: the name, the ONE place, ONE clause of the invariant saying which way it "
                    f"FAILS, and the doc. The paragraph goes in the doc",
                ))
            if kind != "index":
                continue

            if not _BOLD_LEAD_RE.match(cells[0]):
                out.append(Violation(
                    "CM021", rel, line,
                    f'row "{cells[0][:40]}" does not lead with a **bolded name** — that name is the '
                    f"grep handle an agent searches for",
                ))
            if not _BACKTICK_RE.search(cells[1]):
                out.append(Violation(
                    "CM022", rel, line,
                    f'row "{_row_name(cells)}" names no `symbol` or `path` in its "The ONE place" cell — '
                    f"without one the row cannot be acted on",
                ))

            doc = _row_pointer(cells, match, section_pointer, table, idx)
            if doc is None:
                out.append(Violation(
                    "CM007", rel, line,
                    f'row "{_row_name(cells)}" has no doc pointer. A row with nowhere to point is a row '
                    f"whose detail can never leave this file. Add docs/<topic>.md, index it in "
                    f"docs/README.md, and name it here",
                ))
                continue
            if doc == "__SAME_FIRST__":
                out.append(Violation(
                    "CM016", rel, line,
                    f'row "{_row_name(cells)}" points at "same", but it is the FIRST row — there is no '
                    f"row above to inherit from",
                ))
                continue
            referenced_docs.append((line, doc))

            if _NOANCHOR_RE.search(raw):
                continue
            if not _row_is_anchored(cells, REPO_ROOT / doc):
                out.append(Violation(
                    "CM019", rel, line,
                    f'row "{_row_name(cells)}" points at {doc}, but that doc names neither the row nor '
                    f"any symbol from its \"ONE place\" cell — so the row's detail has no home to be "
                    f"trimmed into, and the pointer is probably aimed at the wrong doc. Add the "
                    f"section, fix the pointer, or annotate the row with "
                    f"<!-- claude-md-lint: no-anchor <reason> -->",
                    cm019_level,
                ))
    return out


def _row_is_anchored(cells: Sequence[str], doc: Path) -> bool:
    """Does the doc a row points at actually cover that row? (CM019)

    Two ways to prove it, because a doc legitimately words its headings differently from the
    row that indexes it:

    * the row's bolded NAME appears in a heading or bold term, or
    * any backticked SYMBOL from the row's "ONE place" cell appears anywhere in the doc.

    The symbol test is the stronger evidence — a doc that discusses `_read_groups_directory`
    is provably the doc that owns the groups walk, whatever its headings are called. Demanding
    the doc repeat the row's exact wording would be a spelling rule, not a coverage rule.
    """
    try:
        text = doc.read_text(encoding="utf-8")
    except OSError:
        return True  # CM008 already reports a doc that cannot be read.
    for symbol in _BACKTICK_RE.findall(cells[1] if len(cells) > 1 else ""):
        stem = symbol.strip().strip("`").split("(")[0].strip()
        if len(stem) > 3 and stem in text:
            return True
    anchors = _doc_anchors(doc)
    term = _normalize_anchor(_row_name(cells))
    return bool(anchors) and bool(term) and any(term in a for a in anchors)


def _row_pointer(cells: Sequence[str], match: dict, section_pointer: Optional[str],
                 table: Table, idx: int) -> Optional[str]:
    """Resolve a row's doc pointer: its own cell, `same` (inherit above), or the table's."""
    pointer = match.get("pointer")
    if isinstance(pointer, str) and pointer.startswith("cell:"):
        cell = cells[int(pointer.split(":", 1)[1])]
        m = _DOC_RE.search(cell)
        if m:
            return m.group(0).strip()
        if cell.strip().lower() == "same":
            if idx == 0:
                return "__SAME_FIRST__"
            for back in range(idx - 1, -1, -1):
                prev = table.rows[back][1]
                if len(prev) > int(pointer.split(":", 1)[1]):
                    m2 = _DOC_RE.search(prev[int(pointer.split(":", 1)[1])])
                    if m2:
                        return m2.group(0).strip()
            return "__SAME_FIRST__"
        # A row may still carry its pointer inline in the invariant cell.
        for other in cells:
            m3 = _DOC_RE.search(other)
            if m3:
                return m3.group(0).strip()
        return None
    if section_pointer:
        return section_pointer
    for other in cells:
        m4 = _DOC_RE.search(other)
        if m4:
            return m4.group(0).strip()
    return None


def _check_doc_index(schema: dict, tracked_docs: set) -> List[Violation]:
    """CM013 / CM014 — docs/README.md is the index, and it is complete both ways."""
    out: List[Violation] = []
    index_rel = schema["doc_index"]
    index_path = REPO_ROOT / index_rel
    if not index_path.exists():
        return [Violation("CM013", index_rel, 0, "the doc index does not exist")]
    text = index_path.read_text(encoding="utf-8")
    import urllib.parse

    linked = set()
    for n, line in enumerate(text.splitlines(), start=1):
        for m in re.finditer(r"\]\(([^)]+)\)", line):
            target = urllib.parse.unquote(m.group(1)).split("#")[0]
            if target.startswith(("http://", "https://")):
                continue
            resolved = os.path.normpath(os.path.join(os.path.dirname(index_rel), target))
            linked.add(resolved)
            if not (REPO_ROOT / resolved).exists():
                out.append(Violation("CM014", index_rel, n, f"indexed doc {resolved} does not exist"))
    for doc in sorted(tracked_docs - linked - {index_rel}):
        out.append(Violation(
            "CM013", index_rel, 0,
            f"{doc} is tracked but not indexed. An unindexed doc is why the next author appends a row "
            f"to CLAUDE.md instead of editing the doc that already owns the topic",
        ))
    return out


# --------------------------------------------------------------------------------------
# Targets & size
# --------------------------------------------------------------------------------------


def _git_ls(pattern: str) -> List[str]:
    try:
        result = subprocess.run(["git", "ls-files", "-z", pattern], cwd=REPO_ROOT,
                                capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    return [p for p in result.stdout.split("\0") if p]


def discover_targets(schema: dict) -> List[Path]:
    """Every tracked CLAUDE.md plus the schema's `extra_caps` files.

    Discovered rather than hardcoded, so a new directory-scoped CLAUDE.md is covered the
    day it lands — the root-only hardcode is what left src/cqc_lem/utilities/CLAUDE.md
    (10,661 chars) unguarded for its whole life.
    """
    found = [p for p in _git_ls("*CLAUDE.md") if not p.startswith(".github/")]
    if not found:
        found = ["CLAUDE.md", "src/cqc_lem/utilities/CLAUDE.md"]
    found += [p for p in schema.get("extra_caps", {}) if p not in found]
    return [REPO_ROOT / p for p in found if (REPO_ROOT / p).exists()]


def cap_for(path: Path, schema: dict) -> int:
    rel = str(path.relative_to(REPO_ROOT))
    if rel in schema.get("extra_caps", {}):
        return schema["extra_caps"][rel]
    if rel == schema["root"]:
        return MAX_CHARS
    return schema["nested_cap"]


def _read_size() -> Optional[int]:
    if not TARGET.exists():
        print(f"error: {TARGET} not found", file=sys.stderr)
        return None
    return len(TARGET.read_text(encoding="utf-8"))


def _baseline_size(ref: str) -> Optional[int]:
    """Size of CLAUDE.md at `ref`, or None if it cannot be read.

    A missing ref or a ref predating the file are both None. Never raises — a baseline
    comparison is informational, not a reason to fail the check.
    """
    try:
        result = subprocess.run(
            ["git", "show", f"{ref}:CLAUDE.md"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return len(result.stdout)


def _report_baseline(ref: str, baseline: int, size: int) -> None:
    if baseline > MAX_CHARS and size > MAX_CHARS:
        delta = size - baseline
        sign = "+" if delta >= 0 else ""
        print(
            f"note: CLAUDE.md was already {baseline:,} chars (over the {MAX_CHARS:,} cap) "
            f"on {ref} — this diff changed it by {sign}{delta:,} chars (now {size:,}). "
            f"The overage is inherited, not caused by this diff."
        )
    elif baseline <= MAX_CHARS < size:
        print(
            f"note: this diff pushed CLAUDE.md over the {MAX_CHARS:,}-char cap "
            f"({ref}: {baseline:,} chars, now {size:,})."
        )


def _write_output(key: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"{key}={value}\n")


# --------------------------------------------------------------------------------------
# Runners
# --------------------------------------------------------------------------------------


def run_checks(schema: dict, files: Optional[Sequence[Path]] = None) -> List[Violation]:
    """Size + structure over every target. The one entry point CI and the tests share."""
    out = check_schema_self(schema)
    targets = list(files) if files else discover_targets(schema)
    tracked_docs = {p for p in _git_ls("docs/") if p.endswith(".md")}

    for path in targets:
        rel = str(path.relative_to(REPO_ROOT))
        text = path.read_text(encoding="utf-8")
        # The root file's size is CM005's job (it knows the schema's total budget); everything
        # else is capped here.
        if rel != schema["root"]:
            cap = cap_for(path, schema)
            if len(text) > cap:
                out.append(Violation(
                    "CM015", rel, 1,
                    f"is {len(text):,} / {cap:,} chars. A directory-scoped file loads ON TOP of the "
                    f"root file — same context budget, not a bypass",
                ))
        if rel == schema["root"]:
            out.extend(check_structure(path, text, schema,
                                       doc_index_docs=tracked_docs if tracked_docs else None))
        elif rel.endswith("CLAUDE.md"):
            out.extend(_check_scoped_listed(rel))
    return out


def _check_scoped_listed(rel: str) -> List[Violation]:
    """CM017 — a scoped CLAUDE.md an agent never learns about may as well not exist."""
    try:
        root_text = TARGET.read_text(encoding="utf-8")
    except OSError:
        return []
    if rel in root_text:
        return []
    return [Violation(
        "CM017", rel, 1,
        "is tracked but not named in the root CLAUDE.md — an agent planning across trees never learns "
        "it exists, because a scoped file only auto-loads once you are already editing that tree",
    )]


def _emit(violations: Sequence[Violation], as_json: bool) -> None:
    if as_json:
        print(json.dumps([v._asdict() for v in violations], indent=2))
        return
    github = bool(os.environ.get("GITHUB_ACTIONS"))
    # All on stdout: errors and warnings interleaved across two streams is unreadable in a CI
    # log, and the `::error::` / `::warning::` prefix already carries the severity.
    for v in sorted(violations, key=lambda x: (x.file, x.line, x.code)):
        print(v.render(github))


def _strict_check(size: int) -> int:
    if size > MAX_CHARS:
        print(
            f"error: CLAUDE.md is {size:,} chars (cap: {MAX_CHARS:,}). "
            f"Move detail to docs/*.md and leave the map + invariants here. "
            f"Run `wc -c CLAUDE.md` to see the current size.",
            file=sys.stderr,
        )
        return 1

    print(f"ok: CLAUDE.md is {size:,} / {MAX_CHARS:,} chars")
    return 0


def _soft_check(size: int, warn_at: int, violations: Sequence[Violation] = ()) -> int:
    errors = [v for v in violations if v.level == "error"]
    if size > MAX_CHARS:
        status = "over"
        print(f"::warning::CLAUDE.md is {size:,} chars — OVER the {MAX_CHARS:,}-char cap on main.")
    elif size >= warn_at:
        status = "warn"
        print(
            f"::warning::CLAUDE.md is {size:,} chars — within {MAX_CHARS - size:,} of the "
            f"{MAX_CHARS:,}-char cap on main (warn threshold: {warn_at:,})."
        )
    elif errors:
        status = "structure"
        print(f"::warning::CLAUDE.md is within its size cap but has {len(errors)} structure violation(s).")
    else:
        status = "ok"
        print(f"ok: CLAUDE.md is {size:,} / {MAX_CHARS:,} chars (warn at {warn_at:,})")

    _write_output("status", status)
    _write_output("size", str(size))
    _write_output("violations", str(len(errors)))
    _write_output("codes", ",".join(sorted({v.code for v in errors})))
    # Never fails the build — this is the early-warning path, not the gate.
    return 0


def main() -> int:
    """CLI entry point. Returns the process exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--warn-at", type=int, nargs="?", const=DEFAULT_WARN_CHARS, default=None,
        help=f"soft-warn threshold (default {DEFAULT_WARN_CHARS:,} when passed with no value); "
             f"never fails the build, writes status/size/violations to $GITHUB_OUTPUT",
    )
    parser.add_argument(
        "--baseline-ref", default=None,
        help="git ref (e.g. a PR's base branch) to compare against, to tell an inherited "
             "overage apart from one this diff caused",
    )
    parser.add_argument("--size-only", action="store_true",
                        help="skip the structure rules — size of the root file only")
    parser.add_argument("--files", nargs="*", default=None,
                        help="check only these files instead of every tracked CLAUDE.md")
    parser.add_argument("--json", action="store_true",
                        help="emit violations as JSON (the trim worklist)")
    args = parser.parse_args()

    size = _read_size()
    if size is None:
        return 1

    if args.baseline_ref:
        baseline = _baseline_size(args.baseline_ref)
        if baseline is not None:
            _report_baseline(args.baseline_ref, baseline, size)
        else:
            print(f"note: could not read CLAUDE.md at {args.baseline_ref} — skipping "
                  f"inherited/caused comparison.", file=sys.stderr)

    violations: List[Violation] = []
    if not args.size_only:
        schema = load_schema()
        files = [REPO_ROOT / f for f in args.files] if args.files else None
        violations = run_checks(schema, files)
        _emit(violations, args.json)

    if args.warn_at is not None:
        return _soft_check(size, args.warn_at, violations)

    if args.size_only:
        return _strict_check(size)

    errors = [v for v in violations if v.level == "error"]
    warns = [v for v in violations if v.level != "error"]
    if not args.json:
        if errors:
            print(f"\nerror: {len(errors)} structure violation(s), {len(warns)} warning(s). "
                  f"CLAUDE.md is {size:,} / {MAX_CHARS:,} chars.", file=sys.stderr)
        else:
            print(f"ok: CLAUDE.md is {size:,} / {MAX_CHARS:,} chars; structure clean "
                  f"({len(warns)} warning(s)).")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
