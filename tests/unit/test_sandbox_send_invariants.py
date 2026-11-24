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
        "an API comment lane that asks read_engagement_mode first: a sandbox account reads suggest and "
        "gets the draft stored as a suggestion, an unreadable kind defers it; comment_on_linkedin_post "
        "opens with guard_send behind that",
    ("app/engagement/feed.py", "auto_second_wave_comment"):
        "an API comment lane that asks read_engagement_mode first: a sandbox account reads suggest and "
        "gets the draft stored as a suggestion, an unreadable kind defers it; comment_on_linkedin_post "
        "opens with guard_send behind that",
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
    def test_an_unreadable_kind_publishes_nothing_changes_nothing_and_does_not_retry(
            self, state: Any) -> None:
        """Not a dry run (that would write a terminal state), not a failure and not a retry loop."""
        from cqc_lem.app.engagement.posting import post_to_linkedin

        with ExitStack() as stack:
            stack.enter_context(patch(_MODE, return_value=state))
            stack.enter_context(patch(f"{_POST}.get_post_status", return_value="approved"))
            update = stack.enter_context(patch(f"{_POST}.update_db_post_status", return_value=True))
            creds = stack.enter_context(patch(f"{_POST}.get_user_password_pair_by_id"))
            retry = stack.enter_context(patch.object(post_to_linkedin, "retry"))
            info = stack.enter_context(patch(f"{_POST}.log_info"))
            shares = [stack.enter_context(patch(f"{_POST}.{n}")) for n in (
                "share_on_linkedin", "share_carousel_on_linkedin", "share_document_on_linkedin",
                "share_animated_image_on_linkedin")]
            result = post_to_linkedin.run(1, 40)
        assert "not published" in result and "left unchanged" in result
        update.assert_not_called()          # the row is exactly as it was: retryable on the next beat
        retry.assert_not_called()           # no retry loop
        creds.assert_not_called()           # nothing was read for a publish
        for share in shares:
            share.assert_not_called()
        assert any("sandbox_refused" in repr(c) for c in info.call_args_list), "it must be logged"

    @pytest.mark.parametrize("state", _SENDABLE)
    def test_a_standard_or_internal_account_gets_past_the_kind_check(self, state: Any) -> None:
        from cqc_lem.app.engagement.posting import post_to_linkedin

        with ExitStack() as stack:
            stack.enter_context(patch(_MODE, return_value=state))
            stack.enter_context(patch(f"{_POST}.get_post_status", return_value="approved"))
            stack.enter_context(patch(f"{_POST}.update_db_post_status", return_value=True))
            reached = stack.enter_context(patch(f"{_POST}.get_post_manual_publish",
                                                side_effect=_NetworkReached()))
            with pytest.raises(_NetworkReached):
                post_to_linkedin.run(1, 40)
        reached.assert_called_once()


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


# ---------------------------------------------------------------------------
# Discovery: nothing here depends on a hand-kept list of send paths
# ---------------------------------------------------------------------------

# Names a function calls to ask the sandbox / suggest gate (any of them is a gate).
_GATE_CALLS = {"skip_browser_lane", "require_browser_automation", "sandbox_dry_run",
               "read_engagement_mode", "resolve_engagement_mode", "is_suggest_only",
               "browser_automation_allowed", "guard_send", "may_send", "_browser_lane_users"}


def _all_modules() -> list[pathlib.Path]:
    return sorted(p for p in _SRC.rglob("*.py") if p.name != "__init__.py")


def _decorator_name(node: ast.AST) -> str:
    target = node.func if isinstance(node, ast.Call) else node
    parts = []
    while isinstance(target, ast.Attribute):
        parts.append(target.attr)
        target = target.value
    if isinstance(target, ast.Name):
        parts.append(target.id)
    return ".".join(reversed(parts))


def _is_task_decorator(name: str) -> bool:
    return name in ("shared_task", "task") or name.endswith(".task") or "shared_task" in name


def _discover_tasks() -> dict[str, tuple[str, ast.AST]]:
    """Every Celery task under src/cqc_lem, as name -> (module path, node).

    Found by decorator, so a new `@shared_task.task` function is picked up without anyone remembering
    to list it.
    """
    found: dict[str, tuple[str, ast.AST]] = {}
    for path in _all_modules():
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and any(_is_task_decorator(_decorator_name(d)) for d in node.decorator_list):
                found[node.name] = (str(path.relative_to(_SRC)), node)
    return found


