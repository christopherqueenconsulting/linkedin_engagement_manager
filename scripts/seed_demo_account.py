"""Seed a LOCAL database with the synthetic demo account used for product recordings (issue #2371).

    PYTHONPATH=src poetry run python scripts/seed_demo_account.py --scene demo1
    PYTHONPATH=src poetry run python scripts/seed_demo_account.py --scene demo2 --anchor-date 2026-10-12

The account is "Dana Reyes, Reyes Advisory": a fictional person with a fictional voice, posting three
times a week (Mon / Wed / Fri) on a 30-day plan. Every run resets that ONE account's posts and
preferences and then writes the scene's state, so running a scene twice gives the same rows. No other
account is read or written: the account is identified by an address on a reserved example domain, and
every reset statement is bound to it.

Scenes:

* demo1: three PENDING drafts for the coming week.
* demo2: one draft held by the authenticity gate (seeded score 58 against a seeded minimum of 70), one
  held by the similarity gate (62% overlap against a seeded 40% maximum) together with the POSTED post
  it overlaps, and one REJECTED draft with its reason. These numbers are seeded for the recording, not
  platform defaults.
* demo3: the voice fields are empty, and one PENDING draft is already written in the voice the
  presenter types in during the recording (`DEMO3_NEW_VOICE`).

NEVER run this on the production VPS. Production MySQL is published on 127.0.0.1:3306 there (for
SSH-tunnel database GUIs), and dev checkouts live on the same host, so from a shell on the VPS, or
through an SSH tunnel to it, "127.0.0.1" IS production. The host check below cannot see that.

Checks, in order, before anything is written (exit 2 = refused, 3 = could not connect):

1. Production markers: `ENVIRONMENT`, `APP_ENV` or `ENV` naming production, or
   `ENCRYPTION_REQUIRED=true` (set only on the production stack). Defence in depth only: production
   is not required to set any of them, so their absence proves nothing.
2. Host: `localhost` / `127.0.0.1` / `::1`, or a docker-compose service (or a compose `hostname` /
   `container_name`) defined in this repository's compose files. A host taken from an AWS secret is
   refused. This rules out a remote server by name, but not the production DB behind 127.0.0.1.
3. Connection probe: a database that cannot be reached exits 3 with the connector's reason.
4. Database fingerprint (the real control): a read-only count of what belongs to anyone other than
   a demo account. Any POSTED post owned by a non-demo account, or more than
   `MAX_NON_DEMO_USERS` non-demo accounts, refuses. A fingerprint that cannot be read refuses too:
   an unreadable database is never treated as a local one.

`--teardown` removes the demo account and every row it owns, behind the same checks.

It makes no LinkedIn call and opens no browser: it only imports the database facade and the pure
gate-finding builders, and it turns PostHog flag loading off for its own process.
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = REPO_ROOT / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# A local seed must publish nothing: no telemetry (issue #1661) and no PostHog flag-definition load,
# which is the one network request the preferences upsert could otherwise trigger. Both are set
# before cqc_lem is imported, because the handlers are built at import time.
os.environ.setdefault("LEM_TELEMETRY_MUTED", "1")
os.environ["POSTHOG_FLAGS_ENABLED"] = "0"

import mysql.connector  # noqa: E402

from cqc_lem.platform.db import connection as db_connection  # noqa: E402
from cqc_lem.utilities.db import (  # noqa: E402
    PostStatus,
    PostType,
    ensure_demo_user,
    get_demo_db_fingerprint,
    insert_demo_post,
    insert_planned_post,
    mark_email_verified,
    reset_demo_user_rows,
    soft_delete_posts,
    teardown_demo_user,
    update_db_post_authenticity_score,
    update_db_post_gate_reason,
    update_engagement_preferences,
    update_user_linkedin_display_name,
    update_user_timezone,
)
from cqc_lem.utilities.quality_gates import authenticity_finding, similarity_finding  # noqa: E402

EXIT_OK, EXIT_WRITE_FAILED, EXIT_REFUSED, EXIT_NO_CONNECTION = 0, 1, 2, 3
#: A local database may hold a few test sign-ups besides the demo account; production holds many.
MAX_NON_DEMO_USERS = 3

SCENES = ("demo1", "demo2", "demo3")

DEMO_EMAIL = "dana.reyes@example.com"
DEMO_NAME = "Dana Reyes"
DEMO_COMPANY = "Reyes Advisory"
DEMO_TIMEZONE = "America/New_York"
POSTING_DAYS = [0, 2, 4]  # Mon, Wed, Fri (0 = Monday, the `posting_days` convention)
POSTS_PER_WEEK = 3
PLAN_DAYS = 30
LOCAL_POST_HOUR = 9

# Seeded gate values for demo2. Fictional, chosen for the recording.
AUTHENTICITY_MIN = 70
AUTHENTICITY_SEEDED_SCORE = 58
SIMILARITY_MAX_PCT = 40
SIMILARITY_SEEDED_OVERLAP = 0.62
REJECTION_REASON = "Too salesy, no example"

#: The voice fields: the four the onboarding checklist reads as "voice set" (`utilities/onboarding.py`).
VOICE_FIELDS = ("tone", "comment_style", "focus_topics", "include_topics")

BASE_VOICE = {
    "tone": "Plainspoken and practical, a little wry",
    "comment_style": "Lead with a concrete moment from a client engagement; no hype",
    "focus_topics": ["fixed-scope pricing", "scoping calls", "client handoffs"],
    "include_topics": ["consulting", "pricing", "proposals"],
}
EMPTY_VOICE = {"tone": None, "comment_style": None, "focus_topics": [], "include_topics": []}
#: What the presenter enters on the Account page during the demo3 recording.
DEMO3_NEW_VOICE = {
    "tone": "Direct and warm, short sentences",
    "comment_style": "Open with a client story, end with one practical step",
    "focus_topics": ["fixed-scope pricing", "proposal writing"],
    "include_topics": ["consulting", "proposals"],
}

BASE_PREFERENCES = {
    "posts_per_week": POSTS_PER_WEEK,
    "posting_days": POSTING_DAYS,
    "authenticity_score_min": AUTHENTICITY_MIN,
    "post_similarity_max_pct": SIMILARITY_MAX_PCT,
    "business_goals": f"Win two fixed-scope advisory engagements a quarter for {DEMO_COMPANY}",
    "personal_goals": "Write about pricing the way I wish someone had explained it to me",
    "use_emojis": False,
    "use_hashtags": False,
}

DEMO1_OPENINGS = (
    "I priced my first fixed-scope project by the hour",
    "Three questions I ask in every scoping call",
    "The handoff document my clients actually read",
)
POSTED_OPENING = "What I'd tell my first fixed-scope client"

_DEMO1_DRAFTS = (
    (DEMO1_OPENINGS[0] + ", then added 20% and called it a fixed fee.\n\n"
     "It ran six weeks over. Every extra week came out of my margin, not the client's budget.\n\n"
     "What changed on the next one:\n"
     "- I priced the outcome the client named in the first call, not my hours.\n"
     "- I wrote down three things that were out of scope, in their words.\n"
     "- I set one checkpoint halfway through where either of us could re-scope.\n\n"
     "The hours estimate is still in my notebook. It just isn't the price any more.\n\n"
     "How did you price your first fixed-scope engagement?"),
    (DEMO1_OPENINGS[1] + ":\n\n"
     "1. What happens if this project never ships?\n"
     "2. Who has to say yes before you can say yes?\n"
     "3. What would make you call this a failure in six months?\n\n"
     "The first tells me what the work is worth. The second tells me how long the decision will "
     "really take. The third gives me acceptance criteria before anyone writes a statement of work.\n\n"
     "My early scoping calls were me describing my process. These three questions moved the call "
     "onto the client's problem, and my proposals got shorter."),
    (DEMO1_OPENINGS[2] + " is one page long.\n\n"
     "For years I wrote thirty-page handoffs. Nobody opened them after the final meeting.\n\n"
     "The one page has four parts:\n"
     "- What we built, in one sentence.\n"
     "- The three decisions we made, and why.\n"
     "- Who to call when something breaks.\n"
     "- What I would do next if the engagement continued.\n\n"
     "The test I use now: could someone who missed every meeting pick it up and run with it?"),
)

_POSTED_POST = (
    POSTED_OPENING + ":\n\n"
    "The price is fixed because the scope is. When one moves, the other has to.\n\n"
    "I didn't say that out loud on my first project, so every new request felt like a favour I "
    "couldn't refuse. By week eight we were both frustrated, and neither of us had done anything "
    "wrong.\n\n"
    "Now it's the second paragraph of every proposal I send.")

_SIMILAR_DRAFT = (
    "If I could talk to my first fixed-scope client again, I'd tell them one thing:\n\n"
    "The price is fixed because the scope is fixed. When one moves, the other has to move too.\n\n"
    "I never said that out loud on that first project, so every new request felt like a favour I "
    "couldn't turn down. By week eight we were both frustrated.\n\n"
    "It's now the second paragraph of every proposal I send.")

_GENERIC_DRAFT = (
    "Scope creep is the silent killer of consulting profitability.\n\n"
    "In today's fast-paced business environment, clear boundaries matter more than ever. The most "
    "successful advisors set expectations early, communicate often and protect their time.\n\n"
    "Five ways to keep your projects on track:\n"
    "1. Define success up front\n2. Document everything\n3. Hold regular check-ins\n"
    "4. Say no to out-of-scope requests\n5. Review lessons learned\n\n"
    "What's your best tip for managing scope?")
_AUTHENTICITY_REASONS = [
    "Opens with a generic claim any consultant could have written",
    "No first-hand moment, number or client detail",
]

_SALESY_DRAFT = (
    "Struggling to price your consulting work?\n\n"
    f"The {DEMO_COMPANY} Fixed-Scope Pricing Workshop is open for enrolment. Seats are limited, so "
    "book your spot today and stop leaving money on the table!")

_DEMO3_DRAFT = (
    "A client asked me last week why my proposals are two pages.\n\n"
    "Because the person who decides reads two pages. Anything longer goes to procurement and comes "
    "back with questions.\n\n"
    "Page one is the problem in their words and the outcome we agreed. Page two is the price, the "
    "scope and what is out of it.\n\n"
    "One practical step: before you send your next proposal, cut every sentence that describes your "
    "process. Keep the ones that describe their result.")


class SeedRefused(RuntimeError):
    """A guard refused the run before anything was written."""


class SeedFailed(RuntimeError):
    """A database write did not land, so the scene is incomplete."""


@dataclass
class DemoPost:
    """One fully written post in a scene, and what the gates recorded on it."""

    content: str
    status: PostStatus
    scheduled_time: datetime
    buyer_stage: str
    content_mix: str = "value"
    authenticity_score: Optional[int] = None
    gate_findings: list = field(default_factory=list)
    rejection_reason: Optional[str] = None


@dataclass
class Scene:
    """Everything a scene writes: the preferences, the written posts and the empty plan slots."""

    name: str
    preferences: dict
    posts: list
    plan_slots: list


# --------------------------------------------------------------------------- guards

_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_SERVICE_KEY = re.compile(r"^  ([A-Za-z0-9][A-Za-z0-9_.-]*):\s*(?:#.*)?$")
_HOST_KEY = re.compile(r"^\s+(?:hostname|container_name):\s*['\"]?([^'\"#\s]+)")
_PLACEHOLDER = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*(?::?-([^}]*))?\}$")
_SINGLE_LABEL = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
_PRODUCTION_WORDS = frozenset({"prod", "production", "live"})


def compose_db_hosts(repo_root: Path = REPO_ROOT) -> tuple[set, bool]:
    """Read the service names and literal hostnames from the repo's docker-compose files.

    A line parser rather than a YAML load, because the grid overlay uses compose-only tags (`!override`)
    that a plain YAML loader rejects.

    Args:
        repo_root: The checkout whose `docker-compose*.yml` files are read.

    Returns:
        The accepted names, and whether any `hostname` / `container_name` is an unresolved
        `${VAR}` placeholder (whose runtime value the caller then has to judge).
    """
    names: set = set()
    has_placeholder = False
    for path in sorted(repo_root.glob("docker-compose*.yml")):
        in_services = False
        for line in path.read_text(encoding="utf-8").splitlines():
            if line and not line[0].isspace():
                in_services = line.split("#", 1)[0].strip() == "services:"
                continue
            if not in_services:
                continue
            service = _SERVICE_KEY.match(line)
            if service:
                names.add(service.group(1).lower())
                continue
            host = _HOST_KEY.match(line)
            if host:
                value = host.group(1)
                placeholder = _PLACEHOLDER.match(value)
                if placeholder:
                    has_placeholder = True
                    if placeholder.group(1):
                        names.add(placeholder.group(1).lower())
                else:
                    names.add(value.lower())
    return names, has_placeholder


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def check_db_host(host: Optional[str], compose_names: set, has_placeholder: bool) -> Optional[str]:
    """Decide whether the database host is local, and say why not when it is not.

    A compose `hostname: ${MYSQL_HOST}` cannot be resolved without trusting the very value being
    checked, so for that case only a single DNS label (no dots, not an IP address) is accepted: that
    is a name on the compose network, while anything with a dot or an address could be a remote server.

    Args:
        host: The host the connector will use.
        compose_names: Service names and literal hostnames from `compose_db_hosts`.
        has_placeholder: Whether a compose hostname is an unresolved placeholder.

    Returns:
        None when the host is accepted, otherwise the refusal reason.
    """
    value = (host or "").strip().lower()
    if not value:
        return "MYSQL_HOST is not set; export it so the target database is explicit"
    if value in _LOCAL_HOSTS:
        return None
    if value in compose_names:
        return None
    if has_placeholder and _SINGLE_LABEL.match(value) and not _is_ip(value):
        return None
    return (f"database host {value!r} is not localhost or a docker-compose service in this "
            f"repository; refusing to seed it")


def production_indicator(env: dict) -> Optional[str]:
    """Name the environment variable that marks this process as production, if any.

    Args:
        env: The process environment.

    Returns:
        A refusal reason, or None when nothing indicates production.
    """
    for var in ("ENVIRONMENT", "APP_ENV", "ENV"):
        if (env.get(var) or "").strip().lower() in _PRODUCTION_WORDS:
            return f"{var}={env.get(var)} indicates production"
    if (env.get("ENCRYPTION_REQUIRED") or "").strip().lower() in ("1", "true", "yes", "on"):
        return "ENCRYPTION_REQUIRED is on, which only the production stack sets"
    return None


def guard(env: dict, host: Optional[str], aws_secret_name: Optional[str],
          repo_root: Path = REPO_ROOT) -> Optional[str]:
    """Run every guard; return the first refusal reason, or None when the seed may run.

    Args:
        env: The process environment.
        host: The host the connector will use.
        aws_secret_name: Set when the connector takes its host from an AWS secret.
        repo_root: The checkout whose compose files define the accepted service names.

    Returns:
        A refusal reason, or None.
    """
    reason = production_indicator(env)
    if reason:
        return reason
    if aws_secret_name:
        return "the database host comes from an AWS secret and cannot be checked before connecting"
    names, has_placeholder = compose_db_hosts(repo_root)
    return check_db_host(host, names, has_placeholder)


def fingerprint_refusal(fingerprint: dict) -> Optional[str]:
    """Decide from the read-only fingerprint whether this database could be a real one.

    Args:
        fingerprint: What `get_demo_db_fingerprint` counted.

    Returns:
        A refusal reason, or None when the database looks like a local development one.
    """
    posted = int(fingerprint.get("non_demo_posted_posts", 0))
    users = int(fingerprint.get("non_demo_users", 0))
    if posted:
        return (f"the database holds {posted} POSTED post(s) owned by non-demo accounts; "
                "this looks like a real database")
    if users > MAX_NON_DEMO_USERS:
        return (f"the database holds {users} non-demo accounts (more than {MAX_NON_DEMO_USERS}); "
                "this looks like a real database")
    return None


def _connection_target() -> str:
    return f"{db_connection.MYSQL_HOST}:{db_connection.MYSQL_PORT or db_connection.DEFAULT_MYSQL_PORT}"


def preflight(env: dict) -> tuple[int, Optional[str]]:
    """Run every check that must pass before the first write.

    Args:
        env: The process environment.

    Returns:
        `(EXIT_OK, None)` when the seed may write, otherwise the exit code and the reason.
    """
    reason = guard(env, db_connection.MYSQL_HOST, db_connection.AWS_MYSQL_SECRET_NAME)
    if reason:
        return EXIT_REFUSED, f"refused: {reason}"
    try:
        db_connection.get_db_connection().close()
    except mysql.connector.Error as exc:
        return EXIT_NO_CONNECTION, (
            f"could not connect to MySQL at {_connection_target()} "
            f"(errno {getattr(exc, 'errno', None)}: {exc}); nothing was written")
    # The real control. The checks above are name-based and the production DB is reachable as
    # 127.0.0.1 from the VPS; only the CONTENTS of the database can tell the two apart. Fail closed:
    # a fingerprint that cannot be read is a refusal, never an empty local database.
    try:
        fingerprint = get_demo_db_fingerprint()
    except mysql.connector.Error as exc:
        return EXIT_REFUSED, f"refused: the database fingerprint could not be read ({exc})"
    reason = fingerprint_refusal(fingerprint)
    if reason:
        return EXIT_REFUSED, f"refused: {reason}"
    return EXIT_OK, None


# --------------------------------------------------------------------------- scenes


def _slot(day: date, tz: ZoneInfo) -> datetime:
    return datetime.combine(day, time(LOCAL_POST_HOUR), tzinfo=tz).astimezone(timezone.utc)


def plan_slots(anchor: date, days: int = PLAN_DAYS, tz_name: str = DEMO_TIMEZONE) -> list:
    """The posting slots of the 30-day plan: every Mon / Wed / Fri after `anchor`, at 09:00 local."""
    tz = ZoneInfo(tz_name)
    return [_slot(anchor + timedelta(days=n), tz) for n in range(1, days + 1)
            if (anchor + timedelta(days=n)).weekday() in POSTING_DAYS]


def last_posting_slot_before(anchor: date, tz_name: str = DEMO_TIMEZONE) -> datetime:
    """The most recent Mon / Wed / Fri slot strictly before `anchor`."""
    day = anchor - timedelta(days=1)
    while day.weekday() not in POSTING_DAYS:
        day -= timedelta(days=1)
    return _slot(day, ZoneInfo(tz_name))


def build_scene(name: str, anchor: date) -> Scene:
    """Describe a scene without touching the database.

    Args:
        name: One of `SCENES`.
        anchor: The day the recording happens; the plan starts the day after.

    Returns:
        The scene's preferences, written posts and the plan slots left as skeletons.

    Raises:
        ValueError: Unknown scene.
    """
    if name not in SCENES:
        raise ValueError(f"unknown scene {name!r}; expected one of {', '.join(SCENES)}")
    slots = plan_slots(anchor)
    posts: list = []
    voice = dict(BASE_VOICE)

    if name == "demo1":
        stages = ("awareness", "consideration", "decision")
        posts = [DemoPost(text, PostStatus.PENDING, slots[i], stages[i])
                 for i, text in enumerate(_DEMO1_DRAFTS)]
    elif name == "demo2":
        posts = [
            DemoPost(_POSTED_POST, PostStatus.POSTED, last_posting_slot_before(anchor),
                     "consideration"),
            DemoPost(_GENERIC_DRAFT, PostStatus.PENDING, slots[0], "awareness",
                     authenticity_score=AUTHENTICITY_SEEDED_SCORE,
                     gate_findings=[authenticity_finding(AUTHENTICITY_SEEDED_SCORE, AUTHENTICITY_MIN,
                                                         _AUTHENTICITY_REASONS)]),
            DemoPost(_SIMILAR_DRAFT, PostStatus.PENDING, slots[1], "consideration",
                     gate_findings=[similarity_finding(SIMILARITY_SEEDED_OVERLAP,
                                                       SIMILARITY_MAX_PCT / 100,
                                                       matched_excerpt=_POSTED_POST,
                                                       measure="lexical")]),
            DemoPost(_SALESY_DRAFT, PostStatus.REJECTED, slots[2], "decision", content_mix="promo",
                     rejection_reason=REJECTION_REASON),
        ]
    else:
        voice = dict(EMPTY_VOICE)
        posts = [DemoPost(_DEMO3_DRAFT, PostStatus.PENDING, slots[0], "consideration")]

    taken = {p.scheduled_time for p in posts}
    return Scene(name=name, preferences={**BASE_PREFERENCES, **voice}, posts=posts,
                 plan_slots=[s for s in slots if s not in taken])


def _require(ok: object, what: str) -> None:
    if not ok:
        raise SeedFailed(f"database write failed: {what}")


def apply_scene(scene: Scene) -> dict:
    """Reset the demo account and write `scene` through the app's own repository functions.

    Args:
        scene: What `build_scene` described.

    Returns:
        A summary: the account id and how many rows of each kind were written.

    Raises:
        SeedFailed: A write did not land; the account may be half-seeded, so re-run the scene.
    """
    user_id = ensure_demo_user(DEMO_EMAIL)
    _require(user_id, "create the demo account")
    _require(reset_demo_user_rows(user_id, DEMO_EMAIL) is not None, "reset the demo account")
    _require(update_user_linkedin_display_name(user_id, DEMO_NAME), "set the display name")
    _require(update_user_timezone(user_id, DEMO_TIMEZONE), "set the timezone")
    _require(mark_email_verified(user_id), "mark the email verified")
    _require(update_engagement_preferences(user_id, scene.preferences), "write the preferences")

    for post in scene.posts:
        post_id = insert_demo_post(user_id, DEMO_EMAIL, post.content, post.status,
                                   post.scheduled_time, PostType.TEXT, post.buyer_stage,
                                   post.content_mix)
        _require(post_id, "insert a post")
        if post.authenticity_score is not None:
            _require(update_db_post_authenticity_score(post_id, post.authenticity_score),
                     "store the authenticity score")
        if post.gate_findings:
            _require(update_db_post_gate_reason(post_id, post.gate_findings),
                     "store the gate findings")
        if post.rejection_reason:
            # Inserted as written, then rejected the way the Review queue rejects one.
            _require(soft_delete_posts([post_id], rejection_reason=post.rejection_reason,
                                       user_id=user_id), "reject the post")

    for slot in scene.plan_slots:
        _require(insert_planned_post(user_id, slot, PostType.TEXT, "awareness", "value"),
                 "insert a plan slot")

    return {"user_id": user_id, "scene": scene.name, "posts": len(scene.posts),
            "plan_slots": len(scene.plan_slots)}


# --------------------------------------------------------------------------- CLI


def _default_anchor() -> date:
    return datetime.now(ZoneInfo(DEMO_TIMEZONE)).date()


def parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    """Parse the command line."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--scene", choices=SCENES)
    action.add_argument("--teardown", action="store_true",
                        help="remove the demo account and every row it owns")
    parser.add_argument("--anchor-date", type=date.fromisoformat, default=None,
                        help="the recording day (YYYY-MM-DD); defaults to today in "
                             f"{DEMO_TIMEZONE}. Same scene + same day = same rows.")
    return parser.parse_args(argv)


