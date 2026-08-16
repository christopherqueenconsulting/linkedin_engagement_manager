"""The three types the layered-restructure audit warranted (issue #1154, Phase 3).

`PostEngagementRow` names the columns of a stat row that `utilities/margin.py` and
`utilities/post_stats.py` used to read by hardcoded index; `FeedRunContext` and `PostDraftContext`
name the bundle of per-run inputs that was previously re-threaded through long parameter lists.

No I/O and no `cqc_lem.platform` / `cqc_lem.app` imports: the DB row layout is asserted at the
reader boundary (`PostEngagementRow.from_row`), not by the repository, so `platform/db` keeps
knowing nothing about the domain. Selenium handles are carried as `Any` for the same reason — a
context passes the driver along, it never drives it.
"""

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, NamedTuple, Optional

__all__ = ["FeedRunContext", "PostDraftContext", "PostEngagementRow"]


class PostEngagementRow(NamedTuple):
    """One post's latest captured stats, as `db.get_post_engagement_rows` returns them.

    The reader modules used to carry their own copy of the column layout (`_IDX_IMPRESSIONS = 9`,
    spelled out in two files), so a change to that SELECT had to be found in three places or it
    would silently score the wrong column. Still a tuple, so anything that unpacks or indexes a row
    keeps working.

    `impressions` trails the tuple because only the author's own view exposes it — it may be NULL,
    and `post_stats` uses that to decide whether engagement RATE is scoreable at all.
    """

    scheduled_time: Optional[datetime] = None
    reactions: Optional[int] = None
    comments: Optional[int] = None
    reposts: Optional[int] = None
    archetype: Optional[str] = None
    hook_style: Optional[str] = None
    format: Optional[str] = None
    topic: Optional[str] = None
    buyer_stage: Optional[str] = None
    impressions: Optional[int] = None

    @classmethod
    def from_row(cls, row: Any) -> "PostEngagementRow":
        """Coerce one raw DB row (or an already-coerced one) into named columns.

        A SHORT row is padded with None rather than rejected: callers legitimately pass the
        four-column minimum `(scheduled_time, reactions, comments, reposts)`, and the old
        index-with-a-length-check readers treated a missing column as unknown. Extra trailing
        columns are ignored for the same reason — a widened SELECT must not break scoring.
        """
        if isinstance(row, cls):
            return row
        return cls(*tuple(row)[:len(cls._fields)])


@dataclass(frozen=True)
class FeedRunContext:
    """Everything ONE feed engagement run decided before it looked at a single card.

    Built once in `comment_on_feed_inline` and passed to the roster pass and to each card, so the
    two passes cannot drift on which preferences, voice synthesis or dedup state they are working
    from — they used to receive the same eight arguments through two different parameter orders.

    Frozen, but three fields are the run's mutable ACCUMULATORS and are meant to be appended to:
    `seen` (dedup keys, shared so a roster post is never re-commented from the feed),
    `used_comment_shapes` (per-run archetype rotation) and `recent_comments` (what the similarity
    gate dedups each fresh draft against). Freezing the context is what stops a callee swapping one
    of them for a fresh object, which would silently disable the gate it feeds.
    """

    driver: Any
    wait: Any
    my_profile: Any
    user_id: int
    prefs: dict
    profile_synthesis: Any = None
    seen: set = field(default_factory=set)
    used_comment_shapes: list = field(default_factory=list)
    recent_comments: list = field(default_factory=list)
    engagers: set = field(default_factory=set)
    deadline_ts: Optional[float] = None
    # True only for the LinkedIn GROUP feed lane, which resolves the comment composer before
    # spending an LLM call (issue #1084). It is a property of the RUN, not of a card — the roster
    # pass never adopts it, because a roster target's activity page is not a group feed.
    is_group_feed: bool = False

    def out_of_time(self, now: float) -> bool:
        """Has this run's deadline passed? An unset deadline means the run is budget-bounded only.

        `now` is passed in rather than read here: the callers are Selenium walks that already have
        the timestamp, and a domain type that reads the clock stops being trivially testable.
        """
        return bool(self.deadline_ts) and now >= self.deadline_ts


@dataclass(frozen=True)
class PostDraftContext:
    """The resolved inputs ONE text-post draft is written from.

    Everything here is settled before generation starts — the shape blueprint, the story-bank
    anchor, the lead-magnet CTA, the post-history avoid block, the 70/20/10 class. The generators
    and the type-fallback path all need the same set, which is why `create_text_post` used to repeat
    a ten-keyword argument block four times; the fallback path is `with_post_type`, so a retry can
    never quietly drop one of them.

    `refine_final_post` / `similarity_check` are the once-per-post markers: the refinement,
    authenticity, humanization and review gates run once per post, and a caller that is writing a
    draft some other pass will finish (a regenerate flow, a fallback caption) turns them off. Since
    the `create_text_post` breakup (issue #1217) a RETRY never turns them off itself: the type
    fallback and the review gate's regeneration are bounded loops around the generate step alone, so
    the gates sit outside the loop and cannot run twice on one post.
    """

    user_id: int
    stage: str
    post_type: str
    user_profile: Any = None
    prefs: Optional[dict] = None
    profile_synthesis: Any = None
    blueprint: Optional[dict] = None
    post_id: Optional[int] = None
    lead_magnet_cta: Optional[str] = None
    history_directive: Optional[str] = None
    story_directive: Optional[str] = None
    content_mix: Optional[str] = None
    refine_final_post: bool = True
    similarity_check: bool = True

    def with_post_type(self, post_type: str) -> "PostDraftContext":
        """The same draft, retried as another post type after its source produced nothing.

        The retry is not a fresh post: it keeps the blueprint, story anchor and CTA already chosen
        so the fallback stays the post that was planned. It leaves the once-per-post markers alone
        (issue #1217): the fallback is one turn of a bounded loop around the generate step, and the
        gates those markers guard run after that loop, so standing them down here would disable
        them for the whole post rather than for one attempt.
        """
        return replace(self, post_type=post_type)

    def with_history_directive(self, history_directive: str) -> "PostDraftContext":
        """The same draft, rewritten against an explicit avoid / proof / no-invention directive.

        The review gate's ONE regeneration (issue #1217): everything the post was written from
        stays settled — profile, story anchor, blueprint, CTA — and only the steering the first
        draft failed changes, so the retry is the planned post written again rather than a new one.
        """
        return replace(self, history_directive=history_directive)
