"""The `curated_source` event: declared in EVENTS, every breakdown property a STRING label."""

from unittest.mock import patch

import pytest

from cqc_lem.utilities import observability

pytestmark = pytest.mark.unit


def test_track_curated_source_emits_string_labels_and_zero_counts():
    with patch.object(observability.posthog, "capture") as capture, \
         patch.object(observability, "telemetry_muted", return_value=False):
        observability.track_curated_source("publish", "published", user_id=7,
                                           platform="linkedin", treatment="reshare")
    kwargs = capture.call_args.kwargs
    assert kwargs["event"] == "curated_source"
    assert kwargs["distinct_id"] == "7"
    props = kwargs["properties"]
    assert props["stage"] == "publish" and props["treatment"] == "reshare"
    assert props["reason"] == ""
    assert props["new"] == 0 and props["blocked"] == 0


def test_every_breakdown_is_a_label():
    spec = observability.EVENTS["curated_source"]
    labels = {f.name for f in spec.fields if f.filtered}
    assert {"stage", "status", "platform", "treatment", "reason"} <= labels
