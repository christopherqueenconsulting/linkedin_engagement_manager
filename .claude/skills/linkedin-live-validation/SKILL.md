---
name: linkedin-live-validation
description: Use when a LinkedIn selector, scrape, or automation flow may have drifted, or before/after changing Selenium selectors — how to run the read-only live probe inside the selenium worker and read its JSON report.
---

# Live LinkedIn selector grounding

> **Who may RUN the probe: the owner, from an interactive session on the VPS.** A pipeline agent
> (`tick.sh` MODE=*) must **not** — the RUNBOOK forbids running `docker` and touching prod, headless
> lanes launch with `--dangerously-skip-permissions` so nothing will stop the command, and the probe
> drives the owner's **real LinkedIn session** (an account with a 429-lockout history) while taking a
> Chrome slot out of the pool the Selenium Celery lanes share. If an issue genuinely needs a live
> probe, that is an explicit RUNBOOK escalation (`needs-human` + a Decision Comment), not a command
> to run. The **Fix invariants** below apply to every agent and need no probe run.

The probe (`scripts/linkedin_live_validation.py`) is **read-only** — it navigates and reads, posts/comments/changes nothing. `scripts/` is not baked into the image, so pipe it in on stdin:

```bash
sudo docker exec -i celery_worker_selenium python - --user-id 1 \
    --post-url 'https://www.linkedin.com/feed/update/urn:li:activity:<id>/' \
    < scripts/linkedin_live_validation.py
```

Flags (combine as needed): `--user-id` (default 1) · `--surfaces` (print the surface → probe coverage matrix as JSON; no browser, no network) · `--sweep` [+ `--sweep-profile-url`] (every target-free probe in ONE session — what the weekly drift cron runs) · `--post-url` (own-post render + stats) · `--probe-composer` (composer controls, opens then Escapes) · `--comment-outcome-url` + `--our-slug` + `--comment-text` (comment-outcome readability) · `--dm-thread-url` + `--dm-thread-name` (thread resolution) · `--article-editor-url` (newsletter editor; never clicks Next, so `publish` grades UNKNOWN not MISSING) · `--feed-sort` (Recent-sort control) · `--reaction-probe` + `--reaction-cards N` + `--reaction-open-menu` · `--roster-follow <profile-url>` (top-card Follow/Following control on a roster target's activity page, #962 — resolves and describes it, clicks nothing) · `--roster-connect <profile-url>` (what that same top card says about our CONNECTION — Pending / 1st-degree / Message, #979 — read-only, sends no invite) · `--appreciation-sources` (Recommendations Received + mentions notification feed, #968 — reports per card what the appreciation-DM triggers would do; messages nobody, claims no ledger row) · `--sent-invites` [+ `--sent-invite-days N`] (pending sent invites and how their "Sent … ago" stamps parse, #969 — withdraws nothing; zero rows is graded against the page's own empty state, so a clean account and a rotated anchor read differently) · `--connect-dialog <profile-url>` (custom-invite URL renders the dialog? plus every "Invite … to connect" control naming SOMEONE ELSE, #1012 — never clicks Send or an Invite control) · `--profile-scrape <profile-url>` (name/headline/degree badge vs the page's own text; use a 2nd/3rd-degree profile or the degree half reads `degree_grounded: false`) · `--profile-experiences <profile-url>` (what the rebuilt experience parser reads off `/details/experience/`, beside the raw lines and every rung's hit count, #970) · `--catchup-cards` (#964) · `--group-composer <group-id>` (opens the share box, never clicks Post) · `--group-membership [<group-id>]` (#1052 — the groups directory as the shipped sync reads it, plus one group page's own join/leave controls; defaults to the first DB-enabled group, clicks nothing, joins/leaves nobody) · `--company-invite` [+ `--company-url`] (credits/invitee rows/checkboxes; ticks nothing, never clicks Invite) · `--permalink-comment <post-url>` (card → Comment action → composer on a post permalink, #966 — opens the composer and Escapes it, types and submits nothing) · `--watch` (pins the noVNC debug node).

Reading the report: every probe carries a three-state `state` next to its prose `verdict` — `ok` (chain resolved), `drift` (the PAGE shows content the locator can't see — the only state the weekly cron files an issue for), `unknown` (page didn't render; grounds nothing, never filed). A `post_stats` signal sourced from `none` while the analytics page plainly shows a number = layout drift — the `*_lines` arrays hold the new layout; update the selector rows in `docs/sdui-selenium-notes.md`. Paste the JSON into the issue.

Fix invariants: locators are `data-testid` / `aria-label` / href / TEXT — **never class names**. Composer lookups scoped to their OWN card (`_post_composer_for_card` / `_reply_composer_for_comment`), no page-wide fallback. **Success is the OUTCOME being present, never a click having landed**; **never click a control whose label names a different entity than the target**; **zero items is not "nothing to do" until the page agrees** (`_report_zero_walk`). **A selector miss is a DEBUG no-op, never `log_warning`** (a repeated warning files a defect). Strip non-BMP emoji before `send_keys` (`_strip_non_bmp()`). No-browser alternative for post stats: `scripts/linkedin_post_stats_api_probe.py --post-id <lem id>`.

Authoritative: `docs/sdui-probe-coverage.md` (surface → probe matrix + the weekly drift cron), `docs/LIVE_VALIDATION_FORMAT_AND_STATS.md` §4, `docs/sdui-selenium-notes.md`, `docs/SELENIUM_DEBUGGING.md`.
