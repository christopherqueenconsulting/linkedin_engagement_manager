"""`/api/curated/*` — curated outside sources (docs/curated-sources.md).

Three routes, all resolved through the ONE session resolver (`_main.get_session_user_id`, read at
request time — see `routers/outreach.py` for why it is reached as a module attribute):

- `GET /sources` lists the caller's candidates and drafts, with the guardrail verdict on each.
- `POST /source` queues a URL the owner pasted — an X post, an article — with the author/title/
  excerpt the owner supplies. LEM pays for no X reads, so the owner's own text IS the snapshot.
  It runs the same block gate as every collector.
- `PUT /source` approves or rejects the drafted post a source became.

An `agent`-scoped session may list and queue (both paths are on its surface) but may NEVER approve:
`_refuse_agent_approval` refuses `action="approve"` server-side, before anything is read.
"""

from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from cqc_lem.api.models import ResponseModel
from cqc_lem.utilities.curated_collectors import manual_candidate, record_candidate
from cqc_lem.utilities.curated_sources import (
    STATUS_APPROVED,
    STATUS_DRAFTED,
    STATUS_REJECTED,
    STATUSES,
    credit_line,
)
from cqc_lem.utilities.db import (
    PostStatus,
    bulk_update_posts,
    get_curated_source,
    get_curated_sources,
    get_post_curated_context,
    update_curated_source_status,
    update_db_post_status,
    user_approver,
)
from cqc_lem.utilities.logger import log_info

router = APIRouter(prefix="/api/curated")

_LEN_URL = 1024
_LEN_NAME = 255
_LEN_TITLE = 512
_LEN_EXCERPT = 2000


class CuratedSourceRequest(BaseModel):
    """Body of `POST /api/curated/source` — one URL the owner pasted, with who said what."""

    session_token: str
    url: str = Field(max_length=_LEN_URL)
    author: Optional[str] = Field(default=None, max_length=_LEN_NAME)
    publisher: Optional[str] = Field(default=None, max_length=_LEN_NAME)
    title: Optional[str] = Field(default=None, max_length=_LEN_TITLE)
    excerpt: Optional[str] = Field(default=None, max_length=_LEN_EXCERPT)
    licence: Optional[str] = Field(default=None, max_length=32)


class CuratedSourceActionRequest(BaseModel):
    """Body of `PUT /api/curated/source` — approve or reject the post a source became."""

    session_token: str
    source_id: int
    action: str  # 'approve' | 'reject'


def _caller(session_token: Optional[str]) -> int:
    user_id = _main.get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return user_id


def _summary(row: dict) -> dict:
    treatment = row.get("treatment")
    return {
        "source_id": row["id"], "platform": row.get("platform"), "url": row.get("url"),
        "author": row.get("author"), "publisher": row.get("publisher"), "title": row.get("title"),
        "licence": row.get("licence"), "status": row.get("status"),
        "block_reason": row.get("block_reason"), "post_id": row.get("post_id"),
        "link_only": bool(row.get("link_only")),
        "reshareable": bool(row.get("canonical_id")),
        "credit": credit_line(row, treatment or ""),
    }


@router.get("/sources")
def list_curated_sources(session_token: str,
                         status_filter: Optional[str] = None) -> ResponseModel[dict[str, Any]]:
    """The caller's curated candidates and drafts, newest first, each with its guardrail verdict.

    A read failure is a 503, never an empty list — "no sources" and "could not read them" are
    different answers.
    """
    user_id = _caller(session_token)
    if status_filter and status_filter not in STATUSES:
        raise HTTPException(status_code=422, detail=f"Unknown status '{status_filter}'")
    rows = get_curated_sources(user_id, status=status_filter)
    if rows is None:
        raise HTTPException(status_code=503, detail="Could not read curated sources")
    return ResponseModel(status_code=200, detail={"sources": [_summary(r) for r in rows]})


@router.post("/source")
def create_curated_source(request: CuratedSourceRequest) -> ResponseModel[dict[str, Any]]:
    """Queue a pasted URL as a curated candidate. It is screened exactly like a collected one.

    A blocked candidate is still recorded and returned with its reason, so the owner sees WHY
    (political, paywall, NC licence, excluded platform) instead of a silent no.
    """
    user_id = _caller(request.session_token)
    url = (request.url or "").strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=422, detail="url must be an http(s) URL")
    item = manual_candidate(url, author=request.author, title=request.title,
                            excerpt=request.excerpt, publisher=request.publisher,
                            licence=request.licence)
    row_id, status = record_candidate(user_id, item)
    if not row_id:
        raise HTTPException(status_code=409, detail="That URL is already a curated source")
    row = get_curated_source(row_id, user_id=user_id) or {}
    return ResponseModel(status_code=200, detail={"source_id": row_id, "status": status,
                                                  "block_reason": row.get("block_reason")})


@router.put("/source")
def act_on_curated_source(request: CuratedSourceActionRequest) -> ResponseModel[dict[str, Any]]:
    """Approve or reject the PENDING post a drafted source became.

    `approve` records the CALLER as the approver (`user:<id>`), which is the only approval the
    curated publisher accepts. An agent session is refused before anything is read.
    """
    if request.action not in ("approve", "reject"):
        raise HTTPException(status_code=422,
                            detail=f"Unknown action '{request.action}' — expected approve or reject")
    _main._refuse_agent_approval(request.action)
    user_id = _caller(request.session_token)
    source = get_curated_source(request.source_id, user_id=user_id)
    if not source:
        raise HTTPException(status_code=404, detail="Curated source not found")
    post_id = source.get("post_id")
    context = get_post_curated_context(post_id) if post_id else None
    if context and context.get("unreadable"):
        raise HTTPException(status_code=503, detail="Could not read the curated post")
    owned = bool(context) and context.get("post_user_id") == user_id
    if request.action == "approve":
        if source.get("status") != STATUS_DRAFTED or not owned:
            raise HTTPException(status_code=409, detail="This source has no draft to approve")
        if context.get("post_status") != PostStatus.PENDING.value:
            raise HTTPException(status_code=409, detail="Only a pending draft can be approved")
        if not update_db_post_status(post_id, PostStatus.APPROVED,
                                     approved_by=user_approver(user_id)):
            raise HTTPException(status_code=500, detail="Could not approve the curated post")
        update_curated_source_status(source["id"], STATUS_APPROVED)
        log_info("Curated post approved by its owner", user_id=user_id, post_id=post_id,
                 source_id=source["id"])
        return ResponseModel(status_code=200, detail={"source_id": source["id"],
                                                      "post_id": post_id, "status": "approved"})
    if owned and context.get("post_status") in (PostStatus.PENDING.value,
                                                PostStatus.APPROVED.value):
        bulk_update_posts([post_id], status=PostStatus.REJECTED,
                          rejection_reason="curated source rejected", user_id=user_id)
    if not update_curated_source_status(source["id"], STATUS_REJECTED):
        raise HTTPException(status_code=500, detail="Could not reject the curated source")
    return ResponseModel(status_code=200, detail={"source_id": source["id"], "post_id": post_id,
                                                  "status": "rejected"})


# LAST, for the same reason as `routers/outreach.py`: the router is complete before `main` reads it.
from cqc_lem.api import main as _main  # noqa: E402
