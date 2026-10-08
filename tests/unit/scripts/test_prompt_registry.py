"""Prompt inventory and drift guard (docs/prompt-evals.md).

Two halves. The synthetic-tree tests pin how `scripts/prompt_registry.py` finds a call site and
hashes a prompt's source. The real-tree tests are the guard itself: every LLM call site in
`src/cqc_lem` and `scripts/` has a registry entry, and the committed inventory is current.
"""

import pathlib
import sys
import textwrap

import pytest

pytestmark = pytest.mark.unit

_ROOT = pathlib.Path(__file__).resolve().parents[3]
_SCRIPTS = _ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import prompt_registry as pr  # noqa: E402


def _tree(tmp_path: pathlib.Path, files: dict[str, str]) -> pathlib.Path:
    for rel, body in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(body), encoding="utf-8")
    return tmp_path


_MODULE = '''
    from cqc_lem.utilities.ai.client import client
    from cqc_lem.utilities.ai.ai_helper import _call_llm

    _SYSTEM = "You classify posts."

    def classify(text):
        """Doc."""
        return _call_llm(model="lem-simple", messages=[{"role": "system", "content": _SYSTEM},
                                                        {"role": "user", "content": text}])

    def two_calls(img):
        client.chat.completions.create(model="lem-vision", messages=[])
        client.chat.completions.create(model="lem-vision", messages=[])

    def outer(post):
        def _draft():
            return _call_llm(model="lem-medium", messages=[{"role": "user", "content": post}])
        return _draft()

    class Judge:
        def ask(self):
            return self._client.responses.create(model="lem-research", input="q")

    def not_a_prompt(text):
        client.embeddings.create(model="lem-embedding", input=text)
        client.audio.speech.create(model="lem-tts", input=text, voice="x")
        _complete([])
    '''


class TestScan:
    def test_finds_endpoints_wrappers_nesting_and_indexes(self, tmp_path):
        repo = _tree(tmp_path, {"src/cqc_lem/m.py": _MODULE})

        keys = {s.key: s.kind for s in pr.scan(repo, ("src/cqc_lem",))}

        assert keys == {
            "cqc_lem.m:classify#0": "_call_llm",
            "cqc_lem.m:two_calls#0": "chat.completions.create",
            "cqc_lem.m:two_calls#1": "chat.completions.create",
            "cqc_lem.m:outer._draft#0": "_call_llm",
            "cqc_lem.m:Judge.ask#0": "responses.create",
        }

    def test_embeddings_tts_and_an_unscoped_wrapper_name_are_not_prompts(self, tmp_path):
        repo = _tree(tmp_path, {"src/cqc_lem/m.py": _MODULE})

        functions = {s.function for s in pr.scan(repo, ("src/cqc_lem",))}

        assert "not_a_prompt" not in functions

    def test_a_scoped_wrapper_counts_only_in_its_module(self, tmp_path):
        body = "def f():\n    return _complete([])\n"
        repo = _tree(tmp_path, {"src/cqc_lem/utilities/ai/curated_commentary.py": body,
                                "src/cqc_lem/other.py": body})

        keys = [s.key for s in pr.scan(repo, ("src/cqc_lem",))]

        assert keys == ["cqc_lem.utilities.ai.curated_commentary:f#0"]

    def test_scripts_are_named_by_path_and_pycache_is_skipped(self, tmp_path):
        repo = _tree(tmp_path, {
            "scripts/tool.py": "def go(c):\n    c.chat.completions.create(model='x', messages=[])\n",
            "scripts/__pycache__/tool.py": "def go(c):\n    c.chat.completions.create()\n",
        })

        assert [s.key for s in pr.scan(repo, ("scripts",))] == ["scripts.tool:go#0"]


