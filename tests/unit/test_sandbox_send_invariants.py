"""A sandbox account's sends are harmless by construction, and a new send path cannot skip the gate.

`users.account_kind = 'sandbox'` is what lets an operator approve items on an account safely: nothing
approved there may reach a real person, ever. Three layers hold that, and this file pins each:

1. **Every browser lane** asks `skip_browser_lane`, and `get_docker_driver` refuses a session, for a
   sandbox account — the existing lane suite (`app/test_engagement_mode_lanes.py`) is extended for it.
2. **Every LinkedIn API publish helper** (`poster.py`, `reshare.py`) opens with `guard_send`. This file
   walks those modules and fails, naming the function, when one that takes a `user_id` does not.
3. **The queue-fed send lanes** complete a sandbox row as a dry run BEFORE any gate or client call.

A function that publishes through the API and is called from somewhere new fails
`TestEveryApiPublishCallerIsRegistered` until someone names it and says why it is safe. The direction
of each failure is deliberate and stated: for SENDS an unreadable or unrecognised `account_kind` means
"do not send" (and does NOT complete a row as a dry run, which would lose a possible real send), while
for PERMISSIONS (`get_user_account_kind`) it reads `standard`.
"""

import ast
import importlib
import inspect
import pathlib
from contextlib import ExitStack
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.utilities.engagement_mode import (
    SANDBOX_DRY_RUN_MESSAGE,
    SandboxAccountRefused,
)

pytestmark = pytest.mark.unit

_SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "cqc_lem"
_MODE = "cqc_lem.utilities.engagement_mode.get_user_engagement_state"
_POSTER = "cqc_lem.utilities.linkedin.poster"
_RESHARE = "cqc_lem.utilities.linkedin.reshare"
_POST = "cqc_lem.app.engagement.posting"


def _state(kind: Any, mode: str = "automate") -> dict:
    return {"engagement_mode": mode, "subscription_status": "active",
            "subscription_tier": "professional", "account_kind": kind}


_UNSENDABLE = [
    pytest.param(_state("sandbox"), id="sandbox"),
    pytest.param(_state("sandbox", mode="suggest"), id="sandbox-suggest"),
    pytest.param(_state(None), id="kind-none"),
    pytest.param(_state(""), id="kind-empty"),
    pytest.param(_state("Sandbox"), id="kind-capitalised"),
    pytest.param(_state("sandbox "), id="kind-trailing-space"),
    pytest.param(_state("admin"), id="kind-unrecognised"),
    pytest.param({"engagement_mode": "automate", "subscription_status": "active",
                  "subscription_tier": "professional"}, id="kind-key-missing"),
    pytest.param(None, id="unreadable-none"),
]
# Unsendable, but NOT a sandbox account: these must never be completed as a dry run.
_UNREADABLE_KINDS = [pytest.param(p.values[0], id=p.id) for p in _UNSENDABLE
                     if p.id not in ("sandbox", "sandbox-suggest")]
_SENDABLE = [pytest.param(_state("standard"), id="standard"),
             pytest.param(_state("internal"), id="internal")]


