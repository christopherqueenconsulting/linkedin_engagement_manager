"""Unit tests for logger structured-context coercion + log_warning exc handling."""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


def test_extra_coerces_non_primitives_and_drops_none():
    from cqc_lem.utilities.logger import _extra

    class Boom(Exception):
        pass

    out = _extra(user_id=5, exc=Boom("bad selector"), flag=True, note=None, name="x")
    assert out["user_id"] == 5 and out["flag"] is True and out["name"] == "x"
    assert "note" not in out                       # None is dropped
    assert isinstance(out["exc"], str) and "bad selector" in out["exc"]  # object coerced to str


def test_log_warning_routes_exc_to_exc_info_not_property():
    from cqc_lem.utilities import logger as L
    with patch.object(L.logger, "warning") as w:
        L.log_warning("boom", exc=ValueError("x"), user_id=1, action_type="comment")
    _, kwargs = w.call_args
    assert kwargs.get("exc_info") is not None       # stack trace captured
    assert "exc" not in kwargs["extra"]             # not shipped as a raw attribute
    assert kwargs["extra"]["user_id"] == 1 and kwargs["extra"]["action_type"] == "comment"


def test_log_warning_without_exc_still_works():
    from cqc_lem.utilities import logger as L
    with patch.object(L.logger, "warning") as w:
        L.log_warning("plain", user_id=2)
    _, kwargs = w.call_args
    assert kwargs.get("exc_info") is None
    assert kwargs["extra"]["user_id"] == 2
