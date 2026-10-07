"""scripts/simulate_post_rhythm.py — the $0 offline gauntlet harness for the post rotation.

The offline run is the real ``generate_image_for_post`` with Stage 1, the brief and the AI scene
replaced by deterministic stand-ins; nothing reaches a network or a database.
"""

import json
import pathlib
import sys

import pytest

pytestmark = pytest.mark.unit

_SCRIPTS = pathlib.Path(__file__).resolve().parents[3] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import simulate_post_rhythm as sim  # noqa: E402

_POSTS = [
    "Late invoices cost our agency 38% of last quarter's revenue. Deposits fixed it.",
    "Most founders think they need a bigger team. I think the real problem is that nobody "
    "owns the calendar. Give one person the calendar.",
    "We moved support to a shared queue. Response times fell and weekends came back.",
    "Our routing change saved $12,000 in model spend this year. Quality held steady.",
]


def test_an_offline_run_renders_every_post_and_writes_the_contact_sheet(tmp_path):
    posts = tmp_path / "posts.json"
    posts.write_text(json.dumps(_POSTS[:3] + [{"text": _POSTS[3]}, {"text": ""}]))
    assets = tmp_path / "assets"
    out = tmp_path / "sheet.png"
    code = sim.main([str(posts), "--user-id", "3", "--assets-dir", str(assets), "--offline",
                     "--out", str(out), "--byline", "Jane Doe"])
    assert code == 0
    from PIL import Image

    with Image.open(out) as sheet:
        assert sheet.size[0] == sim.COLUMNS * sim.THUMB_WIDTH + (sim.COLUMNS + 1) * sim.GUTTER
    receipts = list((assets / "images" / "post_previews" / "3").glob("*.brief.json"))
    assert len(receipts) == 4, "every post left a receipt for the next one's rotation"
    treatments = [json.loads(r.read_text())["rhythm"]["treatment"] for r in receipts]
    assert len(set(treatments)) >= 2


def test_rows_are_labelled_and_a_failed_post_shows_its_reason(tmp_path):
    rows = [{"index": 1, "path": None, "reason": "Image generation failed", "rhythm": {}},
            {"index": 2, "path": None, "reason": None,
             "rhythm": {"treatment": "typeset_card", "panel": "off_white", "grade": "warm_dusk",
                        "layout": "split_top"}}]
    assert sim.label_for(rows[0]) == "#1  no image"
    assert sim.label_for(rows[1]) == "#2  typeset_card · off white · warm dusk · split top"
    assert sim.contact_sheet(rows, str(tmp_path / "s.png")).endswith("s.png")


def test_load_posts_refuses_anything_but_a_list(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"text": "x"}))
    with pytest.raises(ValueError):
        sim.load_posts(str(bad))


def test_the_offline_stat_is_one_the_validator_accepts():
    from cqc_lem.utilities.ai.image_graphics import validate_graphic_facts

    stat = sim._offline_stat(_POSTS[0])
    assert stat and stat["unit"] == "%" and stat["value"] == "38"
    assert validate_graphic_facts({"thesis_stat": stat}, _POSTS[0]).get("stat")
    assert sim._offline_stat("No figures here at all.") is None
