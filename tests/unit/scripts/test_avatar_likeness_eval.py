"""Pins scripts/avatar_likeness_eval.py — the #1430 likeness-probe eval harness.

The CLI is the only thing that ever writes a verdict file into this PUBLIC repo, so the failure
mode that matters is a write that carries more than a verdict: a frame path, a name, a source
directory. `leaks_a_frame_path` is graded in the module's own tests; what is graded here is that
the CLI actually REFUSES on it — and that a real run stamps itself `"measured"` so it can never be
confused with the committed schema example.
"""

import importlib.util
import json
import pathlib
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_SCRIPT = pathlib.Path("scripts/avatar_likeness_eval.py")

_PROBE = "cqc_lem.utilities.avatar.likeness_probe.probe_avatar_likeness"


@pytest.fixture(scope="module")
def tool():
    spec = importlib.util.spec_from_file_location("avatar_likeness_eval", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _manifest(tmp_path, entries):
    """Write the labelled manifest and the frames it points at, and return its path."""
    for i, entry in enumerate(entries):
        frame = tmp_path / f"frame{i}.png"
        frame.write_bytes(f"pixels {i}".encode())
        entry.setdefault("frame", str(frame))
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(entries), encoding="utf-8")
    return str(path)


class TestManifest:
    def test_a_label_the_grader_cannot_read_stops_the_run(self, tool, tmp_path):
        manifest = _manifest(tmp_path, [{"label": "probably?"}])
        with pytest.raises(SystemExit):
            tool.main(["--manifest", manifest])

    def test_a_manifest_that_is_not_a_list_stops_the_run(self, tool, tmp_path):
        path = tmp_path / "manifest.json"
        path.write_text(json.dumps({"label": "present"}), encoding="utf-8")
        with pytest.raises(SystemExit):
            tool.main(["--manifest", str(path)])


class TestVerdictFile:
    def test_a_real_run_stamps_itself_measured_and_carries_no_frame_path(self, tool, tmp_path):
        manifest = _manifest(tmp_path, [
            {"label": "present", "gender_presentation": "man", "age_band": "40s"},
            {"label": "absent", "gender_presentation": "man", "age_band": "40s",
             "used_avatar": "false"},
        ])
        out = tmp_path / "verdicts.json"
        with patch(_PROBE, return_value={"checked": True, "present": True, "reason": "ok"}):
            assert tool.main(["--manifest", manifest, "--out", str(out)]) == 0
        payload = json.loads(out.read_text(encoding="utf-8"))
        # "measured" is what separates a run from the committed schema example — the fixture the
        # replay test grades declares "synthetic-schema-example" and must never be read as a rate.
        assert payload["source"] == "measured"
        assert payload["measured_frames"] == 2
        assert all("frame" not in record for record in payload["records"])
        assert payload["scores"]["overall"]["false_positive"] == 1

    def test_the_reason_is_left_out_unless_it_is_asked_for(self, tool, tmp_path):
        manifest = _manifest(tmp_path, [{"label": "present", "gender_presentation": "man"}])
        out = tmp_path / "verdicts.json"
        verdict = {"checked": True, "present": True, "reason": "a man is central"}
        with patch(_PROBE, return_value=verdict):
            tool.main(["--manifest", manifest, "--out", str(out)])
            assert "reason" not in json.loads(out.read_text(encoding="utf-8"))["records"][0]
            tool.main(["--manifest", manifest, "--out", str(out), "--include-reasons"])
        assert json.loads(out.read_text(encoding="utf-8"))["records"][0]["reason"] == verdict["reason"]

    def test_a_record_carrying_anything_else_refuses_the_write(self, tool, tmp_path):
        """The publish guard, at the only place that writes into a PUBLIC repo."""
        manifest = _manifest(tmp_path, [{"label": "present", "gender_presentation": "man"}])
        out = tmp_path / "verdicts.json"
        leaky = [{"frame_id": "a", "label": "present", "checked": True, "present": True,
                  "frame": "/home/owner/private/lora-01.png"}]
        with patch.object(tool, "run_eval", return_value=leaky):
            assert tool.main(["--manifest", manifest, "--out", str(out)]) == 2
        assert not out.exists()


class TestScorecard:
    def test_an_unmeasured_class_prints_as_unavailable_not_as_zero(self, tool, tmp_path, capsys):
        """An empty denominator is `None` — rendering it as 0.0% would report a perfect probe."""
        manifest = _manifest(tmp_path, [{"label": "present", "gender_presentation": "man"}])
        with patch(_PROBE, return_value={"checked": True, "present": True, "reason": "ok"}):
            tool.main(["--manifest", manifest])
        out = capsys.readouterr().out
        assert "false-positive rate (bad frame wrongly passed):    n/a (no frames in this class)" in out
        assert "sufficient to decide the hold default: no" in out

    def test_json_output_is_the_graded_scores(self, tool, tmp_path, capsys):
        manifest = _manifest(tmp_path, [{"label": "absent", "gender_presentation": "man"}])
        with patch(_PROBE, return_value={"checked": False, "present": None, "reason": "no clause"}):
            tool.main(["--manifest", manifest, "--json"])
        scores = json.loads(capsys.readouterr().out)
        assert scores["overall"]["unchecked"] == 1
        assert scores["overall"]["graded"] == 0
