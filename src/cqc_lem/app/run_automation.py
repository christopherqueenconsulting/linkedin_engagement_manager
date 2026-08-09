"""The stable import name for LEM's engagement tasks — a re-export shim, and nothing else (#1154).

Every engagement action LEM takes AS the user used to live here, in one 9,162-line module. All of it
now lives in `app/engagement/`: feed + groups + the roster tail in `feed.py`, the connect rail in
`invites.py`, the newsletter rail in `newsletter.py`, publishing plus the post-publish sweeps in
`posting.py`, and DMs plus outreach — appreciation, the profile-viewer walk, the connect-candidate
scan, the outreach funnel and the catch-up lane — in `outreach.py`. The reusable Selenium mechanics
they share went down to `utilities/linkedin/*`. The per-beat posture is `docs/engagement-automation.md`.

**This file defines no behaviour. Patch the module that OWNS the code, not this one.** A patch
against a name re-exported here binds this module's global, which the code that reads it never
looks at — the patch would bind nothing and the test would pass having tested nothing.
`tests/unit/app/test_engagement_core_patch_seam.py` fails the build on exactly that mistake, and the
private helpers are deliberately NOT re-exported so a stale patch raises `AttributeError` instead.

What IS re-exported, and why it has to be:

- **The 39 tasks.** A Celery task's name is `<module>.<function>` unless pinned, so moving one
  RENAMES it silently — routed queues stop matching, in-flight messages are rejected `NotRegistered`
  and dropped, and the `QueueOnce` lock key re-keys mid-deploy. Every moved task therefore pins
  `name='cqc_lem.app.run_automation.<fn>'`; the module path below is the only thing that changed
  about any of them. `run_scheduler`, `api/main.py` and `api/routers/*` import them from HERE by
  name, and this block is what keeps that spelling working.
- **`get_feed_funnel`** — not a task; `api/routers/user.py` reads it from here to fill the reach panel.
- **`report_catchup_run` and the nine `CATCHUP_*` constants** — `run_scheduler` labels its catch-up
  scan/send reports with them. They are now SOURCED from `app.engagement.outreach`, which is what
  the split's abort criterion for step 5 asked to be proved.

`load_dotenv()` still runs here because `app/__init__.py` imports this module first and has always
had that side effect at that point in the import order.
"""

from dotenv import load_dotenv

# Feed, groups and roster live in `app.engagement.feed` (#1154) — the largest cluster, and the one
# that took the whole SDUI card engine with it. `get_feed_funnel` is the one non-task in the list:
# `api/routers/user.py` reads it from here.
#
# The ~100 private helpers that moved with them — the feed engine (`_score_feed_post`,
# `_engage_card`, `comment_on_feed_inline`), the roster tail (`comment_on_roster_posts`,
# `auto_follow_roster_target`, `advance_roster_connect`) and the group composer — are deliberately
# NOT re-exported.
from cqc_lem.app.engagement.feed import (
    auto_comment_in_groups,
    auto_draft_group_post,
    auto_post_to_group,
    auto_second_wave_comment,
    auto_seed_comment_on_post,
    auto_sync_user_groups,
    automate_commenting,
    comment_on_post,
    consolidate_duplicate_comments_for_user,
    get_feed_funnel,
)

# The connect rail lives in `app.engagement.invites` (#1154). The five PRIVATE helpers that moved
# with it (`_profile_is_first_degree`, `_open_connect_invite_dialog`, `_add_connect_note`,
# `_submit_connect_invite`, `invite_to_connect_now`) are deliberately NOT re-exported.
from cqc_lem.app.engagement.invites import (
    automate_invites_to_company_page_for_user,
    clean_stale_invites,
    invite_to_connect,
    send_connection_request,
    send_roster_connect_invite,
)

# The newsletter rail lives in `app.engagement.newsletter` (#1154). `run_scheduler` imports
# `auto_publish_edition` and `track_newsletter_subscribers` from here by name;
# `auto_publish_newsletter_edition` is re-exported with them so the rail keeps ONE import path
# rather than two that differ for no reason a reader can see. The seven PRIVATE helpers that moved
# with them (`_fill_edition_description`, `_fill_and_publish_article`, `_approved_cover_path`,
# `_tagged_edition_body`, `_parse_subscriber_count`, `_read_newsletter_subscriber_count`,
# `_invite_connections_to_newsletter`) are deliberately NOT re-exported.
from cqc_lem.app.engagement.newsletter import (
    auto_publish_edition,
    auto_publish_newsletter_edition,
    track_newsletter_subscribers,
)

