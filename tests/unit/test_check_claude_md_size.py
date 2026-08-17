"""Unit tests for scripts/check_claude_md_size.py.

Covers the size/threshold/baseline logic (issue #1000) and the fixed-shape structure linter
layered on top of it. Real I/O (git, $GITHUB_OUTPUT) is mocked; structure rules run against
inline fixture strings.

Note `TestRuleParserPositiveControl`: the row/section parser is a set of narrowed regexes,
and a narrowed pattern is worth trusting only once it has been shown to still match what it
forbids (the argument `.github/workflows/ui-build.yml` already makes for its own grep). So
that class feeds the linter known-bad markdown and asserts it is caught — without it, a
parser that silently stopped matching would read as a permanently clean file.
"""

import importlib.util
import json
import pathlib
import subprocess

import pytest

pytestmark = pytest.mark.unit

# The tool lives under scripts/ (not an importable package) — load it by path.
_ROOT = pathlib.Path(__file__).resolve().parents[2]
_PATH = _ROOT / "scripts" / "check_claude_md_size.py"
_spec = importlib.util.spec_from_file_location("check_claude_md_size", _PATH)
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)


class TestStrictCheck:
    def test_under_cap_ok(self, capsys):
        assert guard._strict_check(guard.MAX_CHARS - 1) == 0
        assert "ok:" in capsys.readouterr().out

    def test_at_cap_ok(self, capsys):
        assert guard._strict_check(guard.MAX_CHARS) == 0

    def test_over_cap_fails(self, capsys):
        assert guard._strict_check(guard.MAX_CHARS + 1) == 1
        assert "error:" in capsys.readouterr().err


class TestSoftCheck:
    def test_under_warn_is_ok_and_never_fails(self, capsys):
        rc = guard._soft_check(guard.DEFAULT_WARN_CHARS - 1, guard.DEFAULT_WARN_CHARS)
        assert rc == 0
        assert "ok:" in capsys.readouterr().out

    def test_in_warn_zone_warns_but_does_not_fail(self, capsys):
        rc = guard._soft_check(guard.DEFAULT_WARN_CHARS, guard.DEFAULT_WARN_CHARS)
        assert rc == 0
        out = capsys.readouterr().out
        assert "::warning::" in out
        assert "OVER" not in out

    def test_over_cap_still_does_not_fail_the_build(self, capsys):
        # The soft path is the early-warning shape (issue #1000): a docs-cap
        # regression on main must never redden the push run.
        rc = guard._soft_check(guard.MAX_CHARS + 1, guard.DEFAULT_WARN_CHARS)
        assert rc == 0
        out = capsys.readouterr().out
        assert "::warning::" in out and "OVER" in out

    def test_writes_github_output(self, tmp_path, monkeypatch):
        out_file = tmp_path / "gh_output.txt"
        monkeypatch.setenv("GITHUB_OUTPUT", str(out_file))
        guard._soft_check(guard.MAX_CHARS + 5, guard.DEFAULT_WARN_CHARS)
        content = out_file.read_text()
        assert "status=over" in content
        assert f"size={guard.MAX_CHARS + 5}" in content

    def test_no_github_output_env_does_not_raise(self, monkeypatch):
        monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
        assert guard._soft_check(100, guard.DEFAULT_WARN_CHARS) == 0


class TestBaselineReport:
    def test_inherited_overage_notes_it_was_already_over(self, capsys):
        over = guard.MAX_CHARS + 500
        guard._report_baseline("origin/main", over, over + 200)
        out = capsys.readouterr().out
        assert "already" in out and "inherited" in out

    def test_caused_overage_notes_this_diff_pushed_it_over(self, capsys):
        under = guard.MAX_CHARS - 100
        guard._report_baseline("origin/main", under, under + 300)
        out = capsys.readouterr().out
        assert "pushed CLAUDE.md over" in out

    def test_both_under_cap_prints_nothing(self, capsys):
        under = guard.MAX_CHARS - 500
        guard._report_baseline("origin/main", under, under + 400)
        assert capsys.readouterr().out == ""