# Tasks that reach LinkedIn only THROUGH a named helper that carries the gate; the helper is asserted
# to call a gate below, so this is a pointer, not an exemption.
_DELEGATES = {
    "send_lead_response": "_send_lead_response",
}

# Tasks with NO LinkedIn action of their own: content or media generation, analytics and cost jobs,
# billing and email jobs, and dispatchers that only enqueue the gated tasks above. Each says why.
# A task that calls a gate is NOT listed here (the test fails if one is: exactly one classification).
_NO_LINKEDIN_ACTION = {
    "test_task": "AWS smoke-test task: returns a string",
    "test_get_my_profile": "read-only profile fetch through get_driver_wait_pair(user_id), whose "
                           "get_docker_driver backstop refuses a sandbox account",
    "auto_draft_group_post": "drafts text with an LLM and stores it; the publish is auto_post_to_group",
    "render_avatar_samples_task": "image generation",
    "auto_notify_stale_approvals": "reads the approval queue and emails the owner a digest; no LinkedIn "
                                   "session is opened",
    "auto_weekly_report": "reads post stats and emails the account owner a weekly report; no LinkedIn "
                          "session is opened",
    "auto_generate_content": "content generation (LLM), stores drafts",
    "plan_content_for_user": "content planning (LLM), stores drafts",
    "regenerate_post_video_task": "media generation",
    "regenerate_post_carousel_task": "media generation",
    "regenerate_post_task": "content generation (LLM)",
    "draft_occasion_post_task": "drafts text; the publish is auto_publish_occasion_post",
    "auto_create_weekly_content": "content generation (LLM), stores drafts",
    "collect_curated_sources": "reads public sources; publishes nothing",
    "dispatch_golden_hour_engagement": "only enqueues the gated reply-sweep tasks",
    "auto_weekly_comment_quality": "analytics over stored comments",
    "auto_draft_comment_candidates": "drafts comment text with an LLM and stores it for review; "
                                     "posts nothing and opens no browser",
    "auto_comment_ramp":"moves a stored daily-comment ceiling in Redis; sends nothing",
    "auto_suppression_tripwire": "analytics over stored impressions; pauses, never sends",
    "auto_nightly_content_quality": "analytics over stored posts",
    "auto_weekly_content_quality": "analytics over stored posts",
    "auto_profile_audit_weekly": "rubric grading of a cached profile; opens no browser, sends nothing",
    "auto_profile_audit_monthly": "rubric grading of a cached profile; opens no browser, sends nothing",
    "auto_icp_audience_report_monthly": "analytics over stored engagers; stores a report, sends nothing",
    "auto_media_integrity_scan": "checks stored media files",
    "auto_generate_newsletter_drafts": "content generation (LLM), stores drafts",
    "generate_newsletter_drafts_for_user": "content generation (LLM), stores drafts",
    "generate_newsletter_cover": "image generation",
    "auto_notify_pending_covers": "email to the account owner",
    "regenerate_newsletter_edition": "content generation (LLM)",
    "auto_refresh_profile_syntheses": "LLM synthesis over a stored profile",
    "auto_group_post_drafts": "only enqueues auto_draft_group_post",
    "rebuild_leads_for_user": "scores stored engagement rows",
    "rebuild_lead_scores": "only enqueues rebuild_leads_for_user",
    "auto_sync_brand_account": "database config for the brand account",
    "auto_notify_missing_linkedin_session": "email to the account owner",
    "auto_refresh_linkedin_tokens": "OAuth token refresh: credential plumbing, not a content action",
    "auto_watch_member_analytics_tokens": "email to the account owner about an expiring analytics token",
    "auto_onboarding_nudges": "email to the account owner",
    "auto_survey_prompts": "analytics prompts",
    "auto_backfill_missing_assets": "media generation",
    "auto_encrypt_secrets_at_rest": "database maintenance",
    "auto_clean_old_videos": "file cleanup",
    "sync_stripe_subscriptions": "billing sync",
    "auto_weekly_margin_report": "internal report",
    "auto_daily_cost_alerts": "internal alert",
    "auto_capacity_watch": "internal monitor",
    "auto_impact_report": "owner email report and a metric-free GitHub note",
    "auto_file_feedback_issues": "files GitHub issues for feedback",
    "auto_recluster_feedback": "analytics over stored feedback",
    "auto_changelog_notify": "email to account owners",
    "auto_update_faq": "content generation (LLM) for the FAQ",
    "auto_accrue_monthly_costs": "internal accounting",
    "auto_rollup_llm_costs": "internal accounting",
    "auto_weekly_cost_routing": "internal analytics",
    "auto_produce_feature_tutorial": "renders our own SPA; DEMO_MODE-gated, no account",
    "auto_weekly_youtube_token_check": "YouTube token health check",
    "auto_poll_calendly_bookings": "reads Calendly and labels leads; sends nothing",
    "ingest_call_transcript": "LLM extraction over an uploaded transcript; stores review candidates, sends nothing",
    "transcribe_call_audio": "Whisper transcription of an uploaded recording, then deletes the audio; no LinkedIn "
                             "session is opened",
    "purge_expired_call_transcripts": "retention purge of stored transcripts in our own DB",
}


