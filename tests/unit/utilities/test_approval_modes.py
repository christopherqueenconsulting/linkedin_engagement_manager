"""Issue #2624: the per-user approval mode resolver."""
from unittest.mock import patch

import pytest

from cqc_lem.utilities.approval_modes import (
    APPROVAL_MODE_ENV,
    APPROVAL_MODE_FIELDS,
    env_forces_review,
    may_auto_approve,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in APPROVAL_MODE_ENV.values():
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("field", APPROVAL_MODE_FIELDS)
def test_only_an_explicit_auto_approve_opens_the_gate(field):
    assert may_auto_approve(1, field, {field: "auto_approve"}) is True
    assert may_auto_approve(1, field, {field: "pre_review"}) is False
    assert may_auto_approve(1, field, {field: "garbage"}) is False
    assert may_auto_approve(1, field, {}) is False


@pytest.mark.parametrize("field", APPROVAL_MODE_FIELDS)
@pytest.mark.parametrize("value,forces", [("0", True), ("false", True), (" No ", True),
                                          ("OFF", True), ("", False), ("1", False),
                                          ("true", False), ("maybe", False)])
def test_env_only_forces_review_with_an_off_value(monkeypatch, field, value, forces):
    monkeypatch.setenv(APPROVAL_MODE_ENV[field], value)
    assert env_forces_review(field) is forces
    assert may_auto_approve(1, field, {field: "auto_approve"}) is (not forces)


def test_env_can_never_switch_a_lane_on(monkeypatch):
    monkeypatch.setenv(APPROVAL_MODE_ENV["nurture_dm_mode"], "true")
    assert may_auto_approve(1, "nurture_dm_mode", {"nurture_dm_mode": "pre_review"}) is False


def test_reads_preferences_when_none_supplied():
    with patch("cqc_lem.utilities.db.get_engagement_preferences",
               return_value={"group_post_mode": "auto_approve"}) as get:
        assert may_auto_approve(7, "group_post_mode") is True
    get.assert_called_once_with(7)


def test_an_unreadable_preference_row_means_review():
    with patch("cqc_lem.utilities.db.get_engagement_preferences", return_value=None):
        assert may_auto_approve(7, "group_post_mode") is False