class TestBaselineSize:
    def test_reads_size_from_git_show(self, monkeypatch):
        def fake_run(args, **kwargs):
            assert args[:2] == ["git", "show"]
            return subprocess.CompletedProcess(args, 0, stdout="abcde", stderr="")
        monkeypatch.setattr(guard.subprocess, "run", fake_run)
        assert guard._baseline_size("origin/main") == 5

    def test_missing_ref_returns_none(self, monkeypatch):
        def fake_run(args, **kwargs):
            return subprocess.CompletedProcess(args, 128, stdout="", stderr="bad rev")
        monkeypatch.setattr(guard.subprocess, "run", fake_run)
        assert guard._baseline_size("origin/does-not-exist") is None

    def test_git_not_available_returns_none(self, monkeypatch):
        def fake_run(args, **kwargs):
            raise FileNotFoundError("git not found")
        monkeypatch.setattr(guard.subprocess, "run", fake_run)
        assert guard._baseline_size("origin/main") is None


# ---------------------------------------------------------------------------------------
# Structure linter
# ---------------------------------------------------------------------------------------

_SCHEMA = {
    "root": "CLAUDE.md",
    "doc_index": "docs/README.md",
    "nested_cap": 12000,
    "extra_caps": {},
    "default_row_max": 60,
    "reference_row_max": 40,
    "cm019_level": "warn",
    "preamble": {"budget": 100, "target": 100},
    "sections": [
        {"name": "Alpha", "budget": 500, "target": 400},
        {
            "name": "Beta",
            "budget": 500,
            "target": 400,
            "subsections": ["Known sub"],
            "tables": [{
                "header": ["Surface", "The ONE place", "The invariant that bites", "Doc"],
                "kind": "index", "max_rows": 2, "pointer": "cell:3",
            }],
        },
    ],
}


def _lint(markdown, schema=None):
    """Run the structure rules over fixture markdown, returning {code: [messages]}."""
    schema = schema or _SCHEMA
    violations = guard.check_structure(guard.REPO_ROOT / "CLAUDE.md", markdown, schema)
    out = {}
    for v in violations:
        out.setdefault(v.code, []).append(v.message)
    return out


_CLEAN = (
    "# Title\n\n"
    "## Alpha\n\nbody\n\n"
    "## Beta\n\n### Known sub\n\n"
    "Full posture: docs/README.md\n\n"
    "| Surface | The ONE place | The invariant that bites | Doc |\n"
    "|---|---|---|---|\n"
    "| **One** | `a.py` | fails CLOSED | docs/README.md |\n"
)


class TestSchemaSelfCheck:
    """CM000 — the ceilings are in CODE, so relaxing them is a code+test diff."""

    def test_the_real_schema_obeys_the_hard_ceilings(self):
        assert guard.check_schema_self(guard.load_schema()) == []

    def test_constants_are_what_the_plan_committed_to(self):
        # Pinned deliberately: these two numbers are the whole defense against "just raise
        # the budget", so moving one has to be a visible, argued change — not a drive-by.
        assert guard.HARD_TOTAL_BUDGET == 34_000
        assert guard.MAX_SECTION_BUDGET == 9_000
        assert guard.HARD_TOTAL_BUDGET < guard.MAX_CHARS

    def test_real_schema_targets_leave_headroom_under_the_harness_cap(self):
        schema = guard.load_schema()
        total = sum(s["target"] for s in schema["sections"]) + schema["preamble"]["target"]
        assert total <= guard.HARD_TOTAL_BUDGET
        assert guard.MAX_CHARS - total >= 5_000, "headroom is the point; do not spend it"

    def test_targets_over_the_section_ceiling_are_refused(self):
        schema = json.loads(json.dumps(_SCHEMA))
        schema["sections"][0]["target"] = guard.MAX_SECTION_BUDGET + 1
        schema["sections"][0]["budget"] = guard.MAX_SECTION_BUDGET + 1
        codes = {v.code for v in guard.check_schema_self(schema)}
        assert codes == {"CM000"}

    def test_targets_summing_over_the_total_ceiling_are_refused(self):
        schema = json.loads(json.dumps(_SCHEMA))
        for s in schema["sections"]:
            s["target"] = s["budget"] = guard.MAX_SECTION_BUDGET
        schema["preamble"]["target"] = schema["preamble"]["budget"] = guard.HARD_TOTAL_BUDGET
        msgs = [v.message for v in guard.check_schema_self(schema)]
        assert any("HARD_TOTAL_BUDGET" in m for m in msgs)

    def test_a_budget_below_its_target_is_refused_because_the_ratchet_only_falls(self):
        schema = json.loads(json.dumps(_SCHEMA))
        schema["sections"][0]["budget"] = schema["sections"][0]["target"] - 1
        msgs = [v.message for v in guard.check_schema_self(schema)]
        assert any("only moves DOWN" in m for m in msgs)