class TestEveryCeleryTaskIsClassified:
    def test_the_discovery_found_the_tasks(self) -> None:
        tasks = _discover_tasks()
        assert len(tasks) > 90, f"found only {len(tasks)} — the decorator scan is not reading the tree"
        assert {"send_scheduled_dm", "post_to_linkedin", "send_connection_request"} <= tasks.keys()

    def _gated(self, node: ast.AST) -> set[str]:
        return set(_call_names(node)) & _GATE_CALLS

    def test_every_task_is_gated_delegated_or_registered_with_a_reason(self) -> None:
        tasks = _discover_tasks()
        unclassified = []
        for name, (rel, node) in sorted(tasks.items()):
            if self._gated(node) or name in _DELEGATES or name in _NO_LINKEDIN_ACTION:
                continue
            unclassified.append(f"{rel}:{name}")
        assert not unclassified, (
            "\n\nCelery tasks that neither call the sandbox/suggest gate nor appear in "
            f"_NO_LINKEDIN_ACTION with a reason: {unclassified}\nIf the task can reach LinkedIn, gate it "
            "(skip_browser_lane / sandbox_dry_run / guard_send); if it cannot, register it with why.\n")

    def test_no_task_is_classified_twice(self) -> None:
        tasks = _discover_tasks()
        both = [name for name, (_rel, node) in tasks.items()
                if name in _NO_LINKEDIN_ACTION and (self._gated(node) or name in _DELEGATES)]
        assert not both, (
            f"{both} are registered as having no LinkedIn action but call a gate (or delegate): "
            "remove them from _NO_LINKEDIN_ACTION so each task has exactly one classification")
        assert not set(_DELEGATES) & set(_NO_LINKEDIN_ACTION)

    def test_every_registered_name_is_still_a_task(self) -> None:
        tasks = _discover_tasks()
        stale = [n for n in (*_NO_LINKEDIN_ACTION, *_DELEGATES) if n not in tasks]
        assert not stale, f"registered but no longer a Celery task: {stale}"

    def test_every_reason_is_stated(self) -> None:
        assert all(len(reason.strip()) > 8 for reason in _NO_LINKEDIN_ACTION.values())

    def test_a_delegating_task_points_at_a_helper_that_gates(self) -> None:
        tasks = _discover_tasks()
        for task, helper in _DELEGATES.items():
            rel, node = tasks[task]
            assert helper in _call_names(node), f"{task} no longer calls {helper}"
            helper_node = next(n for p in _all_modules() for n in ast.walk(ast.parse(p.read_text()))
                               if isinstance(n, ast.FunctionDef) and n.name == helper)
            assert self._gated(helper_node), f"{helper} (behind {task}) calls no gate"


# Every function under src/cqc_lem OUTSIDE poster.py/reshare.py that makes a mutating HTTP call
# (requests/httpx post/put/delete/patch/request, or RestliClient) in code that mentions LinkedIn. Each
# is OAuth credential plumbing, not a content action, which `guard_send` is for.
_OAUTH_PLUMBING = {
    ("api/main.py", "linkedin_callback"): "OAuth code-for-token exchange",
    ("api/main.py", "member_analytics_callback"): "OAuth code-for-token exchange (analytics)",
    ("utilities/linkedin/token_refresh.py", "attempt_token_refresh"): "OAuth token refresh",
}
_MUTATING = {"post", "put", "delete", "patch", "request", "send", "stream"}
_HTTP_LIBS = {"requests", "httpx", "urllib3", "aiohttp"}
# Constructors of a client object whose methods then send (`requests.Session()`, `httpx.Client()`).
_CLIENT_CTORS = {"Session", "Client", "AsyncClient", "ClientSession", "PoolManager", "RestliClient"}
# RestliClient methods that change something on LinkedIn.
_RESTLI_WRITES = {"create", "batch_create", "update", "batch_update", "partial_update",
                  "batch_partial_update", "delete", "batch_delete", "action"}


