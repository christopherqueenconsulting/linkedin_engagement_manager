"""Wave-1 prompt datasets, graders and judge rubrics (docs/prompt-evals.md §3-4).

The guard for every `evaluated` prompt: its dataset is big enough, balanced, covers the edge cases,
renders through the REAL production builder into a suite the harness accepts, and every row's canned
answer clears its graders — so the phase-3 dry-run is green for a reason, not by accident. Also the
model-grader rubrics and the calibration sets the judge is checked against.
"""

import collections
import json
import pathlib
import re
import sys

import pytest

pytestmark = pytest.mark.unit

_ROOT = pathlib.Path(__file__).resolve().parents[3]
_SCRIPTS = _ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import benchmark_models as bm  # noqa: E402
import prompt_capture as pc  # noqa: E402
import prompt_registry as pr  # noqa: E402

MIN_ROWS = 20
#: Edge cases every evaluated prompt must cover; promo content applies where a CTA exists.
REQUIRED_TAGS = {"edge:long", "edge:non_english", "adversarial:injection"}
PROMO_FAMILIES = {"comment", "own_comment", "post_longform"}
MIN_FAIL_SHARE = 0.4
RUBRIC_DIR = pr.PROMPTS_DIR / "rubrics"

EVALUATED = {pid: entry for pid, (_site, entry)
             in pr.tracked_prompts(pr.scan(), pr.load_registry()).items()
             if entry["status"] == "evaluated"}
FAMILIES = pr.load_families()


@pytest.fixture(scope="module")
def suites():
    """Render every evaluated prompt once — this runs the real builders under capture."""
    return {pid: bm.load_prompt_suite(pc.render_suite(entry), name=pid)
            for pid, entry in EVALUATED.items()}


def test_the_wave_one_prompts_are_evaluated():
    assert set(EVALUATED) >= {
        "comment.feed", "comment.seed", "post.thought_leadership", "dm.nurture",
        "classify.post_relevance", "classify.lead_intent", "classify.dm_reply_intent",
        "judge.authenticity",
    }


@pytest.mark.parametrize("pid", sorted(EVALUATED))
class TestDataset:
    def test_shape(self, pid):
        entry = EVALUATED[pid]
        rows = pc.load_dataset(entry)
        personas = pc.load_personas()

        assert entry.get("capture"), f"{pid}: an evaluated prompt needs a capture block"
        assert entry.get("family") in FAMILIES, f"{pid}: family {entry.get('family')!r} has no graders"
        ids = [r["id"] for r in rows]
        assert len(ids) == len(set(ids)), f"{pid}: duplicate row ids"
        assert sum(1 for r in rows if not r.get("expect_no_request")) >= MIN_ROWS
        for row in rows:
            assert row.get("source") in ("synthetic", "golden"), row["id"]
            assert isinstance(row.get("canned", {}).get("output"), str), row["id"]
            for name in re.findall(r'"\$(?:persona|synthesis)": "([^"]+)"', json.dumps(row)):
                assert name in personas, f"{pid}/{row['id']}: unknown persona {name!r}"

    def test_edge_case_coverage(self, pid):
        entry = EVALUATED[pid]
        tags = {t for row in pc.load_dataset(entry) for t in row.get("tags") or []}
        required = set(REQUIRED_TAGS)
        if entry["family"] in PROMO_FAMILIES:
            required.add("mix:promo")

        assert required <= tags, f"{pid}: missing edge-case tags {sorted(required - tags)}"

    def test_classifier_labels_are_balanced(self, pid):
        if EVALUATED[pid]["family"] not in ("classifier", "judge"):
            pytest.skip("not a labelled family")
        counts = collections.Counter(r["labels"]["expected"] for r in pc.load_dataset(EVALUATED[pid]))

        assert len(counts) >= 2
        assert max(counts.values()) - min(counts.values()) <= 1, f"{pid}: labels unbalanced {counts}"

    def test_renders_through_the_real_builder(self, pid, suites):
        suite = suites[pid]
        rows = {r["id"]: r for r in pc.load_dataset(EVALUATED[pid])}

        assert len(suite["cases"]) >= MIN_ROWS
        assert suite["tier"].startswith("lem-")
        for case in suite["cases"]:
            assert case["assertions"], case["id"]
            assert not set(case["params"]) & pc.SAMPLING_PARAMS, case["id"]
        skipped = {rid for rid, r in rows.items() if r.get("expect_no_request")}
        assert skipped.isdisjoint({c["id"] for c in suite["cases"]})

    def test_every_canned_answer_clears_its_graders(self, pid, suites):
        for case in suites[pid]["cases"]:
            result = bm.evaluate_case(case, case["canned"]["output"])

            assert result["contract_passes"], (case["id"], result["contract_failures"])
            assert not result["repairable_failures"], (case["id"], result["repairable_failures"])

    def test_the_rubric_exists_for_a_judged_family(self, pid, suites):
        rubric = suites[pid]["judge_rubric"]
        if rubric:
            assert (RUBRIC_DIR / f"{rubric}.md").exists()


def test_rendering_is_deterministic():
    entry = EVALUATED["comment.feed"]

    first, second = pc.render_suite(entry), pc.render_suite(entry)

    assert [c["messages"] for c in first["cases"]] == [c["messages"] for c in second["cases"]]


def test_an_empty_post_short_circuits_before_any_model_call(suites):
    rows = pc.load_dataset(EVALUATED["comment.feed"])

    assert any(r.get("expect_no_request") for r in rows)


class TestRubrics:
    def _criteria(self, name):
        text = (RUBRIC_DIR / f"{name}.md").read_text(encoding="utf-8")
        return re.findall(r"^## (\w+)$", text, flags=re.M)

    @pytest.mark.parametrize("name", sorted({f["rubric"] for f in FAMILIES.values() if f.get("rubric")}))
    def test_rubric_and_calibration_agree(self, name):
        criteria = self._criteria(name)
        path = RUBRIC_DIR / f"{name}.calibration.jsonl"
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

        assert 3 <= len(criteria) <= 5
        # The judge's priced ceiling assumes a bounded rubric (benchmark_prompts.RUBRIC_MAX_CHARS).
        assert len((RUBRIC_DIR / f"{name}.md").read_text(encoding="utf-8")) <= 4000
        assert len(rows) >= MIN_ROWS
        assert len({r["id"] for r in rows}) == len(rows)
        for row in rows:
            assert set(row["labels"]) == set(criteria), row["id"]
            assert set(row["labels"].values()) <= {"pass", "fail"}, row["id"]
            expected = "pass" if all(v == "pass" for v in row["labels"].values()) else "fail"
            assert row["overall"] == expected, row["id"]
        fails = sum(1 for r in rows if r["overall"] == "fail") / len(rows)
        assert fails >= MIN_FAIL_SHARE, f"{name}: only {fails:.0%} fails — calibration needs negatives"
