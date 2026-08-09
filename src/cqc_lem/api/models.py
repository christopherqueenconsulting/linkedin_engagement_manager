"""Response types shared by every `/api` router (#1154).

`ResponseModel` lived in `api/main.py`, which made it unreachable from a router module without an
import cycle. It has no dependencies of its own, so it moves here rather than being reached through
the host module the way the auth kernel is. `main` re-exports it, so the 363 annotations already
written against `cqc_lem.api.main.ResponseModel` are untouched.
"""

from typing import Any

from pydantic import BaseModel


class ResponseModel(BaseModel):
    """The envelope almost every route returns.

    `detail` is deliberately `Any`: a payload dict/list on success, a message string on failure —
    the same shape FastAPI gives an `HTTPException`, so the SPA reads success and error responses
    through one code path.
    """

    status_code: int
    detail: Any