def _http_aliases(tree: ast.AST) -> tuple[set[str], set[str], set[str]]:
    """What this module binds to an HTTP library, under ANY name (#2527: aliases were missed).

    Returns (names bound to an HTTP library module, names bound to a mutating function of one, names
    bound to a client constructor).
    """
    libs, verbs, ctors = set(_HTTP_LIBS), set(), {"RestliClient"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] in _HTTP_LIBS:
                    libs.add(a.asname or a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".")[0]
            for a in node.names:
                if root in _HTTP_LIBS and a.name in _MUTATING:
                    verbs.add(a.asname or a.name)
                if (root in _HTTP_LIBS or root == "linkedin_api") and a.name in _CLIENT_CTORS:
                    ctors.add(a.asname or a.name)
    return libs, verbs, ctors


def _client_factories(tree: ast.AST, ctors: set[str]) -> set[str]:
    """Functions in the module that hand back a client (`def _restli() -> RestliClient`)."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            ret = node.returns
            if isinstance(ret, ast.Name) and ret.id in ctors:
                out.add(node.name)
    return out


def _module_clients(tree: ast.AST, libs: set[str], ctors: set[str]) -> set[str]:
    """Module-level names holding an EXISTING client instance (`client = RestliClient()`)."""
    out = set()
    for node in getattr(tree, "body", []):
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, ast.Call) \
                and _is_ctor(node.value.func, libs, ctors):
            for t in (node.targets if isinstance(node, ast.Assign) else [node.target]):
                if isinstance(t, ast.Name):
                    out.add(t.id)
    return out


def _is_ctor(f: ast.AST, libs: set[str], ctors: set[str]) -> bool:
    if isinstance(f, ast.Name):
        return f.id in ctors
    return isinstance(f, ast.Attribute) and f.attr in _CLIENT_CTORS \
        and isinstance(f.value, ast.Name) and f.value.id in libs


def _function_makes_http_write(node: ast.AST, libs: set[str], verbs: set[str], ctors: set[str],
                               factories: set[str], clients: set[str]) -> bool:
    # The body only: a route decorator (`@router.post(...)`) is not an HTTP call.
    for sub in (s for stmt in node.body for s in ast.walk(stmt)):
        if not isinstance(sub, ast.Call):
            continue
        f = sub.func
        if isinstance(f, ast.Name) and (f.id in verbs or f.id in factories):
            return True
        if _is_ctor(f, libs, ctors):
            return True  # a client built here is a client this function sends with
        if isinstance(f, ast.Attribute):
            recv = f.value
            if isinstance(recv, ast.Name) and recv.id in libs and f.attr in _MUTATING:
                return True
            if isinstance(recv, ast.Name) and recv.id in clients \
                    and f.attr in _MUTATING | _RESTLI_WRITES:
                return True
            # `session.post(...)`, `self._client.put(...)`, `requests.Session().post(...)`: any
            # mutating verb on a receiver that is not an HTTP library module.
            if f.attr in {"post", "put", "patch"}:
                return True
            if isinstance(recv, ast.Call) and _is_ctor(recv.func, libs, ctors):
                return True
            if isinstance(recv, ast.Call) and isinstance(recv.func, ast.Name) \
                    and recv.func.id in factories and f.attr in _RESTLI_WRITES:
                return True
    return False


def _http_writers_in(text: str) -> set[str]:
    """Names of the functions in this source that make a mutating HTTP call in LinkedIn code."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return set()
    libs, verbs, ctors = _http_aliases(tree)
    factories = _client_factories(tree, ctors)
    clients = _module_clients(tree, libs, ctors)
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name in factories:
            continue  # building the client is not a send; its callers are scanned
        if _function_makes_http_write(node, libs, verbs, ctors, factories, clients) \
                and "linkedin" in (ast.get_source_segment(text, node) or "").lower():
            found.add(node.name)
    return found


def _linkedin_http_callers() -> set[tuple[str, str]]:
    return {(str(path.relative_to(_SRC)), name)
            for path in _all_modules() for name in _http_writers_in(path.read_text())}


