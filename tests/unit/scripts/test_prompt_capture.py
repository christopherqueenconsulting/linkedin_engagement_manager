"""Prompt capture and versioning (docs/prompt-evals.md).

Pins three contracts of `scripts/prompt_capture.py`: capture records what a REAL builder sends while
every hermetic guard holds; a prompt's version moves exactly when its text (or, before it is
captured, its source) does; and the committed lock and captures match the tree — the check that
makes an unversioned prompt edit fail CI.
"""

import json
import os
import pathlib
import subprocess
import sys
import types

import pytest

pytestmark = pytest.mark.unit

_ROOT = pathlib.Path(__file__).resolve().parents[3]
_SCRIPTS = _ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import prompt_capture as pc  # noqa: E402
import prompt_registry as pr  # noqa: E402


@pytest.fixture
def fake_builders(monkeypatch):
    """Register a throwaway module of builders the capture can target by dotted path."""
    module = types.ModuleType("fake_prompt_builders")

    def classify(text):
        from cqc_lem.utilities.ai.client import client
        response = client.chat.completions.create(
            model="lem-simple", temperature=0.37, max_tokens=3,
            messages=[{"role": "system", "content": "Answer yes or no."},
                      {"role": "user", "content": text}])
        return response.choices[0].message.content

    def silent(text):
        return text

    def second_client(text):
        import openai
        openai.OpenAI(api_key="x")

    def reads_db(text):
        import mysql.connector
        mysql.connector.connect(host="db")

    def reads_env(text):
        return os.environ.get("OPENAI_API_KEY"), os.environ.get("SOME_HOST_SECRET")

    for fn in (classify, silent, second_client, reads_db, reads_env):
        setattr(module, fn.__name__, fn)
    monkeypatch.setitem(sys.modules, "fake_prompt_builders", module)
    return module


class TestRecordCalls:
    def test_records_the_exact_request_and_answers_with_the_canned_reply(self, fake_builders):
        calls = pc.record_calls("fake_prompt_builders.classify", {"text": "hi"}, reply="yes")

        assert calls == [{"model": "lem-simple", "temperature": 0.37, "max_tokens": 3,
                          "messages": [{"role": "system", "content": "Answer yes or no."},
                                       {"role": "user", "content": "hi"}]}]

    def test_a_second_openai_client_is_refused(self, fake_builders):
        import openai

        with pytest.raises(openai.APIConnectionError):
            pc.record_calls("fake_prompt_builders.second_client", {"text": "x"})

    def test_a_database_connection_is_refused(self, fake_builders):
        import mysql.connector

        with pytest.raises(mysql.connector.Error):
            pc.record_calls("fake_prompt_builders.reads_db", {"text": "x"})

    def test_host_secrets_are_not_visible_and_the_environment_is_restored(self, fake_builders,
                                                                          monkeypatch):
        monkeypatch.setenv("SOME_HOST_SECRET", "s3cret")
        seen = {}
        original = fake_builders.reads_env
        monkeypatch.setattr(fake_builders, "reads_env", lambda text: seen.update(v=original(text)))

        pc.record_calls("fake_prompt_builders.reads_env", {"text": "x"})

        assert seen["v"] == (pc.CAPTURE_ENV["OPENAI_API_KEY"], None)
        assert os.environ["SOME_HOST_SECRET"] == "s3cret"

    def test_target_must_be_dotted(self):
        with pytest.raises(ValueError, match="dotted"):
            pc.record_calls("classify", {})


class TestCaptureEntry:
    def test_builds_the_record(self, fake_builders):
        record = pc.capture_entry({"id": "p.classify", "capture": {
            "target": "fake_prompt_builders.classify", "canonical": {"text": "hi"}, "reply": "no"}})

        assert record["prompt_id"] == "p.classify"
        assert record["tier"] == "lem-simple"
        assert record["messages"][1] == {"role": "user", "content": "hi"}
        assert record["params"] == {"max_tokens": 3, "model": "lem-simple", "temperature": 0.37}
        assert record["content_hash"].startswith("sha256:")

    def test_a_builder_that_sends_nothing_is_never_written(self, fake_builders):
        with pytest.raises(RuntimeError, match="made no LLM request"):
            pc.capture_entry({"id": "p.silent", "capture": {
                "target": "fake_prompt_builders.silent", "canonical": {"text": "x"}}})


