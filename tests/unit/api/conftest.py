"""Shared test helpers for the API unit lane.

Issue #1212 removed the blanket autouse patch over account state. A test that needs
a signed-in caller or a particular strong-auth state must now state it explicitly
via the fixtures exported here. A test that states nothing runs against the real
app and real (mocked) database layer, so missing setup FAILS rather than defaulting.
"""

from contextlib import contextmanager
from typing import Any, Iterator, Optional

import pytest

# Issue #914: shared constants for tests that opt in to a signed-in session.
SESSION_USER_ID = 42
SESSION_EMAIL = "user@example.com"
SESSION_TOKEN = "session-token-abc"


# The modules where the API layer binds the functions we override.
_MAIN = "cqc_lem.api.main"
_AUTH = "cqc_lem.api.routers.auth"
_USER = "cqc_lem.api.routers.user"


@contextmanager
def _stated_account_state(
    *,
    user_id: Optional[int],
    email: Optional[str] = None,
    owns_posts: bool = True,
    no_strong_factor: bool = True,
    step_up_satisfied: bool = True,
    enrollment_allowed: bool = True,
    enrollment_required: bool = False,
    enrollment_hold_active: bool = False,
    recovery_code_session: bool = False,
    has_confirmed_totp: bool = False,
) -> Iterator[None]:
    """Temporarily replace the API's account-state helpers.

    The handlers do NOT use FastAPI ``Depends()`` for these helpers: they call them
    as ordinary module-level functions (e.g. ``_main.get_session_user_id(...)`` inside
    a router). ``app.dependency_overrides`` therefore does not reach them. This
    context manager patches the helpers on every module that binds them, so a test
    that opts in to a state sees that state no matter which router serves the route.

    A context manager is used instead of an autouse fixture so every test must
    NAME the state it wants; silence means the real functions run and the test fails
    hard, which is exactly the point of issue #1212.
    """
    import importlib

    from cqc_lem.utilities import auth_factors

    # Build the stubbed helpers for this state.
    def _get_session_user_id(_token: Optional[str] = None) -> Optional[int]:
        return user_id

    def _require_session_user_id(_token: Optional[str] = None) -> int:
        if user_id is None:
            from fastapi import HTTPException

            raise HTTPException(status_code=401, detail="Invalid or expired session")
        return user_id

    def _get_user_email(_user_id: int) -> Optional[str]:
        return email

    def _user_owns_posts(_user_id: int, _post_ids: list[int]) -> bool:
        return owns_posts

    def _has_strong_factor(_user_id: int) -> bool:
        return not no_strong_factor

    def _step_up_satisfied(
        _user_id: int, _token: Optional[str] = None, extension_scope_ok: bool = False
    ) -> bool:
        return step_up_satisfied

    def _enrollment_allowed(_user_id: int, _token: Optional[str] = None) -> bool:
        return enrollment_allowed

    def _enrollment_required(_user_id: int) -> bool:
        return enrollment_required

    def _enrollment_hold_active() -> bool:
        return enrollment_hold_active

    def _session_signed_in_with_recovery_code(_token: Optional[str] = None) -> bool:
        return recovery_code_session

    def _has_confirmed_totp(_user_id: int) -> bool:
        return has_confirmed_totp

    overrides_by_name = {
        "get_session_user_id": _get_session_user_id,
        "require_session_user_id": _require_session_user_id,
        "get_user_email": _get_user_email,
        "user_owns_posts": _user_owns_posts,
    }
    auth_factor_overrides_by_name = {
        "has_strong_factor": _has_strong_factor,
        "step_up_satisfied": _step_up_satisfied,
        "enrollment_allowed": _enrollment_allowed,
        "enrollment_required": _enrollment_required,
        "enrollment_hold_active": _enrollment_hold_active,
        "session_signed_in_with_recovery_code": _session_signed_in_with_recovery_code,
        "has_confirmed_totp": _has_confirmed_totp,
    }

    # Patch every API module that binds these names. Because the routers import helpers from
    # `auth_factors` into their own namespace, patching only `auth_factors.<name>` is not enough;
    # the stubs have to replace the bound name in each router too.
    hosts = [importlib.import_module(m) for m in (_MAIN, _AUTH, _USER)]
    patched: list[tuple[Any, str, Any]] = []
    for module in hosts:
        for name, stub in {**overrides_by_name, **auth_factor_overrides_by_name}.items():
            if hasattr(module, name):
                original = getattr(module, name)
                setattr(module, name, stub)
                patched.append((module, name, original))

    # Also patch auth_factors itself, so imports that happen later or code that reads the module
    # directly see the same stubs.
    auth_factor_patched: list[tuple[Any, str, Any]] = []
    for name, stub in auth_factor_overrides_by_name.items():
        if hasattr(auth_factors, name):
            original = getattr(auth_factors, name)
            setattr(auth_factors, name, stub)
            auth_factor_patched.append((auth_factors, name, original))

    # `record_auth_event` is re-exported to several modules; silence it so foreign-target
    # 403 cases do not attempt a real DB write.
    audit_patched: list[tuple[Any, str, Any]] = []
    _record_auth_event = lambda *_args, **_kwargs: True  # noqa: E731
    for module in hosts:
        if hasattr(module, "record_auth_event"):
            original = getattr(module, "record_auth_event")
            setattr(module, "record_auth_event", _record_auth_event)
            audit_patched.append((module, "record_auth_event", original))

    try:
        yield
    finally:
        for module, name, original in patched + audit_patched:
            setattr(module, name, original)
        for module, name, original in auth_factor_patched:
            setattr(module, name, original)


@pytest.fixture
def signed_in() -> Iterator[int]:
    """Any token resolves to SESSION_USER_ID, who owns every post named.

    This is the pre-2c state most API tests were written against: the account has no
    strong factor, step-up is trivially satisfied, enrolment is not required, and the
    caller owns every post. Tests that need a different state should use a narrower
    patch or a dedicated fixture.
    """
    with _stated_account_state(
        user_id=SESSION_USER_ID,
        email=SESSION_EMAIL,
        owns_posts=True,
        no_strong_factor=True,
        step_up_satisfied=True,
        enrollment_allowed=True,
        enrollment_required=False,
        enrollment_hold_active=False,
        recovery_code_session=False,
        has_confirmed_totp=False,
    ):
        yield SESSION_USER_ID


@pytest.fixture
def no_session() -> Iterator[None]:
    """Every token resolves to no user, so 401 paths can be exercised."""
    with _stated_account_state(user_id=None):
        yield
