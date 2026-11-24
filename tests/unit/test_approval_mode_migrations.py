"""Issues #2624/#2664: the approval-mode migrations keep every existing user's behaviour.

The migration is the only thing that decides what a user who existed before the deploy gets, so the
fixture here reads the SQL itself: each column defaults to review for a NEW user, and the literal
backfill puts every EXISTING row on what its lane did before the column existed.
"""
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_DIR = Path(__file__).resolve().parents[2] / "compose/local/database/migrations"

# Lane -> (migration, what the lane did BEFORE the column: it either always gated or sent directly).
_TODAY = {
    "nurture_dm_mode": ("V20261123232134__add_outbound_approval_modes.sql", "pre_review"),
    "artifact_dm_mode": ("V20261123232134__add_outbound_approval_modes.sql", "pre_review"),
    "group_post_mode": ("V20261123232134__add_outbound_approval_modes.sql", "auto_approve"),
    "followup_step_mode": ("V20261124004719__add_followup_appreciation_approval_modes.sql",
                           "auto_approve"),
    "appreciation_dm_mode": ("V20261124004719__add_followup_appreciation_approval_modes.sql",
                             "auto_approve"),
}


@pytest.mark.parametrize("column", sorted(_TODAY))
def test_a_new_user_defaults_to_review(column):
    sql = (_DIR / _TODAY[column][0]).read_text()
    assert re.search(rf"ADD COLUMN\s+{column}\s+ENUM\('pre_review','auto_approve'\)\s+NOT NULL\s+"
                     rf"DEFAULT 'pre_review'", sql), column


@pytest.mark.parametrize("column", sorted(_TODAY))
def test_every_existing_user_keeps_todays_behaviour(column):
    migration, expected = _TODAY[column]
    sql = (_DIR / migration).read_text()
    update = sql[sql.index("UPDATE engagement_preferences"):]
    assert re.search(rf"{column}\s*=\s*'{expected}'", update), (column, expected)
    # An unconditional backfill: every row, no WHERE that could leave a user on the column default.
    assert "WHERE" not in update.upper().split(";")[0]


def test_the_backfill_literals_match_the_resolver_defaults():
    from cqc_lem.platform.db.repositories.users import _ENGAGEMENT_DEFAULTS
    from cqc_lem.utilities.approval_modes import APPROVAL_MODE_FIELDS

    assert set(_TODAY) == set(APPROVAL_MODE_FIELDS)
    for column in APPROVAL_MODE_FIELDS:
        assert _ENGAGEMENT_DEFAULTS[column] == "pre_review"