class TestRegistry:
    def _write(self, tmp_path, body):
        path = tmp_path / "registry.yaml"
        path.write_text(textwrap.dedent(body), encoding="utf-8")
        return path

    def test_loads_valid_entries(self, tmp_path):
        path = self._write(tmp_path, """
            sites:
              a:m:f#0: {id: p.one, status: planned}
              a:m:g#0: {id: p.two, status: "exempt:wrapper"}
              a:m:g#1: {id: p.two, status: "exempt:fallback", shares_prompt_with: "a:m:g#0"}
            """)

        assert set(pr.load_registry(path)) == {"a:m:f#0", "a:m:g#0", "a:m:g#1"}

    @pytest.mark.parametrize("entry, message", [
        ("{status: planned}", "needs an `id`"),
        ("{id: p, status: shipped}", "unknown status"),
        ('{id: p, status: "exempt:"}', "unknown status"),
    ])
    def test_rejects_malformed_entries(self, tmp_path, entry, message):
        path = self._write(tmp_path, f"sites:\n  a:m:f#0: {entry}\n")

        with pytest.raises(ValueError, match=message):
            pr.load_registry(path)

    def test_rejects_a_prompt_id_reused_without_declaring_it(self, tmp_path):
        path = self._write(tmp_path, """
            sites:
              a:m:f#0: {id: p.one, status: planned}
              a:m:g#0: {id: p.one, status: planned}
            """)

        with pytest.raises(ValueError, match="used by both"):
            pr.load_registry(path)

    def test_families_are_read_from_the_same_file(self, tmp_path):
        path = self._write(tmp_path, """
            families:
              comment: {rubric: comment, assertions: [{type: no_preamble}]}
            sites: {}
            """)

        assert pr.load_families(path) == {"comment": {"rubric": "comment",
                                                      "assertions": [{"type": "no_preamble"}]}}
        assert pr.load_families(self._write(tmp_path, "")) == {}

    def test_empty_file_is_an_empty_registry(self, tmp_path):
        assert pr.load_registry(self._write(tmp_path, "")) == {}

    def test_drift_names_both_directions(self):
        sites = [pr.CallSite("m", "f", 0, 1, "_call_llm"), pr.CallSite("m", "g", 0, 2, "_call_llm")]

        unregistered, stale = pr.drift(sites, {"m:f#0": {}, "m:gone#0": {}})

        assert unregistered == ["m:g#0"]
        assert stale == ["m:gone#0"]

    def test_tracked_prompts_skip_exempt_and_shared_sites(self):
        sites = [pr.CallSite("m", "f", 0, 1, "k"), pr.CallSite("m", "f", 1, 2, "k"),
                 pr.CallSite("m", "w", 0, 3, "k")]
        registry = {
            "m:f#0": {"id": "p.f", "status": "planned"},
            "m:f#1": {"id": "p.f", "status": "exempt:fallback", "shares_prompt_with": "m:f#0"},
            "m:w#0": {"id": "p.w", "status": "exempt:wrapper"},
        }

        tracked = pr.tracked_prompts(sites, registry)

        assert list(tracked) == ["p.f"]
        assert tracked["p.f"][0].index == 0


class TestSourceHash:
    def _hash(self, tmp_path, body, function="classify"):
        repo = _tree(tmp_path, {"src/cqc_lem/m.py": body})
        return pr.source_hash(pr.CallSite("cqc_lem.m", function, 0, 1, "_call_llm"), repo)

    def test_a_docstring_or_comment_edit_does_not_move_it(self, tmp_path):
        base = self._hash(tmp_path, _MODULE)
        edited = _MODULE.replace('"""Doc."""', '"""A different doc."""  # and a comment')

        assert self._hash(tmp_path, edited) == base

    def test_editing_a_constant_the_function_reads_moves_it(self, tmp_path):
        base = self._hash(tmp_path, _MODULE)
        edited = _MODULE.replace("You classify posts.", "You classify LinkedIn posts.")

        assert self._hash(tmp_path, edited) != base

    def test_editing_the_body_moves_it(self, tmp_path):
        base = self._hash(tmp_path, _MODULE)

        assert self._hash(tmp_path, _MODULE.replace("lem-simple", "lem-medium")) != base

    def test_an_unrelated_function_edit_does_not_move_it(self, tmp_path):
        base = self._hash(tmp_path, _MODULE)

        assert self._hash(tmp_path, _MODULE.replace("lem-vision", "lem-image")) == base

    def test_a_nested_call_hashes_its_outer_function_and_a_method_its_own(self, tmp_path):
        assert self._hash(tmp_path, _MODULE, "outer._draft").startswith("sha256:")
        assert self._hash(tmp_path, _MODULE, "Judge.ask").startswith("sha256:")

    def test_a_vanished_function_is_a_lookup_error(self, tmp_path):
        with pytest.raises(LookupError):
            self._hash(tmp_path, _MODULE, "gone")

    def test_a_script_site_resolves_under_scripts(self, tmp_path):
        repo = _tree(tmp_path, {"scripts/tool.py": "def go():\n    pass\n"})

        assert pr.source_hash(pr.CallSite("scripts.tool", "go", 0, 1, "k"), repo).startswith("sha256:")


