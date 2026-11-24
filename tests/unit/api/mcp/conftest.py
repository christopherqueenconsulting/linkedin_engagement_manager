"""Shared state for the MCP endpoint tests (epic #2682).

`PUBLIC_BASE_URL` is set by an AUTOUSE fixture on purpose: the lifespan reads it when `api_client`
starts the app, and pytest instantiates autouse fixtures before the ones a test names, so the
value is in place before startup rather than after it.
"""

from typing import Iterator, Optional
from unittest.mock import patch

import pytest

BASE_URL = "http://testserver"


class FakeRedis:
    """The two Redis behaviours the kill switch can see: a value (or None), or a raising read."""

    def __init__(self, kill_value: Optional[bytes] = None, raises: bool = False) -> None:
        self.kill_value = kill_value
        self.raises = raises
        self.keys_read: list[str] = []

    def get(self, key: str) -> Optional[bytes]:
        """Return the stored kill value, or raise like a dropped connection."""
        self.keys_read.append(key)
        if self.raises:
            raise ConnectionError("redis down")
        return self.kill_value


@pytest.fixture(autouse=True)
def _mcp_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A configured public base URL matching TestClient's host, and no env kill switch."""
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE_URL)
    monkeypatch.delenv("MCP_SERVER_KILL", raising=False)


@pytest.fixture
def fake_redis() -> Iterator[FakeRedis]:
    """A readable Redis with no kill key set, patched where the kill switch reads it."""
    fake = FakeRedis()
    with patch("cqc_lem.api.mcp.limits._redis", return_value=fake):
        yield fake
