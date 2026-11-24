"""Shared state for the MCP endpoint tests (epic #2682).

`PUBLIC_BASE_URL` is set by an AUTOUSE fixture on purpose: the lifespan reads it when `api_client`
starts the app, and pytest instantiates autouse fixtures before the ones a test names, so the
value is in place before startup rather than after it.

Two more autouse defaults keep the gate hermetic past the bearer step (P2): the resolver answers
"no such session" and the exposure revoke finds nothing, unless a test says otherwise through the
`resolver` / `exposure` fixtures.
"""

from typing import Any, Iterator, Optional
from unittest.mock import MagicMock, patch

import pytest

BASE_URL = "http://testserver"


class FakeRedis:
    """An in-memory Redis for the gate: the kill key, plus the counters the later limits use.

    `kill_value` answers `get("mcp:kill")` (P1's shape); every other key lives in `store`.
    `raises` makes every call fail like a dropped connection; `fail_on` fails only the named
    commands, for a fault in the middle of a sequence. `expiries` records every TTL set, so a test
    can tell a TTL set once from one refreshed.
    """

    def __init__(self, kill_value: Optional[bytes] = None, raises: bool = False) -> None:
        self.kill_value = kill_value
        self.raises = raises
        self.keys_read: list[str] = []
        self.store: dict[str, Any] = {}
        self.expiries: dict[str, int] = {}
        self.ttl_writes: list[str] = []
        self.fail_on: set[str] = set()

    def _check(self, command: str = "") -> None:
        if self.raises or command in self.fail_on:
            raise ConnectionError("redis down")

    def get(self, key: str) -> Optional[Any]:
        """Return the stored value (the kill value for `mcp:kill`)."""
        self.keys_read.append(key)
        self._check()
        if key == "mcp:kill":
            return self.kill_value
        return self.store.get(key)

    def set(self, key: str, value: Any, ex: Optional[int] = None, nx: bool = False) -> bool:
        """Store a value; `nx` refuses an existing key."""
        self._check("set")
        if key == "mcp:kill":
            self.kill_value = value if isinstance(value, bytes) else str(value).encode()
            return True
        if nx and key in self.store:
            return False
        self.store[key] = value
        if ex is not None:
            self.expiries[key] = ex
            self.ttl_writes.append(key)
        return True

    def delete(self, key: str) -> int:
        """Remove a key."""
        self._check()
        if key == "mcp:kill":
            existed, self.kill_value = self.kill_value is not None, None
            return int(existed)
        return int(self.store.pop(key, None) is not None)

    def incr(self, key: str) -> int:
        """Increment a counter."""
        self._check("incr")
        self.store[key] = int(self.store.get(key) or 0) + 1
        return self.store[key]

    def decr(self, key: str) -> int:
        """Decrement a counter."""
        self._check("decr")
        self.store[key] = int(self.store.get(key) or 0) - 1
        return self.store[key]

    def expire(self, key: str, seconds: int) -> bool:
        """Record a TTL."""
        self._check("expire")
        self.expiries[key] = seconds
        self.ttl_writes.append(key)
        return True


@pytest.fixture(autouse=True)
def _mcp_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A configured public base URL matching TestClient's host, and no env kill switch."""
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE_URL)
    monkeypatch.delenv("MCP_SERVER_KILL", raising=False)


@pytest.fixture(autouse=True)
def resolver() -> Iterator[MagicMock]:
    """The gate's session lookup, answering "no such session" unless a test sets a row."""
    with patch("cqc_lem.api.mcp.gate.resolve_session_checked", return_value=None) as mock:
        yield mock


@pytest.fixture(autouse=True)
def exposure() -> Iterator[MagicMock]:
    """The gate's exposure revoke, finding nothing unless a test sets a row."""
    with patch("cqc_lem.api.mcp.gate.revoke_session_by_token", return_value=None) as mock:
        yield mock


@pytest.fixture
def fake_redis() -> Iterator[FakeRedis]:
    """A readable Redis with no kill key set, patched where the kill switch reads it."""
    fake = FakeRedis()
    with patch("cqc_lem.api.mcp.limits._redis", return_value=fake):
        yield fake
