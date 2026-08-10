# LinkedIn profile re-index playbook

LinkedIn re-indexes a profile after edits. The play is to reorder the top-5 profile skills to
match the keywords you want to be known for, then echo those keywords in content the following
weeks so LinkedIn's index and your content agree. LEM automates the echo.

## How LEM helps

1. **Skills-change detection** — every successful profile re-scrape compares the current top-5
   skills to the last recorded snapshot. If the order or the set changed, LEM records the diff.
2. **14-day re-index window** — for ~14 days after a detected change, the top-5 skill keywords
   are woven into generated posts and comments as soft subject steering.
3. **Reconciliation panel** — in Settings → Content, the SPA shows your top-5 profile skills
   alongside your declared focus topics, highlighting overlap. One click adopts any missing skills
   as focus topics.

## The directive

The skills directive lives in `cqc_lem.utilities.profile_skills_window` and is the same shape as
`focus_directive` in `content_alignment.py`. It layers on top of existing steering and never
overrides the actual subject. Existing gates still decide what ships:

- topic-DNA on-niche gate (`content_alignment.py`)
- slop lint (`utilities/quality_gates.py`)
- post similarity gate

## Manual actions that trigger it

- **On-demand profile refresh** (`POST /user/linkedin-profile/refresh`, issue #1076) — a user who
  reorders their skills and presses refresh opens the window immediately.
- **Daily profile beat** — `update_stale_profile` runs for active users and records any change.

## Adopting skills as focus topics

Settings → Content shows a "Profile skills" panel when LEM has a cached profile. Skills already in
your focus topics are green; unadopted skills have an "Adopt … as focus topics" button. The button
writes through the existing `PUT /user/engagement-preferences` endpoint, merging the skills into
your current `focus_topics` without removing anything.

## Window state

- Opened in Redis with a 14-day TTL, so retries and task restarts never re-roll it.
- The `profiles.last_recorded_skills` DB column holds the snapshot that detection compares against,
  so Redis restarts do not create spurious windows.
- Unscored or unreadable profile diff = no window, no warning spam (expected no-op, logged DEBUG).

## Operations notes

- The window is a **steering hint**, not a bypass. If the topic-DNA gate or slop lint rejects a
  draft, it is still held/refused.
- The directive applies to the four auto post archetypes that carry `user_id` (thought leadership,
  industry news, personal story, engagement prompt) and to feed comments, seed/second-wave comments,
  thread replies, and comment-reply follow-ups.
