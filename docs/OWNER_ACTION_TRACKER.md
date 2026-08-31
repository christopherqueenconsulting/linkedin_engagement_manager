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

### 2.1 The agent pipeline was running stale code — ✅ DONE 2026-08-07

The box was running `tick.sh` from 2026-08-05, **165 lines behind `main`**. Verified first that no
on-box hotfixes would be lost: all 13 differing lines were simply older versions.

Installed from `origin/main` by copying `tick.sh` + `RUNBOOK.md` directly rather than running
`install.sh` — **the installer does `touch PAUSED` and rewrites the crontab to hourly**, which would
have stopped the pipeline and changed its cadence from the `*/5` it actually runs at. Backups at
`/home/lem/agent-pipeline/*.bak-20260807`. Verified with a `DRY_RUN=1` tick.

**⚠️ Still owed:** the two auto-fix-lane fixes (`AGENT_CI_LABEL_ACTORS`, the branch-scoped attempt
counter) are on PR #1100, not yet on `main`. **Re-copy after #1100 merges** or both lanes stay dead.

### 2.2 The weekly SDUI drift check — ✅ INSTALLED 2026-08-07

```sh
40 6 * * 1 /home/lem/linkedin_engagement_manager/scripts/weekly_sdui_drift_check.sh
```
It had never been scheduled, which is why #964, #1009, #1012, #970, #1007 and #1006 were each found
dead by hand instead of by the sweep.

**⚠️ Still owed by you:** set `SDUI_PROBE_PROFILE_URL` in `/opt/lem/.env` to a **2nd/3rd-degree**
profile, or the degree badge stays ungrounded (`docs/sdui-probe-coverage.md:99-101`). One line.

### 2.3 The perf/margin snapshot cron — ✅ REPOINTED 2026-08-07

