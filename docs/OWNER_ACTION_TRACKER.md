# Owner action tracker

**Audited 2026-08-07** against prod (`/opt/lem`, v0.133.1, healthy, 5/5 workers), GitHub repo state,
host crontabs and systemd timers, and all 67 `docs/*.md`.

This is the one list of things **only Chris can do** — credentials, env vars, cron installs,
third-party dashboards, and live-LinkedIn grounding runs. Everything else is either shipped or
routed to the agent pipeline.

> **How to use it:** work top-down. P0 is data-loss exposure. The tracker records evidence for every
> claim, because the previous list (`Needs_From_Chris.md`) drifted into asserting things that were
> already done — see [§5](#5-docs-that-were-lying-to-you).

---

## 1. P0 — do these first

### 1.1 Nightly DB backups were dead for a month · issue #1090

`/opt/lem/logs/backup.log` ran daily to **2026-07-08**, then nothing until a manual run today. One
`db-*.sql.gz` exists where `RETAIN_DAYS=7` should leave seven.

Cause: `backup.sh` sources `.env`, and `EMAIL_FROM_NAME` held an unquoted value with spaces, so bash
tried to execute `Engagement`. **Already fixed on the VPS today** — the value is quoted and a manual
backup succeeded. The repo hardening (don't source raw `.env`, fail loudly, watchdog freshness
check) is `agent:ready` on #1090.

**Your action:** none required, but confirm tomorrow's 03:00 cron produced a new file:
```sh
ls -lht /opt/lem/backups | head -3
```

### 1.2 No off-box backup exists at all

`BACKUP_REMOTE` is unset and `rclone` is not installed, so `scripts/backup.sh:42` skips the remote
copy. Combined with 1.1 and 1.3, one disk loss is total loss.

**Your action:** pick a destination (S3/B2/Drive), `rclone config`, then set `BACKUP_REMOTE` in
`/opt/lem/.env`. Documented at `docs/DEPLOYMENT.md:202-207` — currently marked "(Optional)", which
under `ENCRYPTION_REQUIRED=true` it no longer is.

### 1.3 The master key backup still lives on the box it protects · issue #1095

`/home/lem/lem-secret-key-backup.txt` exists on the same machine as the database it decrypts.
`docs/secrets-at-rest.md:48-49`: *"Losing it means losing every stored LinkedIn session, token and
password — there is no recovery path, by design."* Encryption is now **enforced**, so this file is
the only recovery path.

**Your action (~2 min):** copy to a password manager / offline storage, verify the fingerprint reads
`version 1, e5aae98566d6ff0f`, then `shred -u` the on-box copy.

---

## 2. P1 — automation that is silently not running

### 2.1 The agent pipeline is running stale code

The repo's `scripts/agent-pipeline/tick.sh` is 89,185 bytes (today); the installed
`/home/lem/agent-pipeline/tick.sh` is 75,502 bytes (2026-08-05). **Today's fixes to both auto-fix
lanes are not live until you re-install.**

**Your action:** `scripts/agent-pipeline/install.sh`
**Before running:** confirm no on-box hotfixes since Aug 5 would be overwritten — the installer
copies repo → box in one direction.

### 2.2 The weekly SDUI drift check was never installed

`scripts/weekly_sdui_drift_check.sh` exists and is executable; `crontab -l` has **zero** SDUI
entries. The "one issue per drift" safety net CLAUDE.md describes has therefore never fired — which
is why #964, #1009, #1012, #970, #1007 and #1006 were each found dead by hand instead.

**Your action:**
```sh
40 6 * * 1 /home/lem/linkedin_engagement_manager/scripts/weekly_sdui_drift_check.sh
```
Also set `SDUI_PROBE_PROFILE_URL` in `/opt/lem/.env` to a **2nd/3rd-degree** profile, or the degree
badge stays ungrounded (`docs/sdui-probe-coverage.md:99-101`).

### 2.3 The perf/margin snapshot cron runs the superseded script

Your crontab runs `/home/lem/perf-tracking/snapshot.sh` (Jul 24, zero `margin` references). The repo
successor `scripts/perf_snapshot.sh` has 12. **The daily cost/margin block has never been captured.**

**Your action:** repoint the 23:30 entry at
`/home/lem/linkedin_engagement_manager/scripts/perf_snapshot.sh`.

### 2.4 YouTube OAuth token is dead · issue #1094

`invalid_grant: Token has been expired or revoked`. Runbook: `docs/youtube-publishing.md:79-98`.
Note `:84-85` — *do not* chase the External/verification path; the project must be Workspace-owned
with the consent screen **Internal**.

**Your action:** re-mint, then install without a deploy:
```sh
curl -X POST https://lem.christopherqueenconsulting.com/api/admin/youtube-token \
  -H "x-admin-secret: $ADMIN_SECRET" -H 'Content-Type: application/json' \
  -d '{"refresh_token":"1//0g..."}'
```

### 2.5 The realtime ops ping has no destination

`POSTHOG_OPS_WEBHOOK_URL` is unset, so the 429-breaker alert is inert.
`docs/posthog-advanced-surface.md:202-206` calls this out as its own open item — the only missing
piece is which channel to ping.

**Your action:** create a Slack incoming webhook → set the var → `python scripts/posthog_ops_destination.py --apply`.

---

## 3. Credentials — the reconciliation you asked for

**Everything the system needs is present except one.** No PAT is missing except `ANTHROPIC_API_KEY`.

| Credential | Lives in | Status |
|---|---|---|
| `ANTHROPIC_API_KEY` | repo secret + `/opt/lem/.env` | **MISSING in both.** Only effect: the `lem-complex` Claude fallback in `.litellm/config.yaml:165` is dead config. Everything else routes fine. |
| `RELEASE_DISPATCH_TOKEN` | repo secret | Present |
| `GHCR_PAT` | repo secret | Present |
| `FEEDBACK_GITHUB_TOKEN` | `/opt/lem/.env` | Present |
| `AGENT_GH_TOKEN` | `agent-pipeline/config.env` | Present, scoped (`AGENT_REQUIRE_SCOPED_TOKEN=1`) |
| `UI_API_TOKEN` | — | **Correctly deleted** today (#965) |
| `API_ACCESS_TOKENS` | `/opt/lem/.env` | Present, **rotated today** (#965) |
| `LEM_SECRET_KEY` | `/opt/lem/.env` | Present — but see 1.3 |
| `POSTHOG_PERSONAL_API_KEY` | repo secret + `.env` | Present. **Scopes unverified** — see the trap below |
| `POSTHOG_CLI_TOKEN`, `UI_POSTHOG_KEY` | repo secrets | Present |
| `CF_API_TOKEN` / `CF_ZONE_ID` | repo secrets | Present |
| `CODECOV_TOKEN`, `GITGUARDIAN_API_KEY`, `LITELLM_MASTER_KEY`, `OPENROUTER_API_KEY`, `PEXELS_API_KEY`, `VPS_*` | repo secrets | Present |
| `REPLICATE_API_TOKEN`, `REPLICATE_USERNAME`, `CAPSOLVER_API_KEY` | `/opt/lem/.env` only | Missing as **repo secrets**; `e2e-coverage.yml` is `continue-on-error` and the tests auto-skip, so E2E Replicate/CapSolver paths have never run in CI. Low priority. |
| `AWS_*` (4 secrets, 2025-02) | repo secrets | Present but **dormant** — the CDK path was declared unsupported today (#973) |
| `CQCLEMAZUREAPP_*` (5 secrets, 2025-01) | repo secrets | **OBSOLETE — zero references anywhere in `.github/`, `scripts/`, `src/`. Safe to delete.** |
| `CWS_*` (Chrome Web Store) | — | **Not needed.** PR #336 was closed today; only required if you resume store publishing |

**The PostHog scope trap.** One variable name carries **eight different scope requirements** across
the docs (query, insight/dashboard/alert/action/subscription, annotation, feature-flag + experiment,
survey, endpoint/insight_variable…). A key provisioned for one script silently blocks another —
symptoms are `blocked_goal`, `blocked_endpoint`, or empty `subscribed_users`. **I could not read the
live key's scopes.** If a PostHog script misbehaves, check scopes before debugging the script.

---

## 4. Feature flags that are OFF, and what each needs first

| Flag | Blocked on |
|---|---|
| `APPRECIATION_SOURCES_ENABLED` | #1002 — grounded GREEN today; now just needs the flip after #1019 deploys |
| `STALE_INVITE_WITHDRAWAL_ENABLED` | #1006 — **fully grounded today** (read path, confirm dialog, entity check). Flip is now purely your call |
| `TUTORIAL_VIDEOS_ENABLED` | `ELEVENLABS_API_KEY` (unset), `TUTORIAL_DEMO_SESSION_TOKEN` (unset), and #1094 above |
| `COST_ROUTING_ENABLED` + `COST_AWARE_ROUTING_ENABLED` | Set **both together**, recreate app + litellm, preview with `python -m cqc_lem.utilities.cost_routing --json` |
| `POSTHOG_SURVEYS_ENABLED` | Precondition now met (`UI_POSTHOG_KEY` exists). Still needs `posthog_surveys.py --apply --launch` |
| `REQUIRE_STRONG_FACTOR_AFTER` | Unset — mandatory passkey/TOTP enrolment is built and dormant. `docs/strong-authentication.md:373-376`: *"Nobody has scheduled the deadline yet."* Operator decision |
| `EARLY_ADOPTER_TRIAL_ENABLED` | Explicitly `False`; `EARLY_ADOPTER_COUPON_ID` empty |
| `AI_DETECTOR_ENABLED`, `C2PA_ENABLED`, `COMMENT_RESEARCH_ENABLED` | Deliberately off; each needs its own key/cert. No action |
| Avatar surfaces, `roster_auto_follow`, `roster_auto_connect`, `cover_image_auto` | Per-user, opt-in by design |

---

## 5. Docs that were lying to you

These asserted work was pending that is **already done**. Corrected separately; listed so you know
not to act on them if you read them before the fix lands.

- `docs/AUTH_SECURITY_DESIGN.md:315-321` — "every bundled token is still public and still valid
  until #965 rotates them". Rotated today; old values 401.
- `docs/secrets-at-rest.md:60,113-121` and `docs/AUTH_SECURITY_DESIGN.md:253` and `CLAUDE.md:176` —
  `ENCRYPTION_REQUIRED` framed as a future flip. It is ON, and reads are fail-closed.
- `Needs_From_Chris.md:56-71` — error cron and PostHog tokens listed as pending. All done. **Only
  `ANTHROPIC_API_KEY` on line 15 is still real.**
- `docs/contribution-security.md:124-149` — create the scoped PAT and set
  `AGENT_REQUIRE_SCOPED_TOKEN=1`. Both done.
- `docs/stack-watchdog.md:44-53` — install the watchdog. Armed; timer green.
- `docs/SELENIUM_GRID.md:3` — "built, NOT enabled in prod". The grid **is** the deployed topology
  (8 nodes + hub, 10 days). The file contradicts itself at `:38`.
- `docs/DEPLOYMENT.md:88-93` — "delete the repo secret". Done.
- `docs/SETUP_CHECKLIST.md` — 46 unticked boxes that are a bootstrap checklist, not live work.

---

## 6. Known-wrong facts worth correcting

- **CI gate names.** `CLAUDE.md:211-217`, `docs/DEPLOYMENT.md:96-99` and
  `docs/SETUP_CHECKLIST.md:88-93` list four required checks. The real six are `Unit Tests (Python
  3.12)`, `Integration Tests`, `GitGuardian Scan`, `UI Build`, `Migration Versions`, `CodeQL PR
  Quality Gate`. Two required gates are missing from every list, and `CodeQL Security Analysis` is
  named as required when it is not.
- **"≥1 review" is not enforced.** `required_approving_review_count: 0`. `docs/contribution-security.md:92-93`
  has this right; DEPLOYMENT and SETUP_CHECKLIST do not.
- **Flower basic auth is not on.** `docs/DEPLOYMENT.md:27` claims "Cloudflare Access + Flower basic
  auth"; the `--basic_auth` line is commented out and `CELERY_FLOWER_PASSWORD` is unset. Cloudflare
  Access is the only gate.
- **`.github/agents/code-review.agent.md:33`** still instructs the review agent to reject docstring
  blocks — it will reject the very docstrings the new gate requires. Must be updated with #1100.

---

## 7. Owner-owed live grounding (LinkedIn, tracked as issues)

| What | Issue | Note |
|---|---|---|
| Supervised avatar render | #744 | Declare attributes → regenerate → approve. ~3 min |
| Degree badge re-check after deploy | #1031 | Phase 3 of #1021 |
| Supervised first newsletter publish | — | Publish dialog has never been live-validated |
| Populate `include_authors` roster | — | `docs/engagement-growth-analysis-2026-07.md:30`: *"has never been populated"* |
| Story-bank entries | — | Empty bank means placeholder-only drafts, approval-gated |
| Gmail auto-forward filter for reply detection | — | `docs/REPLY_NOTIFICATIONS.md:29-34` |
| Set `users.linkedin_display_name` | — | Required setting, never scraped |
| LinkedIn profile re-index (skills, headline, About) | — | `docs/linkedin-reindex-playbook.md` — entirely manual |

---

## 8. Could not verify — treat as unknown, not as done

1. **All PostHog dashboard-side state** — whether `posthog_provision.py` / `_experiments` /
   `_flags` / `_surveys` `--apply` have ever run, whether replay sampling and error alerts exist.
   Env prerequisites are all present; the UI state was not readable.
2. **`POSTHOG_PERSONAL_API_KEY` scopes** (see the trap in §3).
3. **Whether an external uptime monitor is actually armed** on `/health/deep`.
   `docs/stack-watchdog.md:172-176`: *"a silently disarmed dead-man's switch is the one failure this
   layer cannot report about itself."*
4. **DB-level state** — admin flag, `linkedin_display_name`, story-bank rows, orphaned cookies.
5. **SendGrid/DNS** — SPF, DKIM and DMARC policy level.
6. **Stripe live-vs-test mode** — `STRIPE_API_KEY` is set; the value was not read.