class TestSectionRules:
    def test_clean_fixture_has_no_section_violations(self):
        codes = _lint(_CLEAN)
        assert not {"CM001", "CM002", "CM003", "CM004", "CM010", "CM011"} & set(codes)

    def test_unknown_section_is_refused(self):
        codes = _lint(_CLEAN + "\n## Cost Controls\n\nbody\n")
        assert "CM001" in codes
        assert "CLOSED section set" in codes["CM001"][0]

    def test_missing_required_section_is_reported(self):
        codes = _lint("# Title\n\n## Alpha\n\nbody\n")
        assert "CM002" in codes

    def test_sections_out_of_order_are_reported(self):
        codes = _lint("# Title\n\n## Beta\n\nb\n\n## Alpha\n\na\n")
        assert "CM003" in codes

    def test_section_over_budget_names_its_longest_rows_and_refuses_a_bigger_budget(self):
        fat = _CLEAN.replace("body", "x" * 900)
        codes = _lint(fat)
        assert "CM004" in codes
        assert "Do NOT raise the budget" in codes["CM004"][0]

    def test_fourth_level_heading_is_refused(self):
        codes = _lint(_CLEAN + "\n#### Too deep\n")
        assert "CM010" in codes

    def test_unknown_subsection_under_a_closed_section_is_refused(self):
        codes = _lint(_CLEAN.replace("### Known sub", "### Surprise sub"))
        assert "CM011" in codes

    def test_headings_inside_a_fenced_block_are_not_headings(self):
        # The Directory Map's tree and the AI Call Pattern's python block both contain
        # lines that would otherwise parse as structure.
        fenced = _CLEAN + "\n```\n## Not A Section\n#### Not A Heading\n```\n"
        codes = _lint(fenced)
        assert "CM001" not in codes and "CM010" not in codes


class TestRowContract:
    def _rows(self, *rows):
        head = ("| Surface | The ONE place | The invariant that bites | Doc |\n"
                "|---|---|---|---|\n")
        return ("# T\n\n## Alpha\n\nb\n\n## Beta\n\n### Known sub\n\nx\n\n" + head + "".join(rows))

    def test_row_over_row_max_is_refused_with_the_index_entry_rationale(self):
        codes = _lint(self._rows(f"| **Long** | `a.py` | {'y' * 200} | docs/README.md |\n"))
        assert "CM006" in codes
        assert "INDEX ENTRY" in codes["CM006"][0]

    def test_row_without_a_pointer_is_refused(self):
        codes = _lint(self._rows("| **No home** | `a.py` | fails OPEN | — |\n"))
        assert "CM007" in codes
        assert "can never leave this file" in codes["CM007"][0]

    def test_em_dash_is_not_a_pointer(self):
        assert "CM007" in _lint(self._rows("| **X** | `a.py` | ok | — |\n"))

    def test_dead_pointer_is_refused(self):
        codes = _lint(self._rows("| **X** | `a.py` | ok | docs/not-a-real-doc.md |\n"))
        assert "CM008" in codes

    def test_table_over_max_rows_says_edit_the_existing_row(self):
        codes = _lint(self._rows(
            "| **A** | `a.py` | ok | docs/README.md |\n",
            "| **B** | `b.py` | ok | docs/README.md |\n",
            "| **C** | `c.py` | ok | docs/README.md |\n",
        ))
        assert "CM009" in codes
        assert "EDIT the row that already owns" in codes["CM009"][0]

    def test_wrong_cell_count_is_reported(self):
        codes = _lint(self._rows("| **X** | `a.py` | ok |\n"))
        assert "CM012" in codes

    def test_same_on_the_first_row_has_nothing_to_inherit(self):
        codes = _lint(self._rows("| **X** | `a.py` | ok | same |\n"))
        assert "CM016" in codes

    def test_same_inherits_the_pointer_from_the_row_above(self):
        codes = _lint(self._rows(
            "| **A** | `a.py` | ok | docs/README.md |\n",
            "| **B** | `b.py` | ok | same |\n",
        ))
        assert "CM007" not in codes and "CM016" not in codes

    def test_row_without_a_bold_lead_name_is_refused(self):
        codes = _lint(self._rows("| plain name | `a.py` | ok | docs/README.md |\n"))
        assert "CM021" in codes

    def test_row_naming_no_symbol_in_the_one_place_is_refused(self):
        codes = _lint(self._rows("| **X** | somewhere vague | ok | docs/README.md |\n"))
        assert "CM022" in codes

    def test_a_new_table_is_a_schema_change(self):
        extra = _CLEAN + "\n| Foo | Bar |\n|---|---|\n| a | b |\n"
        codes = _lint(extra)
        assert "CM020" in codes

    def test_reference_rows_need_no_pointer(self):
        schema = json.loads(json.dumps(_SCHEMA))
        schema["sections"][1]["tables"] = [{
            "header": ["Layer", "Technology"], "kind": "reference", "max_rows": 2,
        }]
        md = ("# T\n\n## Alpha\n\nb\n\n## Beta\n\n### Known sub\n\nx\n\n"
              "| Layer | Technology |\n|---|---|\n| DB | MySQL 8 |\n")
        codes = _lint(md, schema)
        assert "CM007" not in codes and "CM021" not in codes


