"""Evaluation of the ICP rubric: per-segment/band acceptance, exploration slice, back-test (#2655 Phase 5).

Pure — no DB, no Selenium. Callers hand in rows and facts. Nothing here changes who is sent an
invite beyond the exploration slice, which only runs when the per-user `icp_builder_enabled` flag is
on and never lowers the user's `min_connection_icp_score` floor.

Acceptance comes from `connection_requests.accepted` (#2658 P3), set only when an existing visit
happened to see the 1st-degree badge. A 0 therefore means "not seen accepted", NOT "declined", so the
rates are lower bounds and every report says so. Replies are not tracked per request yet and are
reported as not measured.
"""

import random
from datetime import date
from typing import Iterable, Optional

from cqc_lem.utilities.icp_rubric import IcpDefinition, has_facts, score_person
from cqc_lem.utilities.lead_scoring import profile_slug

BAND_TOP = "top"
BAND_MID = "mid"
BAND_LOW = "low"
BANDS = (BAND_TOP, BAND_MID, BAND_LOW)
TOP_BAND_MIN = 70
MID_BAND_MIN = 40

# Under this many SENT invites a rate is noise: the report shows n and "insufficient data".
MIN_SAMPLE = 20
INSUFFICIENT = "insufficient data"
NOT_MEASURED = "not measured"

# Share of filed targets drawn from the band just below the top one. Written into the reason so the
# report can tell an exploration row from a ranked one without a new column.
EXPLORATION_FRACTION = 0.10
EXPLORATION_MARK = "[exploration]"

BACKTEST_DAYS = 90


def score_band(score: Optional[int]) -> str:
    """`top` (>=70), `mid` (40-69) or `low`. An unscored row is `low`'s caller's to skip."""
    value = int(score or 0)
    if value >= TOP_BAND_MIN:
        return BAND_TOP
    return BAND_MID if value >= MID_BAND_MIN else BAND_LOW


def _slice(rows: list) -> dict:
    sent = len(rows)
    accepted = sum(1 for r in rows if r.get("accepted"))
    enough = sent >= MIN_SAMPLE
    return {"sent": sent, "accepted": accepted,
            "acceptance_rate": round(accepted / sent, 3) if enough else None,
            "note": None if enough else INSUFFICIENT}


def outcome_report(rows: Iterable[dict]) -> dict:
    """Acceptance per segment and per score band, over SENT rubric-scored invites.

    Args:
        rows: `connection_requests` rows with `status`, `icp_score`, `segment_id`,
            `icp_rubric_version`, `icp_reason` and `accepted`.

    Returns:
        `{"total", "by_segment", "by_band", "exploration", "replies", "caveat"}`. Rows without
        `icp_rubric_version` were never rubric-scored and are excluded. A bucket under
        `MIN_SAMPLE` carries `acceptance_rate: None` and the `insufficient data` note.
    """
    scored = [r for r in rows or []
              if str(r.get("status") or "").lower() == "sent" and r.get("icp_rubric_version")]
    segments: dict = {}
    bands: dict = {b: [] for b in BANDS}
    explore: list = []
    for r in scored:
        segments.setdefault(str(r.get("segment_id") or "unknown"), []).append(r)
        bands[score_band(r.get("icp_score"))].append(r)
        if EXPLORATION_MARK in str(r.get("icp_reason") or ""):
            explore.append(r)
    return {
        "total": _slice(scored),
        "by_segment": {k: _slice(v) for k, v in sorted(segments.items())},
        "by_band": {b: _slice(bands[b]) for b in BANDS},
        "exploration": _slice(explore),
        "replies": NOT_MEASURED,
        "caveat": "accepted is set only when a visit already saw the 1st-degree badge; "
                  "rates are lower bounds",
    }


def backtest(accepted_contacts: Iterable[dict], definition: IcpDefinition, facts_by_url: dict) -> dict:
    """How many past accepted contacts the CURRENT rubric would rank in the top band.

    Informational, no pass mark. A contact with no scraped facts cannot be scored and is counted as
    `unscored`, never as a miss; an ineligible one (hard filter / no segment) counts as `ineligible`.

    Args:
        accepted_contacts: rows with `recipient_profile_url` for accepted invites in the window.
        definition: The user's ICP.
        facts_by_url: `get_profile_facts` output (matched on the profile slug).
    """
    by_slug = {profile_slug(k): v for k, v in (facts_by_url or {}).items() if profile_slug(k)}
    out = {"accepted": 0, "top_band": 0, "mid_band": 0, "low_band": 0, "ineligible": 0,
           "unscored": 0}
    for row in accepted_contacts or []:
        url = str(row.get("recipient_profile_url") or "")
        out["accepted"] += 1
        facts = by_slug.get(profile_slug(url))
        if facts is None or not has_facts(facts):
            out["unscored"] += 1
            continue
        result = score_person(definition, facts, url)
        if not result.eligible:
            out["ineligible"] += 1
            continue
        out[f"{score_band(result.total)}_band"] += 1
    scored = out["accepted"] - out["unscored"]
    out["top_band_share"] = round(out["top_band"] / scored, 3) if scored else None
    out["window_days"] = BACKTEST_DAYS
    return out


def exploration_count(limit: int, user_id: int, day: date,
                      fraction: float = EXPLORATION_FRACTION) -> int:
    """How many of `limit` slots go to exploration. Seeded on (user, day): a retry never re-rolls.

    The whole part of `limit * fraction` is certain; the remainder is a seeded coin flip, so a small
    daily budget still explores about `fraction` of the time instead of never.
    """
    expected = max(0, int(limit)) * fraction
    base = int(expected)
    rng = random.Random(f"icp-explore:{user_id}:{day.isoformat()}")
    return base + (1 if rng.random() < expected - base else 0)


def split_exploration(ranked: list, limit: int, user_id: int, day: date,
                      fraction: float = EXPLORATION_FRACTION) -> list:
    """Pick `limit` of the best-first `ranked` candidates, ~`fraction` from the next-lower band.

    `ranked` items need `.score` and `.person_key`. The exploration slice comes from the nearest
    non-empty band below the best candidate's band, among the candidates not already picked.
    Every candidate is already above the caller's floor, so the floor holds. With no lower-band
    candidate the whole limit goes to the ranked top.

    Returns:
        `(picked, explored_keys)`: the ranked picks followed by the exploration picks, and the set
        of `person_key`s that were drawn as exploration.
    """
    limit = max(0, int(limit))
    n_explore = min(exploration_count(limit, user_id, day, fraction), limit)
    if not ranked or n_explore == 0:
        return list(ranked[:limit]), set()
    main_n = limit - n_explore
    main = list(ranked[:main_n])
    rest = list(ranked[main_n:])
    best_band = score_band(ranked[0].score)
    below = BANDS[BANDS.index(best_band) + 1:]
    pool: list = []
    for band in below:
        pool = [c for c in rest if score_band(c.score) == band]
        if pool:
            break
    if not pool:
        return list(ranked[:limit]), set()
    rng = random.Random(f"icp-explore-pick:{user_id}:{day.isoformat()}")
    drawn = rng.sample(pool, min(n_explore, len(pool)))
    explored = {c.person_key for c in drawn}
    filler = [c for c in rest if c.person_key not in explored][:limit - len(main) - len(drawn)]
    return main + filler + drawn, explored
