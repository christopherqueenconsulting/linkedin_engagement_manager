#!/usr/bin/env python3
"""Prove a CLAUDE.md trim moved detail instead of deleting it.

Every previous trim of this file was a hand edit judged by eye, and that is how invariants
died: the diff shows a section shrinking, and nothing shows whether the sentence that left
landed anywhere. This script answers exactly that question and nothing else.

**The rule it enforces.** For each `##` section, take every load-bearing token the section
carried at the base ref -- backticked symbols, `#NNNN` issue references, and ALL-CAPS words
of four letters or more (NEVER, ONLY, CLOSED, OPEN, SKIPPED, UNKNOWN, the words that carry
an invariant's direction). Every one of them must still appear somewhere in that section's
**union**: the section's new text, plus every doc it names (before or after), plus every
directory-scoped CLAUDE.md. Nothing may vanish from the union. A token that moved from the
root file into the doc the row points at is a SUCCESS -- that is the whole point of the
trim -- so the union, not the root file, is what gets checked.

It is deliberately one-directional: it never asks whether anything was ADDED, only whether
something was LOST. Trimming is the operation that loses things.

Usage:

    python3 scripts/claude_md_trim_audit.py                 # vs HEAD~1
    python3 scripts/claude_md_trim_audit.py --base origin/main
    python3 scripts/claude_md_trim_audit.py --json

Exits 1 if any token vanished. Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
ROOT_FILE = "CLAUDE.md"

_BACKTICK_RE = re.compile(r"`([^`\n]+)`")
_ISSUE_RE = re.compile(r"#\d{2,5}\b")
_SHOUT_RE = re.compile(r"\b[A-Z][A-Z0-9_]{3,}\b")
# Any markdown file CLAUDE.md points at is a legitimate home, not just docs/ — the Git Safety
# section points at .claude/agents/builder.md and the testing one at tests/README.md.
_DOC_RE = re.compile(r"(?:docs|\.claude|tests|compose|scripts)/[A-Za-z0-9_/.\-]*?\.md")

# ALL-CAPS tokens that are shouting for emphasis in prose we may legitimately reword, or
# acronyms that carry no invariant. Everything else must survive.
# Backticked strings that were never real identifiers, so losing them is not losing an invariant.
# Brace globs are the only case so far: `docs/x/{a,b}.md` names no file, and replacing one with the
# real path it stood for is an improvement the audit would otherwise report as a deletion.
_TOKEN_IGNORE_RE = re.compile(r"[{}]")

_SHOUT_IGNORE = {
    "CLAUDE", "HTTP", "HTTPS", "JSON", "YAML", "HTML", "CSS", "SQL", "API", "APIS", "CLI",
    "MYSQL", "REDIS", "AWS", "VPS", "SPA", "URL", "URLS", "UTC", "UUID", "README", "TODO",
    "NOTE", "WARN", "INFO", "DEBUG", "ERROR", "MERGE", "GITHUB", "LINKEDIN", "OAUTH",
}


def _git_show(ref: str, path: str) -> Optional[str]:
    try:
        result = subprocess.run(["git", "show", f"{ref}:{path}"], cwd=REPO_ROOT,
                                capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def _git_ls(pattern: str) -> List[str]:
    try:
        result = subprocess.run(["git", "ls-files", "-z", pattern], cwd=REPO_ROOT,
                                capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return []
    return [p for p in result.stdout.split("\0") if p] if result.returncode == 0 else []


def split_sections(text: str) -> Dict[str, str]:
    """Map `## Heading` -> that section's text. The preamble lands under `(preamble)`."""
    out: Dict[str, str] = {}
    name = "(preamble)"
    buf: List[str] = []
    for line in text.splitlines(keepends=True):
        if line.startswith("## "):
            out[name] = out.get(name, "") + "".join(buf)
            buf = []
            name = line[3:].strip()
        buf.append(line)
    out[name] = out.get(name, "") + "".join(buf)
    return out