def main(argv: Optional[list] = None) -> int:
    """Check, then seed or tear down.

    Returns:
        0 done, 1 a write failed, 2 refused (nothing written), 3 could not connect (nothing written).
    """
    args = parse_args(argv)
    code, reason = preflight(dict(os.environ))
    if code != EXIT_OK:
        sys.stderr.write(f"seed_demo_account: {reason}\n")
        return code

    if args.teardown:
        counts = teardown_demo_user(DEMO_EMAIL)
        if counts is None:
            sys.stderr.write("seed_demo_account: database write failed: tear down the demo account\n")
            return EXIT_WRITE_FAILED
        sys.stdout.write(f"Removed the demo account {DEMO_EMAIL}: {counts}\n")
        return EXIT_OK

    scene = build_scene(args.scene, args.anchor_date or _default_anchor())
    try:
        summary = apply_scene(scene)
    except SeedFailed as exc:
        sys.stderr.write(f"seed_demo_account: {exc}; the scene may be half-written, run it again\n")
        return EXIT_WRITE_FAILED
    sys.stdout.write(
        f"Seeded {summary['scene']} for {DEMO_NAME} ({DEMO_EMAIL}, user {summary['user_id']}): "
        f"{summary['posts']} written posts, {summary['plan_slots']} plan slots.\n")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