# DMs and outreach live in `app.engagement.outreach` (#1154) — appreciation, the profile-viewer
# walk, the connect-candidate scan, the outreach funnel and the catch-up lane, which are one graph
# because they all end at the same send and the same follow-up ladder. `run_scheduler` imports five
# of these tasks by name plus `report_catchup_run` and the nine `CATCHUP_*` labels below;
# `api/main.py` imports `send_lead_response` and `api/routers/admin.py`
# `automate_appreciation_dms_for_user` and `send_private_dm`. The other tasks ride along so the
# cluster keeps ONE import path.
#
# The ~150 PRIVATE helpers that moved with them — the appreciation-source readers, the DM composer
# and its landed-check, the nurture ladder, the funnel drafters and the whole catch-up card walk —
# are deliberately NOT re-exported, and neither are the public non-task helpers
# (`send_dm_now`, `build_dm_from_template`, `check_dm_replied`, `accept_connection_request`, …):
# every one of them is patched by name somewhere, and absence is what makes a stale patch loud.
from cqc_lem.app.engagement.outreach import (
    CATCHUP_PHASE_SCAN,
    CATCHUP_PHASE_SEND,
    CATCHUP_STATUS_AWAITING_APPROVAL,
    CATCHUP_STATUS_CAPPED,
    CATCHUP_STATUS_DISABLED,
    CATCHUP_STATUS_DISPATCHED,
    CATCHUP_STATUS_INACTIVE,
    CATCHUP_STATUS_NOTHING_TO_SEND,
    CATCHUP_STATUS_THROTTLED,
    automate_appreciation_dms_for_user,
    automate_catchup_touches,
    automate_profile_viewer_engagement,
    engage_with_profile_viewer,
    process_outreach_funnel,
    process_user_followups,
    report_catchup_run,
    scan_connection_candidates,
    scan_outreach_funnel_targets,
    send_catchup_touch,
    send_lead_response,
    send_private_dm,
    send_scheduled_dm,
)

# Publishing and the post-publish sweeps live in `app.engagement.posting` (#1154). `run_scheduler`
# imports six by name, `api/main.py` two, `api/routers/admin.py` one and `api/routers/user.py` one;
# `process_comment_followups_for_url` and `reconcile_recent_comment_urns` ride along so the cluster
# keeps ONE import path. The ~60 PRIVATE helpers that moved with them — the reply rail
# (`_reply_to_comments_on_open_post`), the follow-up sweep, the comment-sort control walk, the
# outcome reader and the post-stats parsers — are deliberately NOT re-exported.
from cqc_lem.app.engagement.posting import (
    auto_scrape_post_stats,
    automate_reply_commenting,
    capture_follower_stats,
    post_to_linkedin,
    process_comment_followups_for_url,
    reconcile_recent_comment_urns,
    sweep_comment_followups,
    sweep_comment_outcomes,
    sweep_reply_comments,
    update_stale_profile,
)

__all__ = [
    # The feed / groups / roster tasks, plus `get_feed_funnel`, which is not a task.
    "auto_comment_in_groups",
    "auto_draft_group_post",
    "auto_post_to_group",
    "auto_second_wave_comment",
    "auto_seed_comment_on_post",
    "auto_sync_user_groups",
    "automate_commenting",
    "comment_on_post",
    "consolidate_duplicate_comments_for_user",
    "get_feed_funnel",
    # The connect-rail tasks.
    "automate_invites_to_company_page_for_user",
    "clean_stale_invites",
    "invite_to_connect",
    "send_connection_request",
    "send_roster_connect_invite",
    # The newsletter-rail tasks.
    "auto_publish_edition",
    "auto_publish_newsletter_edition",
    "track_newsletter_subscribers",
    # The posting + post-publish sweep tasks.
    "auto_scrape_post_stats",
    "automate_reply_commenting",
    "capture_follower_stats",
    "post_to_linkedin",
    "process_comment_followups_for_url",
    "reconcile_recent_comment_urns",
    "sweep_comment_followups",
    "sweep_comment_outcomes",
    "sweep_reply_comments",
    "update_stale_profile",
    # The DM + outreach tasks, plus `report_catchup_run` and the catch-up phase/status vocabulary
    # `run_scheduler` labels its scan and send reports with.
    "automate_appreciation_dms_for_user",
    "automate_catchup_touches",
    "automate_profile_viewer_engagement",
    "engage_with_profile_viewer",
    "process_outreach_funnel",
    "process_user_followups",
    "report_catchup_run",
    "scan_connection_candidates",
    "scan_outreach_funnel_targets",
    "send_catchup_touch",
    "send_lead_response",
    "send_private_dm",
    "send_scheduled_dm",
    "CATCHUP_PHASE_SCAN",
    "CATCHUP_PHASE_SEND",
    "CATCHUP_STATUS_AWAITING_APPROVAL",
    "CATCHUP_STATUS_CAPPED",
    "CATCHUP_STATUS_DISABLED",
    "CATCHUP_STATUS_DISPATCHED",
    "CATCHUP_STATUS_INACTIVE",
    "CATCHUP_STATUS_NOTHING_TO_SEND",
    "CATCHUP_STATUS_THROTTLED",
]

# Load .env file
load_dotenv()