class TestEveryLinkedInHttpCallerIsAccountedFor:
    def test_the_scan_finds_the_publish_helpers_and_the_oauth_calls(self) -> None:
        found = _linkedin_http_callers()
        assert ("utilities/linkedin/poster.py", "share_on_linkedin") in found
        assert ("utilities/linkedin/token_refresh.py", "attempt_token_refresh") in found

    def test_every_function_that_calls_linkedin_over_http_is_gated_or_registered(self) -> None:
        offenders = []
        publish_files = {"utilities/linkedin/poster.py", "utilities/linkedin/reshare.py"}
        for rel, name in sorted(_linkedin_http_callers()):
            if (rel, name) in _OAUTH_PLUMBING:
                continue
            if rel in publish_files:
                fn = _functions(_SRC / rel)[name]
                private = name.startswith("_")
                token_only = name in _TOKEN_ONLY_HELPERS
                if private or token_only or "guard_send" in _call_names(fn):
                    continue
            offenders.append(f"{rel}:{name}")
        assert not offenders, (
            f"\n\nThese functions make a mutating call to LinkedIn and are neither a gated publish "
            f"helper nor registered OAuth plumbing: {offenders}\nGive a publish helper a user_id and a "
            "guard_send, or register OAuth plumbing in _OAUTH_PLUMBING with why.\n")

    def test_every_registered_oauth_function_still_makes_the_call(self) -> None:
        found = _linkedin_http_callers()
        stale = [k for k in _OAUTH_PLUMBING if k not in found]
        assert not stale, f"registered but no longer an HTTP caller: {stale}"

    @pytest.mark.parametrize("source", [
        "import requests\ndef f():\n    s = requests.Session()\n    s.post('https://api.linkedin.com')\n",
        "import httpx\ndef f():\n    with httpx.Client() as c:\n        c.put('https://api.linkedin.com')\n",
        "import httpx\ndef f(c):\n    c.patch('https://api.linkedin.com')\n",
        "import requests as r\ndef f():\n    r.post('https://api.linkedin.com')\n",
        "import httpx as hx\ndef f():\n    hx.request('POST', 'https://api.linkedin.com')\n",
        "from requests import post as p\ndef f():\n    p('https://api.linkedin.com')\n",
        "from httpx import Client as C\ndef f():\n    C().get('https://api.linkedin.com')\n",
        "from linkedin_api.clients.restli.client import RestliClient\nCLIENT = RestliClient()\n"
        "def f():\n    CLIENT.create(resource_path='/posts', entity={}, access_token='linkedin')\n",
        "from linkedin_api.clients.restli.client import RestliClient as R\n"
        "def _c() -> R:\n    return R()\n"
        "def f():\n    _c().action(resource_path='/x', access_token='linkedin')\n",
        "from linkedin_api.clients.restli.client import RestliClient\n"
        "def _c() -> RestliClient:\n    return RestliClient()\n"
        "def f():\n    _c().create(resource_path='/x', access_token='linkedin')\n",
    ], ids=["session.post", "httpx.Client-ctx", "client-param.patch", "aliased-requests",
            "aliased-httpx.request", "from-import-alias", "aliased-ctor", "module-restli-instance",
            "aliased-restli-factory", "restli-factory"])
    def test_the_scan_catches_each_evasion(self, source: str) -> None:
        """#2527: session/client objects, aliased imports and existing RestliClient instances."""
        assert _http_writers_in(source) == {"f"}

    def test_the_scan_ignores_reads_and_route_decorators(self) -> None:
        source = ("import requests\nrouter = object()\n"
                  "@router.post('/linkedin')\ndef route():\n    return requests.get('https://linkedin.com')\n")
        assert _http_writers_in(source) == set()


# ---------------------------------------------------------------------------
# #2527: the private writers in poster.py / reshare.py cannot be reached from outside
# ---------------------------------------------------------------------------

_PUBLISH_MODULE_SUFFIXES = ("linkedin.poster", "linkedin.reshare")


