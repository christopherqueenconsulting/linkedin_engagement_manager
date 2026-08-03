import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "todo_issue_sweep", Path(__file__).parents[2] / "scripts" / "todo_issue_sweep.py")
sweep = importlib.util.module_from_spec(_SPEC)
sys.modules["todo_issue_sweep"] = sweep
_SPEC.loader.exec_module(sweep)


class TestClassifyLine:
    def test_token_in_python_comment(self):
        assert sweep.classify_line("    # TODO: Fix this method") == "TODO"

    def test_token_in_js_comment(self):
        assert sweep.classify_line("  // FIXME handle the null case") == "FIXME"

    def test_token_outside_comment_is_ignored(self):
        assert sweep.classify_line("todo_list = get_todos()") is None
        assert sweep.classify_line("hack = build_hack_free_payload()") is None

    def test_phrase_matches_in_docstring(self):
        assert sweep.classify_line('    """Not yet implemented — returns an empty dict."""') == "STUB"

    def test_no_op_stub_phrase(self):
        assert sweep.classify_line("    Not yet implemented — no-op stub.") == "STUB"

    def test_live_grounding_phrase(self):
        line = "# NOTE: needs a supervised live-grounding pass on a real account"
        assert sweep.classify_line(line) == "STUB"

    def test_plain_line_is_none(self):
        assert sweep.classify_line("return moments") is None

    def test_case_insensitive_token(self):
        assert sweep.classify_line("# todo lowercase counts too") == "TODO"


class TestFingerprint:
    def test_stable_across_whitespace_and_punctuation(self):
        a = sweep.fingerprint("src/a.py", "# TODO: Fix   this method!")
        b = sweep.fingerprint("src/a.py", "#  todo Fix this method")
        assert a == b

    def test_path_scoped(self):
        assert (sweep.fingerprint("src/a.py", "# TODO: x")
                != sweep.fingerprint("src/b.py", "# TODO: x"))

    def test_text_change_changes_it(self):
        assert (sweep.fingerprint("src/a.py", "# TODO: x")
                != sweep.fingerprint("src/a.py", "# TODO: y"))

    def test_prefix(self):
        assert sweep.fingerprint("a", "b").startswith(sweep.MARKER_PREFIX)


class TestParseGrep:
    def test_parses_and_classifies(self):
        out = "src/a.py:12:    # TODO: wire this up\nsrc/b.py:3:x = 1\n"
        hits = sweep.parse_grep(out)
        assert len(hits) == 1
        assert hits[0]["path"] == "src/a.py"
        assert hits[0]["line"] == 12
        assert hits[0]["kind"] == "TODO"

    def test_excluded_paths_dropped(self):
        out = "src/cqc_lem/streamlit/pages/x.py:1:# TODO: implement auth\n"
        assert sweep.parse_grep(out) == []

    def test_colon_in_text_survives(self):
        out = "src/a.py:5:# TODO: handle http://example.com properly\n"
        hits = sweep.parse_grep(out)
        assert "http://example.com" in hits[0]["text"]

    def test_garbage_lines_skipped(self):
        assert sweep.parse_grep("not-a-grep-line\n") == []
        assert sweep.parse_grep("") == []


class TestPlanActions:
    def _hit(self, path="src/a.py", text="# TODO: x", line=1):
        return {"path": path, "line": line, "text": text, "kind": "TODO",
                "marker": sweep.fingerprint(path, text)}

    def test_files_unfiled_and_skips_filed(self):
        hits = [self._hit(), self._hit(text="# TODO: y")]
        filed = {hits[0]["marker"]}
        actions = sweep.plan_actions(hits, filed)
        assert [a["action"] for a in actions] == ["skip", "create"]

    def test_cap_defers_the_rest(self):
        hits = [self._hit(text=f"# TODO: item {i}") for i in range(6)]
        actions = sweep.plan_actions(hits, set(), max_new=2)
        assert [a["action"] for a in actions].count("create") == 2
        assert [a["action"] for a in actions].count("deferred") == 4

    def test_same_marker_counted_once(self):
        hits = [self._hit(line=1), self._hit(line=99)]
        actions = sweep.plan_actions(hits, set())
        assert len(actions) == 1

    def test_summary_counts(self):
        hits = [self._hit()]
        assert "1 create" in sweep.summarize(sweep.plan_actions(hits, set()))


class TestBuildIssue:
    def _hit(self):
        text = "# TODO: wire the thing"
        return {"path": "src/a.py", "line": 7, "text": text, "kind": "TODO",
                "marker": sweep.fingerprint("src/a.py", text)}

    def test_title_carries_path_and_kind(self):
        title = sweep.build_title(self._hit())
        assert "src/a.py" in title and "TODO" in title
        assert len(title) <= sweep.MAX_TITLE_CHARS

    def test_body_carries_marker(self):
        hit = self._hit()
        assert hit["marker"] in sweep.build_body(hit)


class TestGitHubFailClosed:
    def test_search_failure_raises(self):
        gh = sweep.GitHubIssues()
        bad = MagicMock(returncode=1, stdout="", stderr="boom")
        with patch.object(gh, "_run", return_value=bad):
            with pytest.raises(RuntimeError):
                gh.is_filed("todo-sweep-abc")

    def test_marker_rechecked_in_body(self):
        gh = sweep.GitHubIssues()
        ok = MagicMock(returncode=0, stdout='[{"number": 1, "body": "other-marker"}]', stderr="")
        with patch.object(gh, "_run", return_value=ok):
            assert gh.is_filed("todo-sweep-abc") is False

    def test_apply_dry_run_files_nothing(self):
        gh = sweep.GitHubIssues()
        actions = [{"action": "create",
                    "hit": {"path": "src/a.py", "line": 1, "text": "# TODO: x", "kind": "TODO",
                            "marker": "todo-sweep-x"}}]
        with patch.object(gh, "create") as create:
            sweep.apply_actions(gh, actions, dry_run=True)
        create.assert_not_called()
