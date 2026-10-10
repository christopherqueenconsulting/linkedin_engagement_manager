# Demo recording seed

Product demos are recorded against a synthetic account on a local database, never a real one.
`scripts/seed_demo_account.py` writes that account and one scene's state (issue #2371).

**Never run this on the production VPS.** Production MySQL is published on `127.0.0.1:3306` there
(for SSH-tunnel database GUIs), and dev checkouts live on the same host. From a shell on the VPS, or
through an SSH tunnel to it, `127.0.0.1` is the production database. Run it on your own machine
against your own local compose stack.

## 1. Start the local stack

From the checkout root on your machine:

```
docker compose up -d mysql flyway redis web_app
```

`flyway` applies the migrations; the gate columns the scenes write (`authenticity_score`,
`gate_reason`, `rejection_reason`) come from them, so a database it has not run on fails the seed.

**Do not start `celery_beat` or any `celery_worker*` service while recording.** The beat schedules
content generation and engagement for every account, including the demo one. That would rewrite the
seeded drafts, fill the `planning` slots, and spend on LLM calls. `selenium-chrome`, `flower` and
`litellm` are not needed either.

`web_app` serves the SPA from `src/cqc_lem/ui/dist` (the dev compose bind-mounts `./src`), so build
it once:

```
cd src/cqc_lem/ui && npm ci && npm run build
```

## 2. Run the seed

No dev service mounts `./scripts`, so run the script on the host. Export the connection settings in that shell first: `connection.py` reads
`MYSQL_*` at import, before it calls `load_dotenv()`, so values that are only in `.env` are not
used. The dev `mysql` service publishes `${MYSQL_PORT}` on the host, so from the host the target is
`127.0.0.1` on that port:

```
export MYSQL_HOST=127.0.0.1
export MYSQL_PORT=3306            # the MYSQL_PORT value in your .env
export MYSQL_USER=...             # the MYSQL_USER value in your .env
export MYSQL_PASSWORD=...         # the MYSQL_PASSWORD value in your .env
export MYSQL_DATABASE=linkedin_manager
PYTHONPATH=src poetry run python scripts/seed_demo_account.py --scene demo1 --anchor-date 2026-10-12
```

`--anchor-date` is the recording day (default: today in America/New_York). The plan starts the day
after it. **Pin it to the same date for every take.** The drafts' dates are computed from it, so takes
recorded on different days without it show different dates.

| Exit | Meaning | What to do |
|---|---|---|
| `0` | Seeded (or torn down) | — |
| `1` | A write failed after writing had started | The scene may be half-written. Run the same command again: every run resets the demo account first |
| `2` | Refused by a check (see **Checks**); nothing written | Read the reason. Do not work around it |
| `3` | Could not connect to MySQL; nothing written | Fix the `MYSQL_*` exports or start `mysql`. Re-running without a change gives the same answer |

## 3. Sign in as Dana

Sign in without any mail being sent by using the app's existing no-mail-provider path. When neither
SendGrid nor SMTP is configured, `POST /api/auth/email/init` signs the address in directly, with no PIN
step (`send_pin_email` reports a bypass). In the `.env` that `web_app` reads, set these to empty:

```
SENDGRID_API_KEY=
SMTP_USER=
SMTP_PASSWORD=
REQUIRE_STRONG_FACTOR_AFTER=
```

In `.env.example`, `SENDGRID_API_KEY`, `SMTP_USER` and `SMTP_PASSWORD` hold non-empty placeholders, and
a non-empty value makes the app try to send. An
empty `REQUIRE_STRONG_FACTOR_AFTER` keeps the session from being held at the passkey enrolment
screen. Restart `web_app` after editing (`docker compose up -d web_app`). Then open
`http://localhost:8000` (your `API_PORT`), sign in with `dana.reyes@example.com`, and go to
`/content` for Content Studio. With these settings the local stack signs in any address without
proof. That is acceptable only on a local stack.

## The account

**Dana Reyes, Reyes Advisory** (`dana.reyes@example.com`, a reserved example domain no real signup can
own). The voice is fictional. Dana posts three times a week (Mon, Wed, Fri, 09:00 America/New_York)
on a 30-day plan. Plan slots with no written post are left as `planning` skeletons, as the content
plan leaves them. The trial window restarts on every run.