def _ungated_writers() -> set[str]:
    """Every function in poster.py/reshare.py that reaches LinkedIn WITHOUT opening on guard_send.

    The private writers (`_create_post`, `_create_document_post_*`, `_restli`, ...) and the named
    token-only helpers: each is safe only because a gated helper in the same module is its sole caller.
    """
    found = set(_TOKEN_ONLY_HELPERS)
    for path in _PUBLISH_MODULES.values():
        text = path.read_text()
        tree = ast.parse(text)
        libs, verbs, ctors = _http_aliases(tree)
        factories = _client_factories(tree, ctors)
        clients = _module_clients(tree, libs, ctors)
        for name, fn in _functions(path).items():
            if "guard_send" in _call_names(fn):
                continue
            if name in factories or _function_makes_http_write(fn, libs, verbs, ctors, factories, clients) \
                    or any(m in (ast.get_source_segment(text, fn) or "") for m in _HTTP_MARKERS):
                found.add(name)
    return found


def _publish_bindings(tree: ast.AST) -> tuple[dict[str, str], set[str]]:
    """(local name -> poster/reshare function it is bound to, local names bound to the modules)."""
    names: dict[str, str] = {}
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.endswith(_PUBLISH_MODULE_SUFFIXES):
                for a in node.names:
                    names[a.asname or a.name] = a.name
            elif node.module.endswith("utilities.linkedin"):
                for a in node.names:
                    if a.name in ("poster", "reshare"):
                        modules.add(a.asname or a.name)
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name.endswith(_PUBLISH_MODULE_SUFFIXES) and a.asname:
                    modules.add(a.asname)
    return names, modules


def _publish_references(tree: ast.AST) -> set[str]:
    """Every poster/reshare function this module names, through any import or alias."""
    names, modules = _publish_bindings(tree)
    used = set(names.values())
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
                and node.value.id in modules:
            used.add(node.attr)
        # `cqc_lem.utilities.linkedin.poster._x`, spelled out in full
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Attribute) \
                and node.value.attr in ("poster", "reshare"):
            used.add(node.attr)
    return used


def _resolved_callers(helpers: set[str]) -> set[tuple[str, str]]:
    """Functions outside poster/reshare that CALL one of `helpers`, through any import alias."""
    found = set()
    for path in _all_modules():
        if path in _PUBLISH_MODULES.values():
            continue
        tree = ast.parse(path.read_text())
        names, modules = _publish_bindings(tree)
        local = {alias for alias, orig in names.items() if orig in helpers}
        if not local and not modules:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for sub in ast.walk(node):
                if not isinstance(sub, ast.Call):
                    continue
                f = sub.func
                if (isinstance(f, ast.Name) and f.id in local) or (
                        isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name)
                        and f.value.id in modules and f.attr in helpers):
                    found.add((str(path.relative_to(_SRC)), node.name))
    return found


class TestThePrivateWritersStayPrivate:
    def test_the_walk_found_the_private_writers(self) -> None:
        writers = _ungated_writers()
        assert {"_create_post", "_restli", "_create_document_post_versioned",
                "_create_image_post_versioned"} <= writers, writers

    def test_no_module_outside_the_publish_files_reaches_an_ungated_writer(self) -> None:
        writers = _ungated_writers()
        offenders = []
        for path in _all_modules():
            if path in _PUBLISH_MODULES.values():
                continue
            hit = _publish_references(ast.parse(path.read_text())) & writers
            if hit:
                offenders.append(f"{path.relative_to(_SRC)}: {sorted(hit)}")
        assert not offenders, (
            "\n\nThese modules import or reference a poster/reshare function that reaches LinkedIn "
            f"without guard_send: {offenders}\nCall the gated share_*/comment_* helper instead, or give "
            "the function a user_id and a guard_send.\n")

    @pytest.mark.parametrize("source", [
        "from cqc_lem.utilities.linkedin.reshare import _create_post\n",
        "from cqc_lem.utilities.linkedin.poster import upload_media as um\n",
        "from cqc_lem.utilities.linkedin import poster as p\ndef f():\n    p._restli()\n",
        "import cqc_lem.utilities.linkedin.reshare as rs\ndef f():\n    rs._create_post('t', {})\n",
    ], ids=["private-import", "aliased-token-helper", "module-alias", "dotted-module-alias"])
    def test_the_reference_scan_catches_each_route_in(self, source: str) -> None:
        assert _publish_references(ast.parse(source)) & _ungated_writers()

    def test_every_caller_through_any_alias_is_registered(self) -> None:
        """The bare-name scan above misses `from poster import share_on_linkedin as s`; this one does not."""
        helpers = {n for path in _PUBLISH_MODULES.values() for n, fn in _functions(path).items()
                   if "guard_send" in _call_names(fn)}
        unregistered = _resolved_callers(helpers) - set(_REGISTERED_CALLERS)
        assert not unregistered, f"unregistered callers of a publish helper: {sorted(unregistered)}"


