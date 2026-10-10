# Demo recording seed

Product demos are recorded against a synthetic account on a local database, never a real one.
`scripts/seed_demo_account.py` writes that account and one scene's state (issue #2371).

## Run it

Start the local compose stack (MySQL plus the API and SPA), then from the checkout root:

```
PYTHONPATH=src poetry run python scripts/seed_demo_account.py --scene demo1
PYTHONPATH=src poetry run python scripts/seed_demo_account.py --scene demo2 --anchor-date 2026-10-12
```

The connection settings (`MYSQL_HOST`, `MYSQL_USER`, `MYSQL_PASSWORD`, `MYSQL_DATABASE`,
`MYSQL_PORT`) are read the same way the app reads them. The guard checks the host the connector will
actually use.
`--anchor-date` is the recording day (default: today in America/New_York). The plan starts the day
after it.

Exit codes: `0` seeded, `2` refused by a guard (nothing written), `1` a write failed. A failed write
can leave the scene half-written, so run the same scene again.

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

## Guards

The script refuses (exit 2) before any write when:

- the database host is not `localhost` / `127.0.0.1` / `::1`, a service name in this repository's
  `docker-compose*.yml`, or a literal compose `hostname` / `container_name`. A compose host given as
  `${MYSQL_HOST}` cannot be checked without trusting the value under test, so for that case only a
  single DNS label (no dot, not an IP address) is accepted. `mysql_db` passes; `db.example.com` and
  `10.0.0.5` do not;
- the host comes from an AWS secret (`AWS_MYSQL_SECRET_NAME`), because it cannot be checked before
  connecting;
- `ENVIRONMENT`, `APP_ENV` or `ENV` is `prod` / `production` / `live`, or `ENCRYPTION_REQUIRED` is on
  (only the production stack sets it).

The production stack uses the same compose service name, so the host check alone cannot tell a local
stack from production. The production markers are the second check. `scripts/` is also not copied
into the production image.

The script makes no LinkedIn call and opens no browser. Its imports are limited to the database
facade and the pure gate-finding builders. It turns off PostHog flag loading and telemetry for its
own process, and it creates the account without a Stripe customer.
`tests/unit/scripts/test_seed_demo_account.py` runs every scene with all sockets blocked and checks
in a fresh interpreter that no Selenium or LinkedIn client module is imported.