class TestPointerResolution:
    def test_section_level_pointer_covers_every_row_in_its_table(self):
        schema = json.loads(json.dumps(_SCHEMA))
        schema["sections"][1]["tables"] = [{
            "header": ["Lane", "The ONE place", "The invariant that bites"],
            "kind": "index", "max_rows": 2, "pointer": "section:docs/README.md",
        }]
        md = ("# T\n\n## Alpha\n\nb\n\n## Beta\n\n### Known sub\n\n"
              "Full posture for every row: docs/README.md\n\n"
              "| Lane | The ONE place | The invariant that bites |\n|---|---|---|\n"
              "| **A** | `a.py` | ok |\n")
        assert "CM007" not in _lint(md, schema)

    def test_losing_the_section_pointer_breaks_every_row_at_once(self):
        schema = json.loads(json.dumps(_SCHEMA))
        schema["sections"][1]["tables"] = [{
            "header": ["Lane", "The ONE place", "The invariant that bites"],
            "kind": "index", "max_rows": 2, "pointer": "section:docs/README.md",
        }]
        md = ("# T\n\n## Alpha\n\nb\n\n## Beta\n\n### Known sub\n\nno pointer here\n\n"
              "| Lane | The ONE place | The invariant that bites |\n|---|---|---|\n"
              "| **A** | `a.py` | ok |\n")
        assert "CM007" in _lint(md, schema)