# ---------------------------------------------------------------------------
# #2527: the gate RUNS first, not just "is present"
# ---------------------------------------------------------------------------

class TestTheGateRunsBeforeAnyHttpCall:
    """Each send path calls guard_send / sandbox_dry_run BEFORE any network call, observed at run time."""

    @pytest.mark.parametrize("module,name", _HELPER_CALLS, ids=[c[1] for c in _HELPER_CALLS])
    def test_guard_send_runs_before_the_network_for_a_sending_account(self, module: str,
                                                                       name: str) -> None:
        from cqc_lem.utilities import engagement_mode as em

        events: list[str] = []
        real_guard = em.guard_send

        def gate(*a: Any, **k: Any) -> None:
            events.append("gate")
            real_guard(*a, **k)

        def net(*_a: Any, **_k: Any) -> None:
            events.append("net")
            raise _NetworkReached()

        with ExitStack() as stack:
            stack.enter_context(patch(_MODE, return_value=_state("standard")))
            stack.enter_context(patch(f"{module}.guard_send", side_effect=gate))
            # Credentials present, so the helper goes on toward the network.
            stack.enter_context(patch(f"{module}.get_user_access_token", return_value="tok", create=True))
            stack.enter_context(patch(f"{module}.get_user_linked_sub_id", return_value="sub", create=True))
            stack.enter_context(patch(f"{module}.RestliClient", side_effect=net, create=True))
            for verb in ("post", "put", "get", "delete", "request"):
                stack.enter_context(patch(f"requests.{verb}", side_effect=net))
            stack.enter_context(patch("requests.Session.send", side_effect=net))
            try:
                _call_minimal(module, name)
            except Exception:  # noqa: BLE001 - the order of events is the assertion
                pass
        assert events and events[0] == "gate", f"{name}: first event was {events[:1]}, not the gate"

    @pytest.mark.parametrize("kind", ["standard", "sandbox"])
    def test_send_scheduled_dm_asks_the_sandbox_gate_before_the_client(self, kind: str) -> None:
        from cqc_lem.app.engagement import outreach

        events: list[str] = []
        real = outreach.sandbox_dry_run

        def gate(*a: Any, **k: Any) -> bool:
            events.append("gate")
            return real(*a, **k)

        def client(*_a: Any, **_k: Any) -> bool:
            events.append("net")
            return True

        row = {"status": "approved", "user_id": 1, "scheduled_time": None,
               "recipient_profile_url": "https://www.linkedin.com/in/x/", "message": "m", "source": None}
        with ExitStack() as stack:
            stack.enter_context(patch(_MODE, return_value=_state(kind)))
            stack.enter_context(patch("cqc_lem.app.engagement.outreach.sandbox_dry_run", side_effect=gate))
            stack.enter_context(patch("cqc_lem.utilities.db.get_scheduled_dm", return_value=row))
            stack.enter_context(patch("cqc_lem.utilities.db.update_scheduled_dm_status", return_value=True))
            stack.enter_context(patch("cqc_lem.app.engagement.outreach.send_dm_now", side_effect=client))
            stack.enter_context(patch("cqc_lem.app.engagement.outreach.get_engagement_preferences",
                                      return_value={"max_dms_per_day": 10}))
            stack.enter_context(patch("cqc_lem.utilities.db.count_dms_sent_today", return_value=0))
            stack.enter_context(patch("cqc_lem.app.engagement.outreach.remaining_actions", return_value=5))
            outreach.send_scheduled_dm.run(dm_id=5)
        assert events[0] == "gate"
        assert events == (["gate", "net"] if kind == "standard" else ["gate"])


# ---------------------------------------------------------------------------
# #2527: a task queued with an ETA decides at RUN time, not at enqueue time
# ---------------------------------------------------------------------------

_ETA_TASKS = {"post_to_linkedin", "send_scheduled_dm", "send_catchup_touch"}