class TestInventory:
    def test_rows_carry_version_source_and_verdicts(self):
        sites = [pr.CallSite("m", "f", 0, 9, "k"), pr.CallSite("m", "w", 0, 3, "k")]
        registry = {"m:f#0": {"id": "p.f", "family": "classifier", "status": "planned"},
                    "m:w#0": {"id": "p.w", "status": "exempt:wrapper"}}
        lock = {"p.f": {"version": 3, "content_hash": "sha256:x"}}
        state = {"p.f@3": {"openai/gpt-oss-20b": {"verdict": "pass"}}}

        text = pr.render_inventory(sites, registry, lock, state)

        assert "2 LLM call sites (exempt 1, planned 1)" in text
        assert "| `p.f` | `m` | `f` | classifier | planned | 3 | rendered text | " \
               "openai/gpt-oss-20b: pass |" in text
        assert "| `p.w` | `m` | `w` | — | exempt:wrapper | — | — | — |" in text
        assert ":9" not in text  # line numbers would go stale on any edit above the call

    def test_unmeasured_and_source_versioned(self):
        text = pr.render_inventory([pr.CallSite("m", "f", 0, 1, "k")],
                                   {"m:f#0": {"id": "p", "status": "planned"}},
                                   {"p": {"version": 1, "content_hash": None}}, {})

        assert "| 1 | source | not yet measured |" in text

    def test_load_json_of_a_missing_file_is_empty(self, tmp_path):
        assert pr.load_json(tmp_path / "absent.json") == {}


class TestCli:
    def test_scan_prints_every_site(self, capsys):
        assert pr.main(["--scan"]) == 0
        assert "cqc_lem.utilities.ai.ai_helper:post_is_relevant#0" in capsys.readouterr().out

    def test_check_reports_drift_and_fails(self, monkeypatch, capsys):
        monkeypatch.setattr(pr, "load_registry", lambda: {"m:gone#0": {}})

        assert pr.main(["--check"]) == 1
        out = capsys.readouterr().out
        assert "STALE m:gone#0" in out
        assert "UNREGISTERED cqc_lem.utilities.ai.ai_helper:post_is_relevant#0" in out

    def test_inventory_writes_the_file(self, monkeypatch, tmp_path):
        target = tmp_path / "docs" / "inventory.md"
        monkeypatch.setattr(pr, "INVENTORY_PATH", target)

        assert pr.main(["--inventory"]) == 0
        assert target.read_text(encoding="utf-8").startswith("# Prompt inventory")


class TestTheRealTree:
    """The guard: CI fails the moment a prompt goes untracked."""

    def test_every_llm_call_site_has_a_registry_entry(self):
        unregistered, stale = pr.drift(pr.scan(), pr.load_registry())

        assert not unregistered, (
            f"New LLM call site(s) with no prompt registry entry: {unregistered}. Add each to "
            "tests/benchmarks/prompts/registry.yaml (docs/prompt-evals.md), then run "
            "`python scripts/prompt_capture.py --write`."
        )
        assert not stale, (
            f"Registry entries whose call site is gone or moved: {stale}. Re-key them to what "
            "`python scripts/prompt_registry.py --scan` prints."
        )

    def test_every_tracked_prompt_still_resolves_its_source(self):
        for site, _entry in pr.tracked_prompts(pr.scan(), pr.load_registry()).values():
            pr.source_hash(site)

    def test_the_committed_inventory_is_current(self):
        expected = pr.render_inventory(pr.scan(), pr.load_registry(), pr.load_json(pr.LOCK_PATH),
                                       pr.load_json(pr.STATE_PATH))

        assert pr.INVENTORY_PATH.read_text(encoding="utf-8") == expected, (
            "docs/prompt-evals/inventory.md is stale — run `python scripts/prompt_capture.py --write`."
        )