class TestDirectoryMap:
    def test_the_real_map_resolves_every_path_it_draws(self):
        """CM018 replaces a test that hardcoded five paths — this walks all of them."""
        text = (guard.REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
        _, sections = guard.parse_markdown(text)
        dmap = next(s for s in sections if s.name == "Directory Map")
        paths = guard._directory_map_paths(dmap.fenced)
        assert len(paths) > 15, "the tree walker stopped resolving — it would pass vacuously"
        missing = [p for _, p in paths if not (guard.REPO_ROOT / p).exists()]
        assert not missing, f"Directory Map names paths that do not exist: {missing}"

    def test_nesting_resolves_against_its_parents(self):
        fenced = [(1, ["src/pkg/", "├── utilities/", "│   └── logger.py", "└── api/"])]
        assert [p for _, p in guard._directory_map_paths(fenced)] == [
            "src/pkg", "src/pkg/utilities", "src/pkg/utilities/logger.py", "src/pkg/api",
        ]


class TestDocIndex:
    def test_every_tracked_doc_is_indexed(self):
        schema = guard.load_schema()
        tracked = {p for p in guard._git_ls("docs/") if p.endswith(".md")}
        assert tracked, "git ls-files returned nothing — the check would pass vacuously"
        assert guard._check_doc_index(schema, tracked) == []

    def test_an_unindexed_doc_is_reported(self):
        schema = guard.load_schema()
        codes = {v.code for v in guard._check_doc_index(schema, {"docs/invented.md"})}
        assert "CM013" in codes


class TestRuleParserPositiveControl:
    """Feed the linter known-bad markdown and assert every planted fault is caught.

    A narrowed regex is worth trusting only once it has been shown to still match what it
    forbids. Without this, a parser that quietly stopped matching rows would report a
    permanently clean CLAUDE.md and nobody would notice.
    """

    def test_three_planted_faults_are_all_detected(self):
        planted = (
            "# T\n\n## Alpha\n\nb\n\n## Beta\n\n### Known sub\n\nx\n\n"
            "| Surface | The ONE place | The invariant that bites | Doc |\n"
            "|---|---|---|---|\n"
            f"| **Too long** | `a.py` | {'y' * 200} | docs/README.md |\n"   # CM006
            "| **No pointer** | `b.py` | fails OPEN | — |\n"                # CM007
            "| **Dead** | `c.py` | ok | docs/nope.md |\n"                   # CM008 (+ CM009)
        )
        codes = set(_lint(planted))
        assert {"CM006", "CM007", "CM008"} <= codes

    def test_the_clean_fixture_trips_none_of_them(self):
        assert not {"CM006", "CM007", "CM008", "CM009", "CM012", "CM021", "CM022"} & set(_lint(_CLEAN))


class TestTargetDiscovery:
    def test_every_tracked_claude_md_is_a_target_not_just_the_root(self):
        targets = [str(p.relative_to(guard.REPO_ROOT)) for p in guard.discover_targets(guard.load_schema())]
        assert "CLAUDE.md" in targets
        # The root-only hardcode is what left this file unguarded for its whole life.
        assert "src/cqc_lem/utilities/CLAUDE.md" in targets

    def test_scoped_files_are_capped_and_the_root_is_not_capped_by_nested_cap(self):
        schema = guard.load_schema()
        assert guard.cap_for(guard.REPO_ROOT / "CLAUDE.md", schema) == guard.MAX_CHARS
        assert guard.cap_for(
            guard.REPO_ROOT / "src/cqc_lem/utilities/CLAUDE.md", schema) == schema["nested_cap"]


class TestScopedFileIsDiscoverable:
    def test_a_scoped_file_the_root_never_names_is_reported(self):
        assert guard._check_scoped_listed("src/cqc_lem/nowhere/CLAUDE.md")

    def test_the_existing_scoped_file_is_named_in_the_root_map(self):
        assert guard._check_scoped_listed("src/cqc_lem/utilities/CLAUDE.md") == []


class TestMainCli:
    def test_default_invocation_is_strict_and_reads_real_file(self, monkeypatch):
        # No args: exercises the real CLAUDE.md, mirroring the documented
        # `python3 scripts/check_claude_md_size.py` local/CI invocation.
        monkeypatch.setattr("sys.argv", ["check_claude_md_size.py"])
        assert guard.main() in (0, 1)

    def test_size_only_skips_the_structure_rules(self, monkeypatch, capsys):
        monkeypatch.setattr(guard, "_read_size", lambda: 100)
        monkeypatch.setattr("sys.argv", ["check_claude_md_size.py", "--size-only"])
        assert guard.main() == 0
        assert "CM0" not in capsys.readouterr().out

    def test_json_output_is_parseable(self, monkeypatch, capsys):
        monkeypatch.setattr("sys.argv", ["check_claude_md_size.py", "--json"])
        guard.main()
        assert isinstance(json.loads(capsys.readouterr().out), list)

    def test_warn_at_flag_alone_uses_default_threshold(self, monkeypatch):
        monkeypatch.setattr(guard, "_read_size", lambda: guard.DEFAULT_WARN_CHARS)
        monkeypatch.setattr("sys.argv", ["check_claude_md_size.py", "--warn-at"])
        assert guard.main() == 0

    def test_missing_target_file_fails(self, monkeypatch):
        monkeypatch.setattr(guard, "_read_size", lambda: None)
        monkeypatch.setattr("sys.argv", ["check_claude_md_size.py"])
        assert guard.main() == 1