class TestContentHash:
    _CALL = {"model": "lem-simple", "max_tokens": 3, "temperature": 0.2,
             "messages": [{"role": "user", "content": "x"}]}

    def test_sampling_and_attribution_do_not_move_it(self):
        resampled = {**self._CALL, "temperature": 0.9, "top_p": 0.5,
                     "extra_body": {"metadata": {"user_id": 7}}}

        assert pc.content_hash(resampled) == pc.content_hash(self._CALL)

    @pytest.mark.parametrize("change", [
        {"max_tokens": 4}, {"model": "lem-medium"}, {"response_format": {"type": "json_object"}},
        {"messages": [{"role": "user", "content": "y"}]},
    ])
    def test_text_and_contract_parameters_move_it(self, change):
        assert pc.content_hash({**self._CALL, **change}) != pc.content_hash(self._CALL)


class TestNextLockEntry:
    def test_a_new_prompt_starts_at_version_one(self):
        entry = pc.next_lock_entry(None, "sha256:s", None, "2026-10-08", "PR #1")

        assert entry == {"version": 1, "source_hash": "sha256:s", "content_hash": None,
                         "changed_in": "PR #1", "history": []}

    def test_unchanged_is_identical(self):
        prev = pc.next_lock_entry(None, "sha256:s", "sha256:c", "2026-10-01")

        assert pc.next_lock_entry(prev, "sha256:s", "sha256:c", "2026-10-08", "PR #2") == prev

    def test_an_uncaptured_prompt_bumps_on_its_source(self):
        prev = pc.next_lock_entry(None, "sha256:s1", None, "2026-10-01")

        entry = pc.next_lock_entry(prev, "sha256:s2", None, "2026-10-08", "PR #2")

        assert entry["version"] == 2
        assert entry["history"] == [{"version": 1, "hash": "sha256:s1", "retired": "2026-10-08"}]
        assert entry["changed_in"] == "PR #2"

    def test_a_captured_prompt_bumps_on_its_text_not_its_source(self):
        prev = pc.next_lock_entry(None, "sha256:s1", "sha256:c1", "2026-10-01")

        refactor = pc.next_lock_entry(prev, "sha256:s2", "sha256:c1", "2026-10-08")
        reworded = pc.next_lock_entry(prev, "sha256:s1", "sha256:c2", "2026-10-08")

        assert refactor["version"] == 1 and refactor["source_hash"] == "sha256:s2"
        assert reworded["version"] == 2 and reworded["history"][0]["hash"] == "sha256:c1"

    def test_becoming_captured_records_the_hash_without_a_bump(self):
        prev = pc.next_lock_entry(None, "sha256:s", None, "2026-10-01")

        entry = pc.next_lock_entry(prev, "sha256:s", "sha256:c", "2026-10-08")

        assert entry["version"] == 1 and entry["content_hash"] == "sha256:c"


class TestLockChecks:
    def test_lock_problems_name_every_kind_of_drift(self):
        committed = {"a": {"version": 1}, "b": {"version": 1}, "gone": {"version": 1}}
        current = {"a": {"version": 1}, "b": {"version": 2}, "new": {"version": 1}}

        problems = pc.lock_problems(committed, current)

        assert problems == ["b: prompt changed since it was versioned",
                            "gone: in prompts.lock.json but no longer a tracked prompt",
                            "new: tracked prompt missing from prompts.lock.json"]

    def test_a_hand_edited_hash_without_a_bump_fails_against_the_base(self):
        base = {"p": {"version": 2, "source_hash": "s1", "content_hash": "c1"},
                "q": {"version": 1, "source_hash": "s1", "content_hash": None}}
        head = {"p": {"version": 2, "source_hash": "s1", "content_hash": "c2"},
                "q": {"version": 2, "source_hash": "s2", "content_hash": None},
                "r": {"version": 1, "source_hash": "s", "content_hash": None}}

        assert pc.base_version_problems(base, head) == ["p: content_hash changed but version stayed 2"]


