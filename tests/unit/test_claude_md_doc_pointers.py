"""CLAUDE.md is the map: every `docs/*.md` it names must exist (issue #1033).

The size cap is held by moving detail out of CLAUDE.md into `docs/`, which only works while the
pointers resolve — a renamed or deleted doc turns an invariant into a dead end, and nothing else in
CI reads CLAUDE.md's prose. Stdlib only, no I/O beyond reading the two files.
"""

import pathlib
import re

import pytest

pytestmark = pytest.mark.unit

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_CLAUDE_MD = _ROOT / "CLAUDE.md"

# Matches `docs/foo.md`, `docs/model-benchmarks/README.md`, and a docs/ directory that is NOT at the
# repo root — `scripts/agent-pipeline/docs/agent-pipeline-routing.md`. Without the optional prefix
# this captured only the `docs/...` tail of such a path and then resolved it against the root, so a
# correct pointer to a real file failed as "missing".
_DOC_REF = re.compile(r"(?:[A-Za-z0-9._-]+/)*docs/[A-Za-z0-9._/-]+\.md")


def _referenced_docs() -> set[str]:
    return set(_DOC_REF.findall(_CLAUDE_MD.read_text(encoding="utf-8")))


class TestDocPointers:
    def test_claude_md_names_at_least_one_doc(self):
        """A regex that silently stopped matching would make every assertion below vacuous."""
        assert len(_referenced_docs()) > 10

    def test_every_referenced_doc_exists(self):
        missing = sorted(ref for ref in _referenced_docs() if not (_ROOT / ref).is_file())
        assert not missing, f"CLAUDE.md points at docs that do not exist: {missing}"

    def test_the_directory_map_files_exist(self):
        """The map's own coordinates — a moved module makes the map lie."""
        for path in ("src/cqc_lem/utilities/db.py", "src/cqc_lem/utilities/human_pacing.py",
                     "src/cqc_lem/utilities/ai/client.py", "src/cqc_lem/utilities/logger.py",
                     "scripts/check_claude_md_size.py"):
            assert (_ROOT / path).is_file(), path