def tokens_of(text: str) -> Set[str]:
    """The load-bearing tokens of a chunk of CLAUDE.md prose."""
    found: Set[str] = set()
    found.update(m.group(1).strip() for m in _BACKTICK_RE.finditer(text))
    found.update(m.group(0) for m in _ISSUE_RE.finditer(text))
    found.update(m.group(0) for m in _SHOUT_RE.finditer(text) if m.group(0) not in _SHOUT_IGNORE)
    return {t for t in found if t and not _TOKEN_IGNORE_RE.search(t)}


def _union_text(section_text_old: str, new_text: str, scoped: str) -> str:
    """Everywhere a token from this section is allowed to have landed.

    Deliberately the WHOLE new root file, not just the same-named section: a trim legitimately
    moves a row into a different section (or splits a section in two), and a token that merely
    changed neighbourhoods has not been lost. What must not happen is a token leaving the union
    of {root file, every scoped CLAUDE.md, every doc this section pointed at then or now}.
    """
    parts = [new_text, scoped]
    for doc in set(_DOC_RE.findall(section_text_old)) | set(_DOC_RE.findall(new_text)):
        path = REPO_ROOT / doc.strip()
        if path.exists():
            parts.append(path.read_text(encoding="utf-8"))
    return "\n".join(parts)


def audit(base: str) -> Tuple[List[dict], List[str]]:
    """Return (findings, notes). A finding is one section's lost tokens."""
    old_text = _git_show(base, ROOT_FILE)
    if old_text is None:
        raise SystemExit(f"error: could not read {ROOT_FILE} at {base}")
    new_text = (REPO_ROOT / ROOT_FILE).read_text(encoding="utf-8")

    scoped_paths = [p for p in _git_ls("*CLAUDE.md") if p != ROOT_FILE and not p.startswith(".github/")]
    # Untracked scoped files count too — the trim usually CREATES them.
    for extra in REPO_ROOT.glob("src/**/CLAUDE.md"):
        rel = str(extra.relative_to(REPO_ROOT))
        if rel not in scoped_paths:
            scoped_paths.append(rel)
    scoped = "\n".join((REPO_ROOT / p).read_text(encoding="utf-8")
                       for p in scoped_paths if (REPO_ROOT / p).exists())

    old_sections = split_sections(old_text)
    new_sections = split_sections(new_text)

    findings: List[dict] = []
    notes: List[str] = []
    for name, old_body in old_sections.items():
        if name not in new_sections:
            notes.append(f'section "## {name}" no longer exists under that name — its tokens are '
                         f"still required somewhere in the union")
        union = _union_text(old_body, new_text, scoped)
        lost = sorted(t for t in tokens_of(old_body) if t not in union)
        if lost:
            findings.append({"section": name, "lost": lost, "count": len(lost)})
    return findings, notes


def main() -> int:
    """CLI entry point. Returns the process exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="HEAD~1",
                        help="git ref holding the pre-trim CLAUDE.md (default HEAD~1)")
    parser.add_argument("--json", action="store_true", help="emit findings as JSON")
    args = parser.parse_args()

    findings, notes = audit(args.base)

    if args.json:
        print(json.dumps({"base": args.base, "findings": findings, "notes": notes}, indent=2))
        return 1 if findings else 0

    for note in notes:
        print(f"note: {note}")
    if not findings:
        print(f"ok: no invariant token vanished between {args.base} and the working tree.")
        return 0

    total = sum(f["count"] for f in findings)
    print(f"error: {total} token(s) vanished from the union across {len(findings)} section(s).\n",
          file=sys.stderr)
    for f in findings:
        print(f'  ## {f["section"]} — {f["count"]} lost:', file=sys.stderr)
        for token in f["lost"]:
            print(f"      {token}", file=sys.stderr)
    print("\nEach one was in CLAUDE.md before and is now in neither the trimmed section, nor a doc "
          "that section names, nor a scoped CLAUDE.md. Move it, do not drop it.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
