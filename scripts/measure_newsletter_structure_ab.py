"""Measure what the newsletter structural checking side (issue #1435) actually moves.

#1284 shipped the structural floor as writer-side WORDING and measured it in a live A/B: the arm
carrying the explicit floor came back with LONGER paragraphs than the control (512/525 against
343/313) and opened inside the fold 0 times out of 2. The conclusion that audit drew — *a
writer-side instruction with no checking side does not hold* — is what #1435 implements, and this
script is how that claim gets numbers instead of a hope.

The arms differ by ONE environment variable, nothing else:

  control    NEWSLETTER_STRUCTURE_ENABLED=off  — the slop lint is the only grader, as before #1435
  treatment  NEWSLETTER_STRUCTURE_ENABLED=on   — the structural report shares the same bounded
                                                 regeneration budget

Everything else is held fixed per index: the same subject, the same blueprint (`select_blueprint`
with a seeded RNG), the same profile synthesis, the same models, the same attempt cap. The two arms
for index i are generated back to back so provider-side drift lands on both.

It writes nothing: no DB, no browser, no LinkedIn. It DOES spend real `lem-complex` generations
through the LiteLLM proxy — that is the measurement — so it needs `LITELLM_BASE_URL` /
`LITELLM_MASTER_KEY` in the environment and it reports the calls each arm spent.

    LITELLM_BASE_URL=http://127.0.0.1:4000 poetry run python \
        scripts/measure_newsletter_structure_ab.py --editions 4

Output goes to stdout because the report IS the product of this script.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from typing import Optional


def _bootstrap_src_path(script_path: str) -> Optional[str]:
    """Put the checkout's `src/` on `sys.path`, and return what was added.

    Piped into a container (`python - < scripts/…`) there is no script path — `__file__` is
    `<stdin>` — and the image installs `cqc_lem` into its own venv, so the guess is skipped rather
    than inserted. Same bootstrap as `scripts/measure_proof_gate_impact.py`.
    """
    if not script_path or script_path.startswith("<"):
        return None
    src = os.path.normpath(
        os.path.join(os.path.dirname(os.path.abspath(script_path)), "..", "src"))
    if not os.path.isdir(src):
        return None
    sys.path.insert(0, src)
    return src


_bootstrap_src_path(globals().get("__file__") or "")

# The subjects the arms are generated against. Fixed in code so a re-run is comparable to this one:
# they are ordinary editions for LEM's own newsletter niche, none of them chosen to favour an arm.
SUBJECTS = [
    "Why your best-performing LinkedIn post is the one you almost did not publish",
    "The three engagement metrics that predict pipeline, and the four that predict nothing",
    "What changed in LinkedIn's 2026 ranking, and what to stop doing about it",
    "How to run a 30-day content plan when you only have two hours a week",
    "The comment section is the product: turning replies into a repeatable motion",
    "Scheduling posts around your audience, not around your calendar",
]

# The author voice both arms write in. A synthesis is passed explicitly so the profile object is
# never dumped into the prompt (`content_alignment.voice_reference`), which is what lets this run
# without a database.
SYNTHESIS = (
    "Christopher Queen is a solutions architect and founder who builds AI automation for B2B "
    "consultancies. He writes plainly, from things he has shipped and measured: deployment "
    "pipelines, LLM cost routing, LinkedIn automation. He prefers a concrete number over an "
    "adjective, names the tradeoff he made, and is comfortable saying what did not work."
)

TOPIC = ("A weekly newsletter for consultants and founders on making LinkedIn produce pipeline "
         "without a content team.")


def _measure(body: str) -> dict:
    """The four structural measures this A/B is about, plus the slop verdict, from the shipped
    graders — `dwell_report` via `newsletter_structure_report`, and `lint_report`.
    """
    from cqc_lem.utilities.ai import slop_lint as _slop
    from cqc_lem.utilities.ai.content_framework import newsletter_structure_report
    # The report reads the enabled flag; grade the RESULT of both arms identically.
    prior = os.environ.pop("NEWSLETTER_STRUCTURE_ENABLED", None)
    try:
        report = newsletter_structure_report(body)
    finally:
        if prior is not None:
            os.environ["NEWSLETTER_STRUCTURE_ENABLED"] = prior
    m = report["metrics"]
    lint = _slop.lint_report(body, "newsletter")
    return {"words": m["words"], "opening_chars": m["hook_chars"],
            "longest_paragraph": m["longest_paragraph_chars"], "has_list": m["has_list"],
            "dwell": report["dwell_score"], "slop_hard": len(lint["hard"]),
            "passes": report["passes"],
            "failures": [f["check"] for f in report["failures"]]}


def _generate(subject: str, blueprint: dict, seed: int, structure_on: bool) -> dict:
    """One edition for one arm, with the calls it cost.

    `_call_llm` is wrapped rather than the proxy client because it is the seam every generation in
    `generate_newsletter_edition` goes through (draft, humanize, mechanical edit, retry) — the cost
    of an arm is exactly how many times it is entered.
    """
    from cqc_lem.utilities.ai import ai_helper
    from cqc_lem.utilities.ai import content_framework as fw
    os.environ["NEWSLETTER_STRUCTURE_ENABLED"] = "on" if structure_on else "off"
    calls = {"n": 0, "models": []}
    original = ai_helper._call_llm

    def _counting(**kwargs):
        calls["n"] += 1
        calls["models"].append(kwargs.get("model"))
        return original(**kwargs)

    # Every draft passes through the structural report on its way to being graded (both arms — the
    # report is CALLED in the control arm too, it just answers `checked: False`). Recording what it
    # was handed is what separates "the model wrote this" from "the checking side changed it".
    drafts = []
    original_report = fw.newsletter_structure_report

    def _recording(body):
        if not drafts or drafts[-1] != body:
            drafts.append(body)
        return original_report(body)

    # The temperature draws inside the generator are random; seeding per index makes the two arms
    # for one subject draw the same sequence, so temperature is not a difference between them.
    random.seed(seed)
    ai_helper._call_llm = _counting
    fw.newsletter_structure_report = _recording
    try:
        edition = ai_helper.generate_newsletter_edition(
            profile=None, topic=TOPIC, subject=subject, profile_synthesis=SYNTHESIS,
            blueprint=blueprint)
    finally:
        ai_helper._call_llm = original
        fw.newsletter_structure_report = original_report
    if not edition:
        return {"ok": False, "calls": calls["n"], "models": calls["models"]}
    out = _measure(edition["body"])
    out.update({"ok": True, "calls": calls["n"], "models": calls["models"],
                "drafts": len(drafts), "first_draft": _measure(drafts[0]) if drafts else None,
                "title": edition["title"], "body": edition["body"]})
    return out


def _arm_summary(rows: list) -> dict:
    """Per-arm aggregates over the editions that generated."""
    ok = [r for r in rows if r.get("ok")]
    if not ok:
        return {"n": 0}
    return {
        "n": len(ok),
        "words_mean": round(sum(r["words"] for r in ok) / len(ok)),
        "in_word_band": sum(1 for r in ok if 800 <= r["words"] <= 1200),
        "longest_paragraph_mean": round(sum(r["longest_paragraph"] for r in ok) / len(ok)),
        "longest_paragraph_max": max(r["longest_paragraph"] for r in ok),
        "no_wall_of_text": sum(1 for r in ok if r["longest_paragraph"] <= 300),
        "opening_within_fold": sum(1 for r in ok if r["opening_chars"] <= 210),
        "with_list_block": sum(1 for r in ok if r["has_list"]),
        "all_four_pass": sum(1 for r in ok if r["passes"]),
        "dwell_mean": round(sum(r["dwell"] for r in ok) / len(ok)),
        "slop_hard_total": sum(r["slop_hard"] for r in ok),
        "llm_calls": sum(r["calls"] for r in ok),
        "llm_calls_per_edition": round(sum(r["calls"] for r in ok) / len(ok), 2),
    }


def _print_report(control: list, treatment: list) -> None:
    """The per-edition table and the arm comparison, in the shape §4 of the audit doc records."""
    print("\nPer-edition measurements (deterministic graders, same as the audit scorecard)\n")
    header = f"{'arm':<10} {'#':<3} {'words':>6} {'open':>6} {'longest':>8} {'list':>5} {'dwell':>6} {'slopH':>6} {'calls':>6}"
    print(header)
    print("-" * len(header))
    for name, rows in (("control", control), ("treatment", treatment)):
        for i, r in enumerate(rows, start=1):
            if not r.get("ok"):
                print(f"{name:<10} {i:<3} {'GENERATION FAILED':>40} {r['calls']:>6}")
                continue
            print(f"{name:<10} {i:<3} {r['words']:>6} {r['opening_chars']:>6} "
                  f"{r['longest_paragraph']:>8} {'yes' if r['has_list'] else 'no':>5} "
                  f"{r['dwell']:>6} {r['slop_hard']:>6} {r['calls']:>6}")
    print("\nFirst draft vs kept draft (what the checking side changed, per arm)\n")
    for name, rows in (("control", control), ("treatment", treatment)):
        firsts = [r["first_draft"] for r in rows if r.get("ok") and r.get("first_draft")]
        if not firsts:
            continue
        print(f"{name}: first drafts -> " + str(_arm_summary(
            [dict(f, ok=True, calls=0) for f in firsts])))
    c, t = _arm_summary(control), _arm_summary(treatment)
    print("\nArm summary\n")
    print(f"{'measure':<28} {'control':>12} {'treatment':>12}")
    print("-" * 54)
    for key in ("n", "words_mean", "in_word_band", "longest_paragraph_mean",
                "longest_paragraph_max", "no_wall_of_text", "opening_within_fold",
                "with_list_block", "all_four_pass", "dwell_mean", "slop_hard_total",
                "llm_calls", "llm_calls_per_edition"):
        print(f"{key:<28} {str(c.get(key, '-')):>12} {str(t.get(key, '-')):>12}")


def main(argv: Optional[list] = None) -> int:
    """Run both arms and print the report. Returns non-zero only when an arm generated nothing."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--editions", type=int, default=4,
                        help="editions per arm (the issue's acceptance floor is 4)")
    parser.add_argument("--seed", type=int, default=1435,
                        help="base RNG seed; arm pairs share seed+index")
    parser.add_argument("--json-out", default=None,
                        help="optional path for the full per-edition JSON, bodies included")
    args = parser.parse_args(argv)

    from cqc_lem.utilities.ai.content_framework import select_blueprint

    count = max(1, min(len(SUBJECTS), args.editions))
    control, treatment = [], []
    for i in range(count):
        subject = SUBJECTS[i]
        random.seed(args.seed + i)
        blueprint = select_blueprint("newsletter", subject=subject)
        print(f"[{i + 1}/{count}] {subject[:70]}… (format={blueprint.get('format')})", flush=True)
        control.append(_generate(subject, blueprint, args.seed + i, structure_on=False))
        treatment.append(_generate(subject, blueprint, args.seed + i, structure_on=True))

    _print_report(control, treatment)
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump({"control": control, "treatment": treatment}, fh, indent=2)
        print(f"\nFull records written to {args.json_out}")
    return 0 if any(r.get("ok") for r in control) and any(r.get("ok") for r in treatment) else 1


if __name__ == "__main__":
    raise SystemExit(main())
