"""Chrome capacity is a FIXED pool, so an un-quit driver is a permanent loss, not a slowdown.

`SE_NODE_MAX_SESSIONS` is about eight slots shared across the four Selenium lanes. A task that
acquires a session and does not give it back removes one of those slots until the worker process is
recycled — and `worker_max_tasks_per_child` only recycles BETWEEN tasks, so a leak accumulates.
Teardown is written out by hand in every task body today; nothing stopped the next one forgetting.
"""

import ast
import pathlib
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_MOD = "cqc_lem.app.run_automation"
_SRC = pathlib.Path("src/cqc_lem/app/run_automation.py")


class TestBrowserSession:
    def test_yields_the_four_values_and_quits(self):
        from cqc_lem.app.run_automation import browser_session

        driver = MagicMock()
        with patch(f"{_MOD}.get_current_profile",
                   return_value=(driver, "wait", "e@x.com", "profile")) as acquire, \
             patch(f"{_MOD}.quit_gracefully") as quit_g:
            with browser_session(7, "Sync Groups") as session:
                assert session.driver is driver
                assert session.user_email == "e@x.com"

        acquire.assert_called_once_with(user_id=7, session_name="Sync Groups")
        quit_g.assert_called_once_with(driver)

    def test_it_still_unpacks_as_the_historical_4_tuple(self):
        from cqc_lem.app.run_automation import browser_session

        driver = MagicMock()
        with patch(f"{_MOD}.get_current_profile", return_value=(driver, "w", "e", "p")), \
             patch(f"{_MOD}.quit_gracefully"):
            with browser_session(7, "X") as session:
                d, w, email, profile = session
        assert (d, w, email, profile) == (driver, "w", "e", "p")

    def test_the_session_is_returned_even_when_the_body_raises(self):
        from cqc_lem.app.run_automation import browser_session

        driver = MagicMock()

        def blow_up():
            with browser_session(7, "X"):
                raise RuntimeError("task body failed")

        with patch(f"{_MOD}.get_current_profile", return_value=(driver, "w", "e", "p")), \
             patch(f"{_MOD}.quit_gracefully") as quit_g:
            with pytest.raises(RuntimeError):
                blow_up()

        quit_g.assert_called_once_with(driver)

    def test_kwargs_reach_get_current_profile(self):
        """measurement_only and force_refresh are the real per-caller variations."""
        from cqc_lem.app.run_automation import browser_session

        with patch(f"{_MOD}.get_current_profile",
                   return_value=(MagicMock(), "w", "e", "p")) as acquire, \
             patch(f"{_MOD}.quit_gracefully"):
            with browser_session(3, "Post Stats", measurement_only=True):
                pass

        assert acquire.call_args.kwargs["measurement_only"] is True

    def test_an_acquisition_failure_is_not_swallowed(self):
        """Each caller answers a 429 / auth wall differently; guessing one would be worse."""
        from cqc_lem.app.run_automation import browser_session

        def acquire_fails():
            with browser_session(7, "X"):
                pass

        with patch(f"{_MOD}.get_current_profile", side_effect=RuntimeError("429")), \
             patch(f"{_MOD}.quit_gracefully") as quit_g:
            with pytest.raises(RuntimeError):
                acquire_fails()

        # Nothing was acquired, so nothing may be torn down.
        quit_g.assert_not_called()


class TestNoTaskLeaksAChromeSlot:
    """The guard that did not exist: every acquisition is answered by a teardown."""

    @staticmethod
    def _acquiring_functions():
        tree = ast.parse(_SRC.read_text())
        out = []
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if fn.name in ("get_current_profile", "browser_session"):
                continue
            acquires = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
                        and isinstance(n.func, ast.Name) and n.func.id == "get_current_profile"]
            uses_cm = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
                       and isinstance(n.func, ast.Name) and n.func.id == "browser_session"]
            if acquires or uses_cm:
                out.append((fn, bool(acquires), bool(uses_cm)))
        return out

    def test_there_are_acquisition_sites_to_check(self):
        """Guards against this whole class silently passing on an empty list."""
        assert len(self._acquiring_functions()) > 10

    def test_every_acquisition_is_paired_with_a_teardown(self):
        offenders = []
        for fn, acquires, uses_cm in self._acquiring_functions():
            if uses_cm and not acquires:
                continue  # browser_session owns the teardown structurally
            quits_in_finally = any(
                isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == "quit_gracefully"
                for t in ast.walk(fn) if isinstance(t, ast.Try)
                for s in t.finalbody for n in ast.walk(s))
            quits_anywhere = any(
                isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == "quit_gracefully" for n in ast.walk(fn))
            if not (quits_in_finally or quits_anywhere):
                offenders.append(fn.name)
        assert offenders == [], (
            "these acquire a Chrome session and never give it back — each one permanently costs "
            f"a slot out of SE_NODE_MAX_SESSIONS: {offenders}")
