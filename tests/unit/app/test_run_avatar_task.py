"""Unit tests for the avatar sample-rendering Celery task (issue #744)."""
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_M = "cqc_lem.app.run_avatar"
_SUCCEEDED = {"id": 12, "status": "succeeded", "model_ref": "owner/lora:v1",
              "approval_status": "pending"}
_SAMPLES = [{"label": "headshot", "path": "images/avatar_samples/12/headshot.webp"}]


def _run(avatar, samples=_SAMPLES, count_regeneration=False):
    from cqc_lem.app.run_avatar import render_avatar_samples_task
    with patch(f"{_M}.get_avatar_training", return_value=avatar), \
         patch("cqc_lem.utilities.avatar.samples.render_avatar_samples", return_value=samples), \
         patch(f"{_M}.update_avatar_samples") as store, \
         patch(f"{_M}.set_avatar_approval") as approval:
        result = render_avatar_samples_task(12, 7, count_regeneration=count_regeneration)
    return result, store, approval


class TestRenderAvatarSamplesTask:
    def test_stores_samples_and_returns_the_count(self):
        result, store, _ = _run(_SUCCEEDED)
        assert result == 1
        store.assert_called_once_with(12, _SAMPLES, count_regeneration=False)

    def test_regeneration_is_counted_against_the_cap(self):
        _, store, _ = _run(_SUCCEEDED, count_regeneration=True)
        assert store.call_args.kwargs["count_regeneration"] is True

    def test_pending_avatar_is_not_re_reset(self):
        _, _, approval = _run(_SUCCEEDED)
        approval.assert_not_called()

    def test_new_samples_invalidate_an_earlier_verdict(self):
        """The user approved the images they SAW — these are different images."""
        from cqc_lem.utilities.db import AVATAR_APPROVAL_PENDING
        _, _, approval = _run({**_SUCCEEDED, "approval_status": "approved"})
        approval.assert_called_once_with(7, 12, AVATAR_APPROVAL_PENDING)

    def test_missing_avatar_returns_none(self):
        result, store, _ = _run(None)
        assert result is None
        store.assert_not_called()

    @pytest.mark.parametrize("override", [{"status": "processing"}, {"model_ref": None}])
    def test_unfinished_training_renders_nothing(self, override):
        result, store, _ = _run({**_SUCCEEDED, **override})
        assert result is None
        store.assert_not_called()

    def test_no_samples_leaves_the_avatar_un_approvable(self):
        """Storing an empty list would look like 'previewed'; leaving it alone keeps the
        approve endpoint's 400 in place."""
        result, store, _ = _run(_SUCCEEDED, samples=[])
        assert result == 0
        store.assert_not_called()