class TestEtaQueuedSendsReReadAtRunTime:
    def test_every_eta_dispatch_carries_only_ids(self) -> None:
        """No status or kind is snapshotted into the message, so nothing stale can be acted on."""
        path = _SRC / "app" / "run_scheduler.py"
        seen = set()
        for node in ast.walk(ast.parse(path.read_text())):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "apply_async"
                    and any(k.arg in ("eta", "countdown") for k in node.keywords)):
                continue
            target = node.func.value
            name = target.id if isinstance(target, ast.Name) else getattr(target, "attr", "")
            if name not in _ETA_TASKS:
                continue
            seen.add(name)
            kw = next((k.value for k in node.keywords if k.arg == "kwargs"), None)
            if isinstance(kw, ast.Dict):
                keys = {k.value for k in kw.keys if isinstance(k, ast.Constant)}
                assert keys <= {"user_id", "post_id", "dm_id", "touch_id"}, (name, keys)
        assert {"post_to_linkedin", "send_scheduled_dm", "send_catchup_touch"} <= seen, seen

    @pytest.mark.parametrize("rel,name", [("app/engagement/posting.py", "post_to_linkedin"),
                                          ("app/engagement/outreach.py", "send_scheduled_dm"),
                                          ("app/engagement/outreach.py", "send_catchup_touch")])
    def test_the_task_reads_its_row_then_the_kind_in_its_own_body(self, rel: str, name: str) -> None:
        fn = _functions(_SRC / rel)[name]
        calls = _call_names(fn)
        assert {"get_post_status", "get_scheduled_dm", "get_catchup_touch"} & set(calls), name
        assert "sandbox_dry_run" in calls, name

    def test_a_post_approved_at_enqueue_on_an_account_flipped_to_sandbox_before_the_eta_is_a_dry_run(
            self) -> None:
        from cqc_lem.app.engagement.posting import post_to_linkedin

        # Enqueued while `standard`; the message carries only ids, so the run reads `sandbox`.
        with ExitStack() as stack:
            stack.enter_context(patch(_MODE, return_value=_state("sandbox")))
            stack.enter_context(patch(f"{_POST}.get_post_status", return_value="scheduled"))
            stack.enter_context(patch(f"{_POST}.update_db_post_status", return_value=True))
            shares = [stack.enter_context(patch(f"{_POST}.{n}")) for n in (
                "share_on_linkedin", "share_carousel_on_linkedin", "share_document_on_linkedin",
                "share_animated_image_on_linkedin")]
            result = post_to_linkedin.run(1, 40)
        assert SANDBOX_DRY_RUN_MESSAGE in result
        for share in shares:
            share.assert_not_called()

    def test_a_dm_cancelled_after_enqueue_is_not_sent_at_its_eta(self) -> None:
        from cqc_lem.app.engagement.outreach import send_scheduled_dm

        row = {"status": "canceled", "user_id": 1, "scheduled_time": None,
               "recipient_profile_url": "https://www.linkedin.com/in/x/", "message": "m", "source": None}
        with ExitStack() as stack:
            stack.enter_context(patch(_MODE, return_value=_state("standard")))
            stack.enter_context(patch("cqc_lem.utilities.db.get_scheduled_dm", return_value=row))
            client = stack.enter_context(patch("cqc_lem.app.engagement.outreach.send_dm_now"))
            send_scheduled_dm.run(dm_id=5)
        client.assert_not_called()

    def test_a_catchup_touch_on_an_account_flipped_to_sandbox_is_a_dry_run(self) -> None:
        from cqc_lem.app.engagement import outreach

        touch = {"status": "approved", "user_id": 1, "message": "hi"}
        with ExitStack() as stack:
            stack.enter_context(patch(_MODE, return_value=_state("sandbox")))
            stack.enter_context(patch.object(outreach, "get_catchup_touch", return_value=touch))
            update = stack.enter_context(patch.object(outreach, "update_catchup_touch_status"))
            stack.enter_context(patch.object(outreach, "report_catchup_run"))
            skip = stack.enter_context(patch.object(outreach, "skip_browser_lane"))
            result = outreach.send_catchup_touch.run(touch_id=3)
        assert SANDBOX_DRY_RUN_MESSAGE in result
        update.assert_called_once()
        skip.assert_not_called()


class TestEverySessionOpenerReachesTheBackstop:
    def test_get_docker_driver_refuses_an_account_before_it_opens_a_session(self) -> None:
        path = _SRC / "utilities" / "selenium_util.py"
        fn = _functions(path)["get_docker_driver"]
        source = ast.get_source_segment(path.read_text(), fn)
        assert "require_browser_automation(" in source
        assert source.index("require_browser_automation(") < source.index("webdriver.Remote(")
