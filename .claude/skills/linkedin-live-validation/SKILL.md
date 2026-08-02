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

Flags (combine as needed): `--user-id` (default 1) · `--post-url` (own-post render + stats) · `--probe-composer` (composer controls, opens then Escapes) · `--comment-outcome-url` + `--our-slug` + `--comment-text` (comment-outcome readability) · `--dm-thread-url` + `--dm-thread-name` (thread resolution) · `--article-editor-url` (newsletter editor; never clicks Next, so `publish` grades UNKNOWN not MISSING) · `--feed-sort` (Recent-sort control) · `--reaction-probe` + `--reaction-cards N` + `--reaction-open-menu` · `--watch` (pins the noVNC debug node).

Reading the report: a `post_stats` signal sourced from `none` while the analytics page plainly shows a number = layout drift — the `*_lines` arrays hold the new layout; update the selector rows in `docs/sdui-selenium-notes.md` §3. Paste the JSON into issue #404.

Fix invariants: locators are `data-testid` / `aria-label` / href / TEXT — **never class names**. Composer lookups scoped to their OWN card (`_post_composer_for_card` / `_reply_composer_for_comment`), no page-wide fallback. **A selector miss is a DEBUG no-op, never `log_warning`** (a repeated warning files a defect). Strip non-BMP emoji before `send_keys` (`_strip_non_bmp()`). No-browser alternative for post stats: `scripts/linkedin_post_stats_api_probe.py --post-id <lem id>`.

Authoritative: `docs/LIVE_VALIDATION_FORMAT_AND_STATS.md` §4, `docs/sdui-selenium-notes.md`, `docs/SELENIUM_DEBUGGING.md`.
