"""Types and field defaults shared by every `/api` router (#1154).

`ResponseModel` lived in `api/main.py`, which made it unreachable from a router module without an
import cycle. It has no dependencies of its own, so it moves here rather than being reached through
the host module the way the auth kernel is. `main` re-exports it, so the 363 annotations already
written against `cqc_lem.api.main.ResponseModel` are untouched.

Everything here shares that one property: it binds inside a pydantic CLASS BODY, at IMPORT time.
That is the line between this module and the auth kernel. A kernel symbol is read at REQUEST time,
so a router reaches it as `_main.<name>` and the ~596 patches aimed at `cqc_lem.api.main` keep
binding what the handler reads. A class-body default has no such moment — by the time `_main` is
imported (last, below the routes) the class is already built — so a value used in class bodies on
BOTH sides of a split has to live somewhere neither side owns.
"""

from typing import Annotated, Generic, Optional, TypeVar

from pydantic import BaseModel, Field

# Nothing here is read by this module — every name is for somebody else, which is the whole point of
# it. `__all__` is the one declaration ruff and CodeQL both understand; without it CodeQL files
# py/unused-global-variable against `_LEN_DM_TEMPLATE` (a leading underscore reads as private, so an
# unread one reads as dead), and a per-line ruff directive is invisible to CodeQL.
__all__ = ["DetailT", "ResponseModel", "SessionTokenField", "_LEN_DM_TEMPLATE", "error_responses"]

DetailT = TypeVar("DetailT")


class ResponseModel(BaseModel, Generic[DetailT]):
    """The envelope almost every route returns.

    Bare `ResponseModel` still means `detail: Any` — pydantic treats an unparametrized type variable
    as `Any` — so a route that has no single payload shape (or a caller constructing the envelope by
    hand) is unchanged, on the wire and in the schema.

    Parametrizing it is what the public schema is for (#1219): `/api/openapi.json` has been world-
    readable since #1020, and an `Any` `detail` documents nothing about what a route returns.
    `ResponseModel[str]` says "a message", `ResponseModel[dict[str, Any]]` says "an object",
    `ResponseModel[list[dict[str, Any]]]` says "an array of objects".

    The parameter is deliberately a CONTAINER type, not a per-route field model. FastAPI serializes
    the response THROUGH this annotation, so a model with named fields would silently drop every key
    it does not declare — the SPA would lose data to a docs change. The container types below add
    real information to the schema while leaving the bytes on the wire byte-for-byte identical.
    """

    status_code: int
    detail: DetailT


# The OpenAPI `responses=` block shared by ~35 routes. Data, no dependencies, so it lives here for
# the same reason `ResponseModel` does: a router module cannot import a NAME out of `main` without
# a cycle. `main` re-exports it.
error_responses = {
    400: {"description": "Bad Request"},
    401: {"description": "Unauthorized"},
    403: {"description": "Forbidden"},
    404: {"description": "Not Found"},
    405: {"description": "Method Not Allowed"},
    422: {"description": "Unprocessable Entity"}
}

# A `session_token` on a request model is a LIVE credential for a non-browser caller (the SPA sends
# the non-secret sentinel, but a script or the extension sends the real thing). `repr=False` keeps
# it out of every f-string one of these models ever lands in: `/update_post/` carried a
# `f"Received Post Request: {post}"` log and gained a token field in the same change (#914), so the
# leak was one line away from live. Deleting that line fixed the instance; this makes the class of
# line harmless, which is the only version that survives the next handler author.
SessionTokenField = Annotated[Optional[str], Field(default=None, repr=False)]

# dm_templates.template_text (TEXT; app cap). One of the input length limits kept in lockstep with
# the DB column widths — the rest of that block is still in `main`; this one is here because the
# `/api/user` router's `DmTemplateItem` and `main`'s own scheduled-DM models both read it in a class
# body, so neither module can own it.
_LEN_DM_TEMPLATE = 2000