def _functions(path: pathlib.Path) -> dict:
    return {n.name: n for n in ast.parse(path.read_text()).body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _call_names(node: ast.AST) -> list[str]:
    """Names called anywhere in `node`, in source order (`f(...)` and `x.f(...)` both give `f`)."""
    found = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            f = sub.func
            found.append(f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else "")
    return found


def _params(node: ast.AST) -> list[str]:
    return [a.arg for a in node.args.args + node.args.kwonlyargs]


def _first_index(source: str, *needles: str) -> int:
    hits = [source.index(n) for n in needles if n in source]
    return min(hits) if hits else -1


# ---------------------------------------------------------------------------
# The gate's own truth table
# ---------------------------------------------------------------------------

class TestTheGateTruthTable:
    @pytest.mark.parametrize("state,may_send,is_sandbox", [
        (_state("standard"), True, False), (_state("internal"), True, False),
        (_state("sandbox"), False, True),
        (_state("Sandbox"), False, False), (_state("sandbox "), False, False),
        (_state(None), False, False), (_state(""), False, False), (_state("garbage"), False, False),
        (None, False, False),
    ])
    def test_may_send_and_is_sandbox(self, state: Any, may_send: bool, is_sandbox: bool) -> None:
        from cqc_lem.utilities import engagement_mode as em

        with patch(_MODE, return_value=state):
            assert em.may_send(1) is may_send
            assert em.is_sandbox_account(1) is is_sandbox
            assert em.sandbox_dry_run(1, "t") is is_sandbox

    def test_a_read_fault_is_unreadable_not_standard(self) -> None:
        """The opposite direction from `get_user_account_kind`, which reads a fault as `standard`."""
        from cqc_lem.utilities import engagement_mode as em

        with patch(_MODE, side_effect=TypeError("db port unset")):
            assert em.read_account_kind(1) is None
            assert em.may_send(1) is False
            assert em.is_sandbox_account(1) is False

    def test_no_account_at_all_may_not_send(self) -> None:
        from cqc_lem.utilities import engagement_mode as em

        assert em.may_send(None) is False
        assert em.sandbox_dry_run(None, "t") is False

    @pytest.mark.parametrize("state", _UNSENDABLE)
    def test_guard_send_refuses_every_unsendable_state(self, state: Any) -> None:
        from cqc_lem.utilities import engagement_mode as em

        with patch(_MODE, return_value=state), pytest.raises(SandboxAccountRefused):
            em.guard_send(1, "poster.share_on_linkedin")

    @pytest.mark.parametrize("state", _SENDABLE)
    def test_guard_send_lets_standard_and_internal_through(self, state: Any) -> None:
        from cqc_lem.utilities import engagement_mode as em

        with patch(_MODE, return_value=state):
            em.guard_send(1, "poster.share_on_linkedin")  # must not raise

    def test_the_sending_kinds_are_exactly_standard_and_internal(self) -> None:
        from cqc_lem.utilities.engagement_mode import SENDING_ACCOUNT_KINDS

        assert SENDING_ACCOUNT_KINDS == frozenset({"standard", "internal"})

    def test_the_dry_run_writes_a_log_line_with_the_user_and_the_lane(self) -> None:
        from cqc_lem.utilities import engagement_mode as em

        with patch(_MODE, return_value=_state("sandbox")), \
             patch.object(em, "log_info") as info:
            assert em.sandbox_dry_run(7, "send_scheduled_dm") is True
        kwargs = info.call_args.kwargs
        assert kwargs["user_id"] == 7 and kwargs["task_name"] == "send_scheduled_dm"
        assert kwargs["action_type"] == "sandbox_dry_run"


# ---------------------------------------------------------------------------
# Layer 2: the API publish helpers
# ---------------------------------------------------------------------------

_PUBLISH_MODULES = {"poster": _SRC / "utilities" / "linkedin" / "poster.py",
                    "reshare": _SRC / "utilities" / "linkedin" / "reshare.py"}
# Public functions that call the LinkedIn API with a TOKEN and no user: each is reached only from a
# helper above that has already passed `guard_send`. A new entry is a claim that must stay true, so
# the parameters are asserted to hold no `user_id`.
_TOKEN_ONLY_HELPERS = {"upload_media", "upload_document", "upload_image_versioned"}
# Public functions that fetch a media or thumbnail URL and perform no LinkedIn action.
_READ_ONLY_PUBLIC = {"download_media", "fetch_thumbnail"}
_HTTP_MARKERS = ("requests.post", "requests.put", "requests.delete", "RestliClient(")


def _publish_source(name: str, fn: ast.AST, path: pathlib.Path) -> str:
    return ast.get_source_segment(path.read_text(), fn) or ""


class TestEveryApiPublishHelperOpensWithGuardSend:
    def _helpers(self) -> list[tuple[str, str, ast.AST, pathlib.Path]]:
        found = []
        for module, path in _PUBLISH_MODULES.items():
            for name, fn in _functions(path).items():
                found.append((module, name, fn, path))
        return found

    def test_the_walk_found_the_publish_helpers(self) -> None:
        gated = [n for (_m, n, fn, _p) in self._helpers() if "guard_send" in _call_names(fn)]
        assert len(gated) >= 9, f"the walk found only {gated}"

    def test_every_public_helper_with_a_user_id_that_calls_the_api_is_gated_first(self) -> None:
        offenders = []
        for module, name, fn, path in self._helpers():
            if name.startswith("_") or "user_id" not in _params(fn):
                continue
            source = _publish_source(name, fn, path)
            if not any(m in source for m in _HTTP_MARKERS) and "guard_linkedin" not in _call_names(fn):
                continue
            calls = _call_names(fn)
            if "guard_send" not in calls:
                offenders.append(f"{module}.{name}: takes a user_id and reaches LinkedIn, no guard_send")
                continue
            first_http = _first_index(source, *_HTTP_MARKERS, "RestliClient")
            if first_http != -1 and source.index("guard_send(") > first_http:
                offenders.append(f"{module}.{name}: guard_send comes after the first network call")
        assert not offenders, "\n\nThese LinkedIn publish helpers are not gated for a sandbox account:\n" \
            + "\n".join(f"  {o}" for o in offenders) + "\n"

    def test_every_other_public_function_that_calls_the_api_is_a_named_token_only_helper(self) -> None:
        offenders = []
        for module, name, fn, path in self._helpers():
            if name.startswith("_") or "user_id" in _params(fn):
                continue
            source = _publish_source(name, fn, path)
            if not any(m in source for m in _HTTP_MARKERS):
                continue
            if name not in _TOKEN_ONLY_HELPERS and name not in _READ_ONLY_PUBLIC:
                offenders.append(f"{module}.{name}")
        assert not offenders, (
            f"{offenders} call the LinkedIn API with no user_id and are not in _TOKEN_ONLY_HELPERS: "
            "give them a user_id and a guard_send, or name them here with the helper that guards them")

    def test_the_named_exceptions_still_exist_and_hold_no_user_id(self) -> None:
        names = {}
        for _module, name, fn, _path in self._helpers():
            names[name] = fn
        for helper in _TOKEN_ONLY_HELPERS:
            assert helper in names, f"{helper} is named but no longer exists"
            assert "user_id" not in _params(names[helper]), f"{helper} now takes a user_id: gate it"
        for helper in _READ_ONLY_PUBLIC:
            assert helper in names


# Every function outside poster.py/reshare.py that CALLS a guarded publish helper, with why each is safe
# for a sandbox account. A new caller fails the test below until it is named here.
_REGISTERED_CALLERS = {
    ("app/engagement/posting.py", "post_to_linkedin"):
        "completes a sandbox post as a dry run before any publish (sandbox_dry_run), and every "
        "share_* it calls opens with guard_send for an unreadable kind",
    ("app/engagement/feed.py", "auto_seed_comment_on_post"):
        "an API comment lane that asks is_suggest_only first: a sandbox account stores a suggestion",
    ("app/engagement/feed.py", "auto_second_wave_comment"):
        "an API comment lane that asks is_suggest_only first: a sandbox account stores a suggestion",
    ("app/run_curated_sources.py", "publish_curated_post"):
        "reached only from post_to_linkedin, after its sandbox dry run; the share_* helpers it calls "
        "open with guard_send",
}


class TestEveryApiPublishCallerIsRegistered:
    def _guarded_helpers(self) -> set[str]:
        names = set()
        for path in _PUBLISH_MODULES.values():
            for name, fn in _functions(path).items():
                if "guard_send" in _call_names(fn):
                    names.add(name)
        return names

    def test_every_caller_of_a_publish_helper_is_named(self) -> None:
        helpers = self._guarded_helpers()
        assert helpers, "no guarded helpers found"
        callers = set()
        for path in _SRC.rglob("*.py"):
            if path.name in ("poster.py", "reshare.py"):
                continue
            try:
                tree = ast.parse(path.read_text())
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                        and helpers & set(_call_names(node)):
                    callers.add((str(path.relative_to(_SRC)), node.name))
        unregistered = callers - set(_REGISTERED_CALLERS)
        assert not unregistered, (
            f"\n\nNew callers of a LinkedIn API publish helper, not registered as safe for a sandbox "
            f"account: {sorted(unregistered)}\nAdd each to _REGISTERED_CALLERS with the reason, after "
            "making sure a sandbox account cannot reach it.\n")

    def test_every_registered_caller_still_exists_and_still_calls_a_helper(self) -> None:
        helpers = self._guarded_helpers()
        for (rel, name), _why in _REGISTERED_CALLERS.items():
            fns = {n.name: n for n in ast.walk(ast.parse((_SRC / rel).read_text()))
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
            assert name in fns, f"{rel}:{name} is registered but gone"
            assert helpers & set(_call_names(fns[name])), f"{rel}:{name} no longer calls a helper"


def _call_minimal(module: str, name: str) -> None:
    fn = getattr(importlib.import_module(module), name)
    args = [1]
    for p in list(inspect.signature(fn).parameters.values())[1:]:
        if p.default is inspect.Parameter.empty:
            args.append(["a"] if "slide" in p.name else "x")
    fn(*args)


_HELPER_CALLS = [(_POSTER, n) for n in ("share_on_linkedin", "share_carousel_on_linkedin",
                                        "share_document_on_linkedin", "share_animated_image_on_linkedin",
                                        "comment_on_linkedin_post", "delete_linkedin_comment")] + \
                [(_RESHARE, n) for n in ("share_reshare_on_linkedin", "share_curated_image_on_linkedin",
                                         "share_article_on_linkedin")]


class _NetworkReached(Exception):
    pass


class TestThePublishHelpersRefuseBeforeTheNetwork:
    @staticmethod
    def _tripwires(stack: ExitStack, module: str) -> None:
        stack.enter_context(patch(f"{module}.RestliClient", side_effect=_NetworkReached(), create=True))
        for verb in ("post", "put", "get", "delete"):
            stack.enter_context(patch(f"requests.{verb}", side_effect=_NetworkReached()))
        stack.enter_context(patch("requests.Session.send", side_effect=_NetworkReached()))

    @pytest.mark.parametrize("state", _UNSENDABLE)
    @pytest.mark.parametrize("module,name", _HELPER_CALLS, ids=[c[1] for c in _HELPER_CALLS])
    def test_a_sandbox_or_unreadable_account_is_refused_before_any_network_call(
            self, module: str, name: str, state: Any) -> None:
        with ExitStack() as stack:
            stack.enter_context(patch(_MODE, return_value=state))
            self._tripwires(stack, module)
            with pytest.raises(SandboxAccountRefused):
                _call_minimal(module, name)

    @pytest.mark.parametrize("state", _SENDABLE)
    @pytest.mark.parametrize("module,name", _HELPER_CALLS, ids=[c[1] for c in _HELPER_CALLS])
    def test_a_standard_or_internal_account_still_gets_past_the_guard(
            self, module: str, name: str, state: Any) -> None:
        """Anti-overreach: the guard stands aside, so the call goes on to its own work."""
        with ExitStack() as stack:
            stack.enter_context(patch(_MODE, return_value=state))
            self._tripwires(stack, module)
            try:
                _call_minimal(module, name)
            except SandboxAccountRefused:
                pytest.fail(f"{name} refused a {state['account_kind']} account")
            except Exception:  # noqa: BLE001 - anything else means it got past the guard
                pass


# ---------------------------------------------------------------------------
# Layer 3: the queue-fed send lanes
# ---------------------------------------------------------------------------

_DRY_RUN_LANES = [
    ("app/engagement/outreach.py", "send_scheduled_dm"),
    ("app/engagement/outreach.py", "send_catchup_touch"),
    ("app/engagement/outreach.py", "_send_lead_response"),
    ("app/engagement/invites.py", "send_connection_request"),
    ("app/engagement/posting.py", "post_to_linkedin"),
]


class TestTheDryRunLanesGateFirst:
    @pytest.mark.parametrize("rel,name", _DRY_RUN_LANES, ids=[n for _r, n in _DRY_RUN_LANES])
    def test_the_lane_asks_sandbox_dry_run_before_anything_that_could_send(self, rel: str,
                                                                          name: str) -> None:
        path = _SRC / rel
        fn = _functions(path)[name]
        source = ast.get_source_segment(path.read_text(), fn)
        assert "sandbox_dry_run(" in source, f"{name} no longer asks the sandbox gate"
        gate_at = source.index("sandbox_dry_run(")
        for later in ("skip_browser_lane(", "send_dm_now(", "invite_to_connect_now(",
                      "share_on_linkedin(", "share_document_on_linkedin(",
                      "get_user_password_pair_by_id(", "get_current_profile("):
            if later in source:
                assert gate_at < source.index(later), f"{name}: {later} comes before the sandbox gate"


class TestSandboxPostsAreDryRun:
    def test_a_sandbox_post_is_completed_and_never_published(self) -> None:
        from cqc_lem.app.engagement.posting import post_to_linkedin
        from cqc_lem.utilities.db import PostStatus

        with ExitStack() as stack:
            stack.enter_context(patch(_MODE, return_value=_state("sandbox")))
            stack.enter_context(patch(f"{_POST}.get_post_status", return_value="approved"))
            update = stack.enter_context(patch(f"{_POST}.update_db_post_status", return_value=True))
            creds = stack.enter_context(patch(f"{_POST}.get_user_password_pair_by_id"))
            shares = [stack.enter_context(patch(f"{_POST}.{n}")) for n in (
                "share_on_linkedin", "share_carousel_on_linkedin", "share_document_on_linkedin",
                "share_animated_image_on_linkedin")]
            result = post_to_linkedin.run(1, 40)
        assert SANDBOX_DRY_RUN_MESSAGE in result
        update.assert_called_once_with(40, PostStatus.POSTED)
        creds.assert_not_called()
        for share in shares:
            share.assert_not_called()

    @pytest.mark.parametrize("state", _UNREADABLE_KINDS)
    def test_an_unreadable_kind_is_not_completed_as_a_dry_run(self, state: Any) -> None:
        """It falls through to the publish helpers, whose `guard_send` refuses it."""
        from cqc_lem.app.engagement.posting import post_to_linkedin

        with ExitStack() as stack:
            stack.enter_context(patch(_MODE, return_value=state))
            stack.enter_context(patch(f"{_POST}.get_post_status", return_value="approved"))
            update = stack.enter_context(patch(f"{_POST}.update_db_post_status", return_value=True))
            stack.enter_context(patch(f"{_POST}.get_post_manual_publish", side_effect=_NetworkReached()))
            with pytest.raises(_NetworkReached):
                post_to_linkedin.run(1, 40)  # got past the dry-run check to the next step
        assert not [c for c in update.call_args_list if "POSTED" in repr(c)]


class TestTheMockedClientIsNeverTouched:
    """The task-level proof: an approved item on a sandbox account never reaches the LinkedIn client.

    The same item on a standard or internal account still does.
    """

    @staticmethod
    def _run_dm(kind: Any) -> tuple[Any, MagicMock, MagicMock]:
        from cqc_lem.app.engagement.outreach import send_scheduled_dm

        row = {"status": "approved", "user_id": 1, "scheduled_time": None,
               "recipient_profile_url": "https://www.linkedin.com/in/x/", "message": "m",
               "source": None}
        with ExitStack() as stack:
            stack.enter_context(patch(_MODE, return_value=_state(kind)))
            stack.enter_context(patch("cqc_lem.utilities.db.get_scheduled_dm", return_value=row))
            update = stack.enter_context(patch("cqc_lem.utilities.db.update_scheduled_dm_status",
                                               return_value=True))
            client = stack.enter_context(patch("cqc_lem.app.engagement.outreach.send_dm_now",
                                               return_value=True))
            stack.enter_context(patch("cqc_lem.app.engagement.outreach.get_engagement_preferences",
                                      return_value={"max_dms_per_day": 10}))
            stack.enter_context(patch("cqc_lem.utilities.db.count_dms_sent_today", return_value=0))
            stack.enter_context(patch("cqc_lem.app.engagement.outreach.remaining_actions",
                                      return_value=5))
            result = send_scheduled_dm.run(dm_id=5)
        return result, client, update

    def test_a_sandbox_dm_is_completed_without_the_client(self) -> None:
        result, client, update = self._run_dm("sandbox")
        client.assert_not_called()
        assert SANDBOX_DRY_RUN_MESSAGE in result
        update.assert_called_once()
        assert "sent" in repr(update.call_args).lower()

    @pytest.mark.parametrize("kind", ["standard", "internal"])
    def test_a_standard_or_internal_dm_still_reaches_the_client(self, kind: str) -> None:
        result, client, update = self._run_dm(kind)
        client.assert_called_once()
        assert SANDBOX_DRY_RUN_MESSAGE not in result

    @pytest.mark.parametrize("kind", [None, "", "Sandbox", "garbage"])
    def test_an_unrecognised_kind_neither_sends_nor_completes_the_row(self, kind: Any) -> None:
        result, client, update = self._run_dm(kind)
        client.assert_not_called()
        update.assert_not_called()
