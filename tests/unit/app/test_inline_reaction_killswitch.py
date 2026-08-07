"""Inline reactions stand down while issue #816 is open.

LinkedIn's SDUI drifted away from all three aria-label anchors `react_to_post_inline` keys on, so
reactions stopped landing (63 selector misses in 48h) while each miss still burned
MAX_WAIT_RETRY x ~5s inside a Selenium session holding a Grid slot. These tests pin the two things
that must stay true while it is off: the reaction path is not entered at all, and commenting is
completely unaffected by the stand-down.
"""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_MOD = "cqc_lem.app.run_automation"


class TestInlineReactionKillSwitch:
    def test_disabled_never_touches_the_reaction_flow(self):
        """The point of the switch is the wall clock, not just the outcome: entering
        `react_to_post_inline` at all costs the retry time even when every locator misses.
        """
        with patch(f"{_MOD}.INLINE_REACTIONS_ENABLED", False), \
             patch(f"{_MOD}.react_to_post_inline") as react:
            from cqc_lem.app import run_automation
            assert run_automation.INLINE_REACTIONS_ENABLED is False
            react.assert_not_called()

    def test_standing_down_is_debug_not_a_warning(self):
        """A deliberate stand-down is working behaviour. Warning it every card is exactly the
        expected-no-op that the recurrence rule escalates into a filed defect.
        """
        import inspect

        from cqc_lem.app import run_automation
        src = inspect.getsource(run_automation._engage_card)
        assert "if not INLINE_REACTIONS_ENABLED:" in src
        gate = src.split("if not INLINE_REACTIONS_ENABLED:", 1)[1].split("elif", 1)[0]
        assert "log_debug" in gate, "the stand-down must be DEBUG"
        assert "log_warning" not in gate and "log_error" not in gate

    def test_default_is_on_now_that_the_flow_is_verified(self):
        """The #816 stand-down is lifted: the real break was the card walk (#901), and the fixed
        flow was verified END-TO-END on a live session ('no reaction' -> 'Insightful' on a real
        post). Shipping it off would leave a working feature disabled by default.
        """
        from cqc_lem.utilities.env_constants import INLINE_REACTIONS_ENABLED
        assert INLINE_REACTIONS_ENABLED is True

    def test_the_flag_survives_as_a_tourniquet(self):
        """Kept rather than deleted: it is the one-env-var stand-down for the NEXT SDUI rotation.
        Flipping a variable beats shipping a revert while the feed is misbehaving.
        """
        import inspect

        from cqc_lem.app import run_automation
        assert "INLINE_REACTIONS_ENABLED" in inspect.getsource(run_automation._engage_card)

    def test_commenting_is_not_gated_on_the_switch(self):
        """Reactions were always non-fatal to commenting; turning them off must not change that."""
        import inspect

        from cqc_lem.app import run_automation
        src = inspect.getsource(run_automation._engage_card)
        gate_idx = src.index("if not INLINE_REACTIONS_ENABLED:")
        post_idx = src.index("post_comment_inline")
        assert post_idx > gate_idx, "sanity: the comment call follows the reaction gate"
        # The comment call must sit OUTSIDE the gated branch — same indentation as the gate itself.
        comment_line = [ln for ln in src[gate_idx:].splitlines() if "post_comment_inline" in ln][0]
        gate_line = [ln for ln in src.splitlines() if "if not INLINE_REACTIONS_ENABLED:" in ln][0]
        indent = lambda s: len(s) - len(s.lstrip())
        assert indent(comment_line) == indent(gate_line), (
            "commenting must not be nested under the reaction gate")
