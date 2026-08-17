# `cqc_lem/app/engagement` — how this tree is wired

Scoped context for the engagement lanes. The root `CLAUDE.md` keeps one row per lane (the ONE
place and the invariant that bites) and `docs/engagement-automation.md` holds the full posture;
what lives here is the stuff you can only get wrong by editing a file in **this** tree.

## One module per lane (#1154)

| Module | What it owns |
|---|---|
| `feed.py` | the feed walk, the group composer, the roster tail |
| `posting.py` | publishing (`post_to_linkedin`) and every sweep measuring what a post earned — reply sweep, comment follow-ups, comment outcomes, post/audience stats |
| `outreach.py` | DMs and who gets one |
| `invites.py` | company-page invites, roster connect escalation, stale-invite withdrawal |
| `newsletter.py` | newsletter editions and their covers |

## The task name is a WIRE IDENTIFIER, not a module path

Every task here pins its name explicitly:

```python
@shared_task.task(name='cqc_lem.app.run_automation.<fn>', ...)
```

`app/run_automation.py` was emptied by #1154 and **deleted** by #1206. The name is still spelled
`run_automation` because it is the identifier a queued message carries — **moving a task RENAMES
it**, and a renamed task is one that in-flight messages and `celeryconfig.task_routes` can no
longer route to. It is still correct in `task_routes`; never "correct" it.
`tests/unit/app/test_task_name_stability.py` holds both halves and fails the build on a drift.

## Import and patch from the module that DEFINES the task

Because `run_automation.py` no longer exists, `run_scheduler` and `api/*` import each task from
its defining module. Patch targets follow the definition, not a re-export: patch
`cqc_lem.app.engagement.outreach.send_dm_now`, not wherever it was imported to.

## Flags in this tree default OFF

Every feature flag named on an engagement lane is off unless explicitly enabled. The ONE
exception is `STALE_INVITE_WITHDRAWAL_ENABLED`, on since #1006 grounded it live.

Safety controls are **not** flags and never will be — the 429 breaker, the automation pause and
the per-day caps are unconditional (`utilities/flags.py`, `docs/AUTOMATION_COOLDOWN.md`).

## Every lane opens a browser through the shared session helper

`get_current_profile` / `browser_session` (`utilities/linkedin/session.py`) are the only way a
lane gets a driver, and a Selenium slot held past its use is one another lane wanted. A
rate-limited session returns cleanly rather than raising — see the 429 posture in
`utilities/linkedin/rate_limit.py`.

Full posture for every lane: **`docs/engagement-automation.md`**.
