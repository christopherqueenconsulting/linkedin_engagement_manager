"""Shared defaults for the API unit lane.

Issue #745 (2c) put a step-up gate in front of every credential-touching endpoint and made the
email PIN conditional on whether the account holds a strong factor. Both ask the database two
questions that the ~40 test modules in this directory never mocked, because they did not exist —
and an unmocked read here is not a failed query, it is a `TypeError` out of the connection pool.

So this fixture answers those two questions with the state EVERY account is in until it enrols
something: no passkey, no authenticator app, therefore nothing to step up with and nothing to
demote the PIN for. That is the pre-2c behaviour these modules were written against.

`tests/unit/api/test_strong_auth.py` patches over it per-test to exercise both sides of the gate —
a nested `patch` wins over this one.
"""

from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _account_without_a_strong_factor():
    with patch("cqc_lem.api.main.has_strong_factor", return_value=False), \
         patch("cqc_lem.api.main.step_up_satisfied", return_value=True):
        yield