class TestBuildLockAndWrite:
    def test_build_lock_versions_every_tracked_prompt(self):
        lock = pc.build_lock({}, "2026-10-08", captures={"classify.lead_intent": {
            "content_hash": "sha256:c"}})
        tracked = pr.tracked_prompts(pr.scan(), pr.load_registry())

        assert set(lock) == set(tracked)
        assert lock["classify.lead_intent"]["content_hash"] == "sha256:c"
        assert all(entry["version"] == 1 for entry in lock.values())

    def test_write_captures_replaces_the_directory(self, monkeypatch, tmp_path):
        monkeypatch.setattr(pc, "CAPTURED_DIR", tmp_path)
        (tmp_path / "stale.json").write_text("{}")

        pc.write_captures({"p.one": {"prompt_id": "p.one"}})

        assert sorted(p.name for p in tmp_path.iterdir()) == ["p.one.json"]
        assert json.loads((tmp_path / "p.one.json").read_text()) == {"prompt_id": "p.one"}

    def test_write_mode_writes_lock_captures_and_inventory(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(pr, "LOCK_PATH", tmp_path / "lock.json")
        monkeypatch.setattr(pr, "INVENTORY_PATH", tmp_path / "inventory.md")
        monkeypatch.setattr(pc, "CAPTURED_DIR", tmp_path / "captured")
        monkeypatch.setattr(pc, "capture_all", lambda: {"classify.lead_intent": {
            "prompt_id": "classify.lead_intent", "content_hash": "sha256:c"}})

        assert pc.main(["--write", "--changed-in", "PR #9"]) == 0

        lock = json.loads((tmp_path / "lock.json").read_text())
        assert lock["classify.lead_intent"]["changed_in"] == "PR #9"
        assert (tmp_path / "captured" / "classify.lead_intent.json").exists()
        assert (tmp_path / "inventory.md").exists()
        assert "captured, 0 bumped" in capsys.readouterr().out

    def test_check_mode_fails_on_a_stale_lock(self, monkeypatch, capsys):
        monkeypatch.setattr(pr, "load_json", lambda path: {})

        assert pc.main(["--check"]) == 1
        assert "Run: python scripts/prompt_capture.py --write" in capsys.readouterr().out


@pytest.fixture(scope="module")
def captures():
    """Capture every real prompt once for the module — it runs the real builders."""
    return pc.capture_all()


class TestTheRealTree:
    """The version guard: a prompt edit that was not re-captured and re-versioned fails CI."""

    def test_every_captured_prompt_matches_its_committed_capture(self, captures):
        committed = {p.stem: json.loads(p.read_text(encoding="utf-8"))
                     for p in pc.CAPTURED_DIR.glob("*.json")}

        assert set(committed) == set(captures), "run `python scripts/prompt_capture.py --write`"
        for pid, record in captures.items():
            assert committed[pid] == record, (
                f"{pid}: the prompt production sends no longer matches its capture. Run "
                "`python scripts/prompt_capture.py --write` and commit the diff + version bump."
            )

    def test_the_lock_versions_the_tree(self, captures):
        committed = pr.load_json(pr.LOCK_PATH)
        current = pc.build_lock(committed, "2000-01-01", captures=captures)

        problems = pc.lock_problems(committed, current)

        assert not problems, (
            "prompts.lock.json is stale — a prompt changed without a version bump:\n  "
            + "\n  ".join(problems) + "\nRun `python scripts/prompt_capture.py --write`."
        )

    def test_no_version_was_rewritten_without_a_bump_against_the_base_branch(self):
        rel = pr.LOCK_PATH.relative_to(_ROOT).as_posix()
        base_ref = os.environ.get("PROMPT_LOCK_BASE_REF", "origin/main")
        shown = subprocess.run(["git", "show", f"{base_ref}:{rel}"], cwd=_ROOT,
                               capture_output=True, text=True, check=False)
        if shown.returncode != 0:
            pytest.skip(f"{base_ref} has no {rel} in this checkout (shallow clone or first PR)")

        problems = pc.base_version_problems(json.loads(shown.stdout), pr.load_json(pr.LOCK_PATH))

        assert not problems, problems
