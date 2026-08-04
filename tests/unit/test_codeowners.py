"""CODEOWNERS has to keep matching the tree it protects.

A CODEOWNERS pattern that matches nothing is not an error to GitHub — it is silently ignored. So a
rename ("we moved deploy.sh") quietly un-protects a control surface, and nothing anywhere says so.
These tests assert the file still resolves, and that the paths we consider control surfaces are
actually covered.
"""

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
CODEOWNERS = ROOT / ".github" / "CODEOWNERS"


def _rules() -> list[tuple[str, list[str]]]:
    rules = []
    for line in CODEOWNERS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        rules.append((parts[0], parts[1:]))
    return rules


class TestCodeownersResolves:
    def test_the_file_exists(self):
        # Its absence is the whole finding this work started from.
        assert CODEOWNERS.is_file()

    def test_every_pattern_matches_something_in_the_tree(self):
        missing = [pat for pat, _ in _rules()
                   if pat != "*" and not (ROOT / pat.strip("/")).exists()]
        assert not missing, f"CODEOWNERS patterns match nothing (silently ignored): {missing}"

    def test_every_rule_names_an_owner(self):
        ownerless = [pat for pat, owners in _rules()
                     if not owners or not all(o.startswith("@") for o in owners)]
        assert not ownerless, f"CODEOWNERS rules with no/invalid owner: {ownerless}"

    def test_the_catch_all_comes_first_so_specific_rules_win(self):
        # Last match wins in CODEOWNERS. A `*` at the bottom would override every specific rule.
        patterns = [pat for pat, _ in _rules()]
        assert patterns[0] == "*", "the `*` catch-all must be the FIRST rule, not the last"
        assert "*" not in patterns[1:], "a second `*` would override every specific rule below it"


class TestControlSurfacesAreCovered:
    """The paths where a change alters what LATER changes are allowed to do."""

    @pytest.mark.parametrize("path", [
        ".github/workflows/",            # the gates themselves
        ".github/CODEOWNERS",            # this file
        "scripts/agent-pipeline/",       # the trust boundary + the agent's instruction set
        "scripts/deploy.sh",             # reaches production
        "scripts/rollback.sh",
        "src/cqc_lem/api/main.py",       # the ONE caller resolver
        "src/cqc_lem/utilities/crypto.py",
        "compose/local/database/migrations/",
        "scripts/triage_issues.py",      # can write `agent:ready`
        "src/cqc_lem/utilities/feedback/issue_service.py",
    ])
    def test_control_surface_has_an_explicit_rule(self, path):
        patterns = {pat.strip("/") for pat, _ in _rules()}
        assert path.strip("/") in patterns, (
            f"{path} is a control surface but has no explicit CODEOWNERS rule — the `*` catch-all "
            f"covers it only until someone adds a broader rule below it")
