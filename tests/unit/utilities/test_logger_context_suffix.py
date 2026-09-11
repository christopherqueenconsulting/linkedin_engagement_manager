"""The structured context reaches the file line (issue #2057).

Every call site passes `user_id=` / `post_id=` / `profile_url=` … and the PostHog backend got it,
but `_LevelFormatter` rendered only the message into `/opt/lem/logs/cqc_lem_*.log`, so a run could
not be attributed to a person, a post or a user from the persistent log. The documented keys now
trail the line as ` | key=value …`. These pin the three properties the change must keep: the suffix
renders at every level, undocumented keys and `exc` never render, and the level prefix the
escalation contract greps is byte-identical when there is no context.
"""

import logging

import pytest

from cqc_lem.utilities.logger import _LevelFormatter, _extra

pytestmark = pytest.mark.unit


def _record(level: int, **extra) -> logging.LogRecord:
    record = logging.LogRecord(name="cqc-lem", level=level, pathname="/app/src/cqc_lem/feed.py",
                               lineno=42, msg="commented on post", args=(), exc_info=None,
                               func="do_thing")
    for key, value in _extra(**extra).items():
        setattr(record, key, value)
    return record


class TestTheContextTrailsTheLine:
    @pytest.mark.parametrize("level", [logging.INFO, logging.WARNING, logging.ERROR,
                                       logging.CRITICAL, logging.DEBUG])
    def test_documented_keys_render_in_fixed_order_at_every_level(self, level):
        line = _LevelFormatter().format(_record(level, post_id=101, user_id=1,
                                                profile_url="https://www.linkedin.com/in/x/"))
        assert line.endswith(" | user_id=1 post_id=101 profile_url=https://www.linkedin.com/in/x/")
        assert "commented on post | user_id=1" in line

    def test_a_record_with_no_context_renders_exactly_as_before(self):
        fmt = _LevelFormatter()
        line = fmt.format(_record(logging.WARNING))
        assert line.endswith(" WARNING [feed.py:42]: commented on post")
        assert " | " not in line

    def test_the_warning_prefix_is_byte_identical_with_context(self):
        # The escalation contract and hand greps key on `WARNING [file:line]: message`; the suffix
        # sits AFTER it.
        line = _LevelFormatter().format(_record(logging.WARNING, user_id=7))
        assert " WARNING [feed.py:42]: commented on post | user_id=7" in line

    def test_undocumented_keys_and_exc_never_render(self):
        record = _record(logging.INFO, user_id=1, secret="hunter2", cookie="li_at=x")
        record.exc = RuntimeError("boom")
        line = _LevelFormatter().format(record)
        assert line.endswith(" | user_id=1")
        assert "hunter2" not in line and "li_at" not in line and "boom" not in line

    def test_empty_and_none_values_are_skipped_and_whitespace_is_collapsed(self):
        line = _LevelFormatter().format(_record(logging.INFO, user_id=None, task_name="",
                                                action_type="comment", ai_model="lem  medium\n"))
        assert line.endswith(" | action_type=comment ai_model=lem medium")

    def test_the_suffix_helper_is_the_one_place_the_shape_lives(self):
        record = _record(logging.INFO, http_status=503, api_provider="litellm", duration_ms=12)
        assert _LevelFormatter.context_suffix(record) == \
            "duration_ms=12 api_provider=litellm http_status=503"
        assert _LevelFormatter.context_suffix(_record(logging.INFO)) == ""