Every run first deletes this account's posts and engagement preferences, then writes the scene. Each
DELETE also matches on the demo email, so no other account's rows are touched. The account row
stays, so a signed-in browser stays signed in. Running the same scene on the same anchor day twice
gives the same rows (only auto-increment ids differ).

| Scene | State |
|---|---|
| `demo1` | Three PENDING drafts for the next three posting days, opening "I priced my first fixed-scope project by the hour…", "Three questions I ask in every scoping call" and "The handoff document my clients actually read" |
| `demo2` | One PENDING draft held by the authenticity gate (score 58, minimum 70). One held by the similarity gate (62% overlap, maximum 40%), plus the POSTED post it overlaps ("What I'd tell my first fixed-scope client"). One REJECTED draft with the reason "Too salesy, no example" |
| `demo3` | Voice fields (`tone`, `comment_style`, `focus_topics`, `include_topics`) empty, and one PENDING draft already written in the voice the presenter enters (`DEMO3_NEW_VOICE` in the script) |

The 58 / 70 and 62% / 40% values are made up for the recording. They are not platform defaults. The
minimum and maximum are saved as the account's own gate thresholds, so the Account page matches the
held drafts. The scores, findings and rejection reason are written through the same repository
functions the app uses (`update_db_post_authenticity_score`, `update_db_post_gate_reason`,
`soft_delete_posts`), so Content Studio shows them the same way it shows real ones.

## Clean up

```
PYTHONPATH=src poetry run python scripts/seed_demo_account.py --teardown
```

This removes the demo account and every row it owns. The `users` DELETE matches on both the id and
the demo email, and the per-user tables cascade from it: posts (with their logs, approvals and
shipped variants), engagement preferences, sessions, profiles, cookies and onboarding state.
`cost_ledger` rows are kept, with the user set to NULL. Two auth tables have no foreign key to
`users`, so the login audit rows and auth challenges are deleted by the account's id. It runs behind
the same checks as a seed.

**This is also the rollback** if a seed ever lands in the wrong database. Run this same command with
the same exports. It touches only `dana.reyes@example.com`. If the fingerprint refuses it there, the
seed could not have run there either.

## Checks

These run in order, before anything is written:

1. **Production markers.** `ENVIRONMENT`, `APP_ENV` or `ENV` set to `prod` / `production` / `live`,
   or `ENCRYPTION_REQUIRED` on, refuses (exit 2). This is defence in depth only. Production is not
   required to set any of these, so their absence proves nothing.
2. **Host name.** The host must be `localhost` / `127.0.0.1` / `::1`, a service name in this
   repository's `docker-compose*.yml`, or a literal compose `hostname` / `container_name`. A compose
   host given as `${MYSQL_HOST}` cannot be checked without trusting the value being checked, so for
   that case only a single DNS label (no dot, not an IP address) passes. A host taken from an AWS
   secret is refused. This rules out a remote server by name, but **not** the production database:
   on the VPS, or through a tunnel to it, production answers on `127.0.0.1:3306`, and the
   production stack's own host is the same compose name.
3. **Connection.** Up to `MYSQL_CONNECT_RETRY_ATTEMPTS` connect attempts (default 3, backing off
   2 s then 4 s, only while the server is unreachable); a failure exits 3 with the connector's error.
4. **Database fingerprint: the real control.** A read-only count (`get_demo_db_fingerprint`) of
   what belongs to anyone other than a demo account. Any POSTED post owned by a non-demo or missing
   account, or more than 3 non-demo accounts, refuses (exit 2). A local database holds the demo
   account and perhaps a few test sign-ups; production holds real people and published posts. A
   fingerprint that cannot be read also refuses: an unreadable database is never treated as a
   local one.

`scripts/` is also not copied into the production image.

The script makes no LinkedIn call and opens no browser. Its imports are limited to the database
facade and the pure gate-finding builders. It turns off PostHog flag loading and telemetry for its
own process, and it creates the account without a Stripe customer.
`tests/unit/scripts/test_seed_demo_account.py` runs every scene and the teardown with all sockets
blocked, and checks in a fresh interpreter that no Selenium or LinkedIn client module is imported.
