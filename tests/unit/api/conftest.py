"""Shared defaults for the API unit lane.

Issue #745 (2c) put a step-up gate in front of every credential-touching endpoint and made the
email PIN conditional on whether the account holds a strong factor. Both ask the database two
questions that the ~40 test modules in this directory never mocked, because they did not exist —
and an unmocked read here is not a failed query, it is a `TypeError` out of the connection pool.

So this fixture answers those questions with the state EVERY account is in until it enrols
something: no passkey, no authenticator app, therefore nothing to step up with, nothing to demote
the PIN for, and enrolment wide open. That is the pre-2c behaviour these modules were written
against.

`tests/unit/api/test_strong_auth.py` patches over it per-test to exercise both sides of the gate —
a nested `patch` wins over this one.

Since #1154 these names are bound in more than one module: the enrolment and step-up handlers live
in `api/routers/user.py`, the PIN-login ones still in `api/main.py`, and a few are read by both.
A blanket default has to rebind every module that reads the name — patching only `main` would leave
the router calling the real function against a dead database. `_default_everywhere` does that, and
still fails loudly if NO module binds the name (a rename must not silently stop being defaulted).
"""

from contextlib import ExitStack, contextmanager
from unittest.mock import patch

import pytest

# Every module an `/api` handler's dependencies can be bound in. Add a slice's module here when the
# next one lands; a name absent from one of them is simply not patched there.
_HANDLER_MODULES = ("cqc_lem.api.main", "cqc_lem.api.routers.user")


@contextmanager
def _default_everywhere(name: str, **kwargs):
    """Patch `name` in every handler module that binds it."""
    import importlib

    hosts = [m for m in _HANDLER_MODULES if hasattr(importlib.import_module(m), name)]
    assert hosts, f"nothing binds {name!r} any more — this default is patching a dead name"
    with ExitStack() as stack:
        for host in hosts:
            stack.enter_context(patch(f"{host}.{name}", **kwargs))
        yield


@pytest.fixture(autouse=True)
def _account_without_a_strong_factor():
    with _default_everywhere("has_strong_factor", return_value=False), \
         _default_everywhere("step_up_satisfied", return_value=True), \
         _default_everywhere("enrollment_allowed", return_value=True), \
         _default_everywhere("session_signed_in_with_recovery_code", return_value=False), \
         _default_everywhere("has_confirmed_totp", return_value=False), \
         _default_everywhere("enrollment_required", return_value=False), \
         _default_everywhere("enrollment_hold_active", return_value=False):
        yield


# Issue #914: the routes that used to read the acting user out of an `email` / `user_id` request
# parameter now resolve it from the session, so a module exercising one of them needs a signed-in
# caller. Opt in with the fixture — it is deliberately NOT autouse, because "no session" is the
# state half of `test_param_auth_scoping.py` is asserting on.
SESSION_USER_ID = 42
SESSION_EMAIL = "user@example.com"
SESSION_TOKEN = "session-token-abc"


@pytest.fixture
def signed_in():
    """Any token resolves to SESSION_USER_ID, who owns every post named.

    `record_auth_event` is stubbed because a denied foreign target writes an `auth_audit_log` row
    (`_deny`) — best effort in production, but an unmocked one here is a real connection attempt on
    every 403 case.
    """
    with patch("cqc_lem.api.main.get_session_user_id", return_value=SESSION_USER_ID), \
         _default_everywhere("get_user_email", return_value=SESSION_EMAIL), \
         _default_everywhere("record_auth_event", return_value=True), \
         _default_everywhere("user_owns_posts", return_value=True):
        yield SESSION_USER_ID