The 23:30 entry now runs `/home/lem/linkedin_engagement_manager/scripts/perf_snapshot.sh`. The old
on-box `snapshot.sh` had **zero** `margin` references against the successor's 12, so the daily
cost/unit-economics block (#491) had never been captured. Crontab backup at
`/tmp/lem-gh/crontab.bak-20260807`.

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

### 2.6 `SUPPRESSION_COMMENT_DAYS` is pinned to 7 in prod · issue #1136

#1136 narrows the comment-demotion half of the suppression tripwire from a rolling week to 3 days,
because the beat runs daily and a week-wide window averages a demotion spike against up to six
healthy days. `/opt/lem/.env` carries the old `.env.example` value `SUPPRESSION_COMMENT_DAYS=7`, and
an explicit env var wins over the new default — so **the merge is a no-op in production until this
line changes**. The readable-comment floor is derived
(`ceil(COMMENT_QUALITY_MIN_SAMPLE * days / 7)`, 5 at 3 days), so there is no second knob to touch.

**Your action (~1 min, at the next deploy):** in `/opt/lem/.env` set `SUPPRESSION_COMMENT_DAYS=3`
or delete the line, then restart the stack. Confirmed by the owner 2026-08-17 (PR #1617, option 2A).

### 2.7 The shipped-video corpus sampler still needs its production run · issue #1654

#1363 shipped every code half of the native-video audit — the sampler (#1506), the store-time
measure receipt (#1517) and the retained `open`/`mid`/`close` keyframes (#1595) — and closed on your
`1A 2A`. The receipt and the keyframes both survive `purge_post_assets`, but they are written at
STORE time, so they only exist for video rendered **after #1595 deployed** — which means
the scorecard in `docs/content-quality-audits/video.md` §8 is still the empty 2026-08-14 run. The
sampler reads production MySQL and the `lem_assets` volume, which no agent can reach.

**Your action (~5 min, once ~6 native-video posts have shipped since the #1595 deploy):** from the
prod-image sidecar you used on 2026-08-14,

```sh
poetry run python scripts/sample_shipped_videos.py --limit 10 --json --no-frames  # readiness
poetry run python scripts/sample_shipped_videos.py --limit 10                     # scorecard + frames
```

Paste both into #1654. Add `agent:ready` to it **only when the first command reports
`"sufficient_corpus": true`** — the field is printed either way, and a `false` means fewer than
`MIN_CORPUS = 6` posts graded, which is the one thing decision `2A` says must not be written up as a
scorecard. The label is off at filing for exactly that reason; with it, an agent writes §8 from your
output.

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
| `POSTHOG_PERSONAL_API_KEY` | — | **REVOKED 2026-08-31** (#1453). Replaced by five purpose-scoped keys; see below |
| `POSTHOG_CLI_TOKEN`, `UI_POSTHOG_KEY` | repo secrets | Present |
| `CF_API_TOKEN` / `CF_ZONE_ID` | repo secrets | Present |
| `CODECOV_TOKEN`, `GITGUARDIAN_API_KEY`, `LITELLM_MASTER_KEY`, `OPENROUTER_API_KEY`, `PEXELS_API_KEY`, `VPS_*` | repo secrets | Present |
| `REPLICATE_API_TOKEN`, `REPLICATE_USERNAME`, `CAPSOLVER_API_KEY` | `/opt/lem/.env` only | Missing as **repo secrets**. **Moot since #1215:** the only tests that wanted them lived in the e2e lane, which was deleted — nothing in CI reads them now. |
| `AWS_*` (4 secrets, 2025-02) | repo secrets | Present but **dormant** — the CDK deploy tree was deleted in #973. Still read at runtime only if `AWS_REGION` is set (SQS broker discovery in `app/celeryconfig.py`); otherwise safe to delete |
| `CQCLEMAZUREAPP_*` (5 secrets, 2025-01) | repo secrets | **OBSOLETE — zero references anywhere in `.github/`, `scripts/`, `src/`. Safe to delete.** |
| `CWS_*` (Chrome Web Store) | — | **Not needed.** PR #336 was closed today; only required if you resume store publishing |

**The PostHog scope trap.** One variable name carries **eight different scope requirements** across
the docs (query, insight/dashboard/alert/action/subscription, annotation, feature-flag + experiment,
survey, endpoint/insight_variable…). A key provisioned for one script silently blocks another —
symptoms are `blocked_goal`, `blocked_endpoint`, or empty `subscribed_users`. If a PostHog script
misbehaves, read the HTTP status before debugging the script — and before reaching for scopes:
**`401` is the key, `403` is the scope, `404` is the path.** Conflating the last two cost #1453
eleven days (see the benchmark note in `docs/kpi-dashboards.md`).

**Split COMPLETE (#1453), 2026-08-31.** Each purpose reads its own key
(`POSTHOG_ANNOTATION_API_KEY` / `POSTHOG_RUNTIME_API_KEY` / `POSTHOG_QUERY_API_KEY` /
`POSTHOG_BENCHMARK_API_KEY` / `POSTHOG_OPERATOR_API_KEY`) through
`src/cqc_lem/utilities/posthog_keys.py`, and the shared account-scoped key has been **revoked**.
Scopes per purpose: `docs/kpi-dashboards.md` § Purpose-scoped personal keys.

| Key | Where it lives | Notes |
|---|---|---|
| `POSTHOG_ANNOTATION_API_KEY` | GitHub Actions secret | **Being a secret is not enough** — the `deploy` job has to export it into the step's env, and for 11 days it did not. Nothing noticed, because the step is `continue-on-error` and the script exits 0 on a missing key by design. `tests/unit/test_posthog_annotate.py` now fails the build if that wiring is dropped |
| `POSTHOG_RUNTIME_API_KEY` | `/opt/lem/.env` | App containers must be recreated for a change to take effect |
| `POSTHOG_QUERY_API_KEY` | `/opt/lem/.env` | Host crons source it fresh each run; no restart needed |
| `POSTHOG_BENCHMARK_API_KEY` | `/opt/lem/.env` | Same. `evaluation:read` + `evaluation:write` + `query:read` |
| `POSTHOG_OPERATOR_API_KEY` | **nowhere** | Exported into a shell for a hand-run provisioning script, by design |

**Still yours, and only this:** create the `operator` key with the scopes in
`docs/kpi-dashboards.md`, keep it in a password manager, and export it per invocation:

```bash
POSTHOG_OPERATOR_API_KEY=phx_... poetry run python scripts/posthog_provision.py --dry-run
```

A dry run only READS, so it does not prove the write scopes. One real `posthog_provision.py` run
confirms those; it is additive.

**Verifying any of it:** `python scripts/posthog_key_check.py` (add `--purpose <name>` for one).
Read-only, PASS/FAIL per surface, naming the env var that actually supplied each key. Every consumer
fails silently, so this command is the evidence; a green deploy is not. On the box, expect
`annotation` and `operator` to FAIL — those keys correctly live in CI and in your shell, not in
`/opt/lem/.env`. A `via POSTHOG_PERSONAL_API_KEY` line now means an unpopulated consumer holding a
revoked credential.

---

## 4. Feature flags that are OFF, and what each needs first

A ✅ row is one that is now **ON** — kept in the table only so nobody re-does the work. Read the row,
not the heading.

| Flag | Blocked on |
|---|---|
| `APPRECIATION_SOURCES_ENABLED` | #1002 — grounded GREEN today; now just needs the flip after #1019 deploys |
| `STALE_INVITE_WITHDRAWAL_ENABLED` | ✅ **DONE 2026-08-15 — nothing left to do; row kept only so nobody re-does it.** #1006 is closed. The `/opt/lem/.env` override was cleared and the worker recreated, so the code default (`true`) now applies in prod. Two nights of `stale_invite_run` confirm it: 2026-08-15 `withdrew` (rows_seen 39, stale_seen 3, withdrawn 3, unverified 0), 2026-08-16 `none_stale` (rows_seen 36 — exactly 39 − 3). To silence the beat again, set the variable to `false` |
| `TUTORIAL_VIDEOS_ENABLED` | `ELEVENLABS_API_KEY` (unset), `TUTORIAL_DEMO_SESSION_TOKEN` (unset), and #1094 above |
| `COST_ROUTING_ENABLED` + `COST_AWARE_ROUTING_ENABLED` | Set **both together**, recreate app + litellm, preview with `python -m cqc_lem.utilities.cost_routing --json` |
| `POSTHOG_SURVEYS_ENABLED` | Precondition now met (`UI_POSTHOG_KEY` exists). Still needs `posthog_surveys.py --apply --launch` |
| `REQUIRE_STRONG_FACTOR_AFTER` | Unset — mandatory passkey/TOTP enrolment is built and dormant. `docs/strong-authentication.md:373-376`: *"Nobody has scheduled the deadline yet."* Operator decision |
| `EARLY_ADOPTER_TRIAL_ENABLED` | Explicitly `False`; `EARLY_ADOPTER_COUPON_ID` empty |
| `AVATAR_LIKENESS_VIDEO_HOLD_ENABLED` | #1430 — stays `false` until a false-negative rate exists, and that rate is blocked on **you**: the active avatars declare no `gender_presentation` / `age_band`, so every one of the 152 `avatar_likeness_probe` events is `checked=false`. Declare both on the active avatars in the Avatars SPA (the controls render on every succeeded avatar, active ones included), then the ≈2026-08-28 telemetry re-read has something to grade. The same declaration switches on #744's subject clause, inert today for the same reason |
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
| Degree badge re-check after deploy | #1031 | Phase 3 of #1021 |
| Supervised first newsletter publish | — | Publish dialog has never been live-validated |
| Populate `include_authors` roster | — | `docs/engagement-growth-analysis-2026-07.md:30`: *"has never been populated"* |
| Story-bank entries | — | Empty bank means placeholder-only drafts, approval-gated |
| Gmail auto-forward filter for reply detection | — | `docs/REPLY_NOTIFICATIONS.md:29-34` |
| Set `users.linkedin_display_name` | — | Required setting, never scraped |
| LinkedIn profile re-index (skills, headline, About) | — | `docs/linkedin-reindex-playbook.md` — entirely manual |

---

## 8. PostHog — measured 2026-08-07 against the live project

Queried project `475262` with the prod key. Most of the surface **is** provisioned; two things are
not, and one of them matters.

| Object | Count | Verdict |
|---|---|---|
| Dashboards | 13 | ✅ `posthog_provision.py --apply` has run |
| Insights | 73 | ✅ |
| Alerts | 4 | ✅ |
| Subscriptions | 1 | ✅ |
| Annotations | 51 | ✅ release annotations are landing |
| Feature flags | 5 | ✅ provisioned |
| **Surveys** | **0** | ❌ `posthog_surveys.py --apply --launch` has never run |
| **Experiments** | **0** | ❌ **see below** |

**Session replay is correct:** `session_recording_opt_in: true`, `minimum_duration: 5000 ms`, and
project sampling is `None` — which is right, because `CLAUDE.md` says never to set project sampling
(it multiplies with the SDK-side slice).

**The experiments finding is the important one.** `CLAUDE.md` lists three registered experiments —
`cost-routing-arm`, `comment-contract-prompt`, `post-media-variant` — and none of the five live flags
is any of them. `utilities/experiments.py` resolves an unresolvable experiment to **CONTROL**, so all
three have been silently serving control since they shipped. **No experiment has ever actually run.**
Nothing is broken and nothing is at risk; the arms just never split.

**Your action (optional, when you want the data):** `python scripts/posthog_experiments.py --apply`,
and `python scripts/posthog_surveys.py --apply --launch` for NPS/CSAT. The `UI_POSTHOG_KEY`
precondition for surveys is already met.

**Flag rollouts** are all `0%` except `feed-fallback-when-empty-default` at 100% — consistent with
the env-var defaults, since `utilities/flags.py` fails open to the env var.

## 9. Confirmed by the owner 2026-08-07

- **Uptime monitor is armed** on `/health/deep`. Closes the §2 gap in `docs/stack-watchdog.md`.
- **Stripe is in TEST mode.** Worth remembering before the early-adopter program opens —
  `EARLY_ADOPTER_TRIAL_ENABLED` is `False` and `EARLY_ADOPTER_COUPON_ID` is empty, so nothing can
  transact by accident today.
- **The supervised avatar render is done** — attributes declared first, previews re-rolled, likeness
  approved. It closed #744 and is off the §7 list; the ordering that made it a real test is written
  down in `docs/AVATAR_FIDELITY_AND_VIDEO_LANGUAGE.md` §4.

## 10. Still could not verify

1. **`POSTHOG_PERSONAL_API_KEY` scopes** — the key works for dashboards, flags, alerts and
   annotations, so those scopes are present. Survey/experiment write scopes are untested because
   there is nothing to read. See the trap in §3.
2. **DB-level state** — admin flag, `users.linkedin_display_name`, story-bank rows, orphaned cookies.
   `docker exec` into the app containers was blocked.
3. **SendGrid/DNS** — SPF, DKIM and DMARC policy level. PIN/parse mail is evidently working.
