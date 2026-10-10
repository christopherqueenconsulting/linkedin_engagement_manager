# Off-host copy of the nightly DB dump

`scripts/backup.sh` runs at 03:00 UTC from the `deploy` crontab (`docs/host-crons.md`). It writes
`/opt/lem/backups/db-<stamp>.sql.gz` and keeps 7 days on the **same disk** as the database. If
that disk is lost, the database and every backup of it go with it (`docs/OWNER_ACTION_TRACKER.md`
§1.2).

This page is the design, the one-time install, and the two restore procedures for a second copy
kept off the host. **Owner:** the repository owner installs it, receives its watchdog alert (the
`WATCHDOG_ALERT_EMAIL` chain in `scripts/stack_watchdog.sh`), and runs the monthly drill.

## What the code does

| Piece | Behaviour |
|---|---|
| `backup.sh` `offsite_copy` | Runs after the local dump passes its checks. It sends **only tonight's dump**: `timeout 3600 rclone copyto --immutable <dump> <BACKUP_REMOTE><name>`. It never deletes or overwrites anything off-host. On success it writes `backups/.offsite-last-ok` (the dump's name) and logs `off-host copy OK: <name>`. Log lines name the remote only, never the full destination. |
| `BACKUP_REMOTE` unset | Logs `off-host copy: not configured (BACKUP_REMOTE unset); this dump exists only on this disk` on every run and exits 0. This is the state until the install below. |
| `BACKUP_REMOTE` not a configured rclone **`crypt`** remote | Refuses before any upload, logs `ERROR: off-host copy: … refusing …`, and exits 1. This covers a plain bucket remote, an unknown name, an inline `:s3,…` connection string, and a value starting with `-`. The dump holds member personal data, so it only leaves the host encrypted. |
| `rclone` missing, or the upload fails or times out | Logs `ERROR: off-host copy …` and **exits 1**. The local dump is already written and is kept. |
| `stack_watchdog.sh` `check_backup_freshness` | Only when `BACKUP_REMOTE` is set in `/opt/lem/.env`. A missing `.offsite-last-ok` reports `backup:offsite:missing`. One older than `WATCHDOG_BACKUP_AGE_HOURS` (48) reports `backup:offsite:stale:<N>h`. Both alert the same way as a stale local dump. |

The old hook ran `rclone copy` of the whole backups directory, and only when both `rclone` and
`BACKUP_REMOTE` were present. Otherwise it skipped silently, so the log never showed that no
off-host copy existed.

## Design choices (owner)

1. **Destination.** Any rclone backend works. The recommendation is **Cloudflare R2**: the
   Cloudflare account already exists for the tunnel, so there is no new vendor or billing
   relationship. Backblaze B2 or any S3-compatible bucket works the same for this design.
   Size for planning: the dumps from 2026-10-02 to 2026-10-09 were 1,426,067–1,489,006 bytes each,
   so 35 days of copies is about 52 MB. Check the provider's current pricing when choosing, and
   accept or confirm its data-processing terms. The provider is a processor of encrypted member
   data.
2. **Encryption before it leaves the box, enforced.** The dump is plain SQL. Secret columns are
   AES-GCM ciphertext (`docs/secrets-at-rest.md`), but the rest holds member data: names, emails
   and post text. `backup.sh` uploads only through an rclone **`crypt`** remote, so the provider
   stores only ciphertext and encrypted file names.
3. **Threat model, stated plainly.** Deploy's `rclone.conf` *obscures* the bucket secret and both
   crypt passwords; it does not encrypt them. Crypt therefore protects against a leak at the
   provider or of the bucket. It does **not** protect against compromise of this host, which
   already holds the plaintext database.
4. **What must exist off the box for a restore.** Keep these in the password manager: both crypt
   passwords, the provider account login, the bucket name and endpoint, and the rest of
   `/opt/lem/.env`, which a replacement host needs to bring up `mysql_db` at all. Without the crypt
   passwords, every off-host copy is unreadable. **`LEM_SECRET_KEY`** is also needed: the encrypted
   columns decrypt only with it. #1095
   ("move the LEM_SECRET_KEY backup off the VPS") was closed as completed on 2026-08-10. Before
   relying on an off-host restore, confirm the off-box key copy is where you expect. A dump restored without the key restores
   everything except stored LinkedIn sessions and tokens.
5. **Retention lives in the bucket, not on the box.** Set a lifecycle rule that expires objects
   after **35 days**. The local 7 days stay as they are. If the provider offers object or bucket
   lock, set a retention of at least 30 days: the box's credential can write objects, so without
   a lock a stolen credential could delete the history. The script itself never deletes
   (`--immutable`, single-file `copyto`).
6. **Credential scope.** One token, scoped to this one bucket, object read and write only. No
   account-level keys. Give the bucket a name that is not guessable, e.g. `lem-db-` plus a
   random suffix. This page writes it as `<bucket>`.

## Personal data, retention and deletion

- Every off-host copy holds every member's personal data, encrypted by `crypt`. Copies are kept
  for up to **35 days** (the lifecycle rule). With a lock set, a copy cannot be removed early, so
  it can outlive a deletion request by up to that window.
- Deleting an account does not reach copies that already exist. Those copies expire under the
  lifecycle rule within 35 days.
- **A restore brings back every account deleted after the dump's timestamp.** Before the
  restored stack serves traffic, re-apply every account deletion completed after the `<stamp>` in
  the dump's file name. The list to re-apply from is **NOT YET AVAILABLE**: the code has no
  account-deletion path, so deletions are done by hand. Until there is one, keep a dated record of
  each completed deletion outside the database (owner action, `docs/OWNER_ACTION_TRACKER.md`
  §1.2). Both restore procedures below end with this step.

## Install (owner, once)

The copy goes live only after this change ships in a release, because the cron runs the
deployed-tag checkout at `/opt/lem`.

0. **Check the release and the cron line.**
   - `grep -c offsite_copy /opt/lem/scripts/backup.sh` prints `2` or more.
   - After the next 03:00 run, `tail -3 /opt/lem/logs/backup.log` shows `off-host copy: not
     configured`.
   - As root, `crontab -u deploy -l | grep backup.sh` must end in `>> logs/backup.log 2>&1`. The
     ERROR lines go to stderr and reach the log only with `2>&1`.
1. **Provider console.**
   - Create the private `<bucket>`.
   - Add the 35-day lifecycle rule, and the lock if one is offered.
   - Create the bucket-scoped token. Enter its endpoint, key ID and secret straight into rclone in
     step 3, and nowhere else.
   - Record the bucket name, endpoint and account login in the password manager.
2. **As root**, install rclone and check the flag the script uses:
   ```sh
   apt-get install -y rclone
   rclone version
   rclone help flags | grep -c -- '--immutable'   # must print 1 or more
   ```
3. **As the deploy user**, run `rclone config` and create two remotes.
   - `lem-store`: type `s3`, provider per step 1, the token from step 1, the provider's endpoint.
     For R2, or any token that can reach objects but not buckets, also set
     `no_check_bucket = true`. rclone's S3 docs (R2 section) say an "Object Read & Write" R2 token
     may need it for uploads to work.
   - `lem-offsite`: type `crypt`, remote `lem-store:<bucket>`, standard filename encryption. Let
     rclone generate the password and the salt (`password2`), then **copy both into the password
     manager now**.

   Then check the setup, using bucket-level commands, which a bucket-scoped token is allowed to
   run:
   ```sh
   chmod 600 ~/.config/rclone/rclone.conf
   rclone listremotes --long          # lem-offsite: crypt
   rclone lsf lem-store:<bucket>      # succeeds (empty on first install)
   ```
4. **As the deploy user, prove one upload *before* enabling the alert.** `backup.sh` reads
   `BACKUP_REMOTE` from the environment first:
   ```sh
   cd /opt/lem && BACKUP_REMOTE=lem-offsite: ./scripts/backup.sh >> logs/backup.log 2>&1; echo "rc=$?"
   tail -4 logs/backup.log            # expect: off-host copy OK: db-<stamp>.sql.gz
   cat backups/.offsite-last-ok       # the same name
   rclone lsl lem-offsite:            # the same name, size within a few bytes of the local file
   ```
   If `rc` is not 0, read the `ERROR` line, fix it, and repeat. Nothing alerts yet.
5. **As the deploy user, enable it.** Only after step 4 printed `off-host copy OK`:
   ```sh
   cd /opt/lem
   cp -a .env .env.bak-$(date -u +%Y%m%d)
   echo 'BACKUP_REMOTE=lem-offsite:' >> .env
   ```
   Put it in `.env`, not in the crontab environment, because the watchdog reads `.env` only. The
   marker from step 4 is fresh, so the watchdog stays quiet.
6. **Run the restore drill below once now.** After that, run it monthly.

## Restore drill (monthly; production is never touched)

A copy is a backup only once a restore from it has worked. As the deploy user:

```sh
umask 077
d=$(mktemp -d)                                   # mode 700; only the deploy user can read it
trap 'docker rm -fv restore-drill >/dev/null 2>&1; rm -rf "$d"' EXIT
name=$(rclone lsf lem-offsite: | grep '^db-' | sort | tail -1)
rclone copyto "lem-offsite:$name" "$d/$name"
gzip -t "$d/$name" && echo gzip-ok
docker run -d --name restore-drill -e MYSQL_ROOT_PASSWORD=drill -e MYSQL_DATABASE=drill mysql:8.0
until docker exec restore-drill mysqladmin ping -uroot -pdrill --silent 2>/dev/null \
      && docker logs restore-drill 2>&1 | grep -q 'port: 3306'; do sleep 2; done
gunzip -c "$d/$name" | docker exec -i -e MYSQL_PWD=drill restore-drill mysql -uroot drill
docker exec -e MYSQL_PWD=drill restore-drill mysql -uroot -N -e \
  "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='drill'"
docker exec mysql_db sh -c 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql -uroot -N -e \
  "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=\"linkedin_manager\""'
```

- The two counts must match.
- Pass or fail, the `EXIT` trap removes the throwaway container, **its data volume** and the
  private directory. Exit the shell to fire it. The `mysql:8.0` image keeps its data in an
  anonymous volume, which holds a decrypted copy of member data. `docker rm` leaves that volume
  behind unless it is given `-v`.
- Once, before the first drill, check for volumes left by any earlier manual restore test:
  `docker volume ls -qf dangling=true`. Inspect each one (`docker volume inspect <name>`) before
  removing it.
- The production count is read live, while the drill restores last night's dump. If a release
  that added tables shipped in between, the counts differ for that reason alone. Check
  `/api/app-info` and the release notes before treating a mismatch as a failed restore.
- The drill uses `mysql:8.0`, the image `mysql_db` runs (`docker-compose.yml`). If that ever
  changes, change this line with it.
- The production count is a read-only query. The password stays inside the container's own
  environment.

## Restore after host or disk loss

This works from the password manager alone: nothing below reads `/opt/lem/backups`, which is gone.

1. On the replacement host, rebuild the stack per `docs/DEPLOYMENT.md` up to a running, empty
   `mysql_db`. Do **not** start the app services yet.
2. As root, install rclone (Install step 2).
3. In the provider console, issue a **new** bucket-scoped token. The old one lived on the lost
   host, so revoke it.
4. As the deploy user, recreate `lem-store` with the new token and `lem-offsite` with the **same**
   crypt password and salt (`password2`) from the password manager (Install step 3).
5. Pick the newest dump:
   ```sh
   rclone lsl lem-offsite:
   name=$(rclone lsf lem-offsite: | grep '^db-' | sort | tail -1)
   ```
6. Restore it into production:
   ```sh
   umask 077; d=$(mktemp -d); trap 'rm -rf "$d"' EXIT
   rclone copyto "lem-offsite:$name" "$d/$name" && gzip -t "$d/$name"
   gunzip -c "$d/$name" | docker exec -i mysql_db sh -c \
     'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql -uroot linkedin_manager'
   ```
   Do this before the first `scripts/deploy.sh` run on the new host. The dump carries Flyway's
   history table, so the deploy then applies only migrations newer than the dump.
7. Put `LEM_SECRET_KEY` (and `LEM_SECRET_KEY_VERSION`) back in `/opt/lem/.env` from its off-box
   copy (#1095).
8. Re-apply account deletions completed after the dump's `<stamp>` (see Personal data, retention
   and deletion). Only then start the app services.
9. Re-run the Install from step 4 on the new host, so the off-host copy resumes.

## Rollback

- Restore the `.env` backup from Install step 5, or delete the `BACKUP_REMOTE=` line. `backup.sh`
  goes back to logging `not configured`, and the watchdog stops checking the marker. This is also
  the stop-gap while a provider outage would otherwise alert on every watchdog run. The cost is
  losing the alert.
- Revoke the token in the provider console. Copies already in the bucket expire under the
  lifecycle rule.
- The code change needs no rollback: with `BACKUP_REMOTE` unset it does nothing but log.

## Failure modes and who sees them

| Failure | Signal | Owner action |
|---|---|---|
| Upload fails (network, provider, revoked token) or exceeds `BACKUP_REMOTE_TIMEOUT` (3600 s) | `ERROR: off-host copy failed` in `logs/backup.log`, cron exit 1. After 48 h the watchdog alerts `backup:offsite:stale:<N>h`. | As deploy, run `rclone lsf lem-store:<bucket>`. If the token was revoked, issue a new one. |
| `BACKUP_REMOTE` names a plain bucket remote, an unknown name, an inline connection string or a flag-shaped value | `ERROR: off-host copy: … refusing …`. Nothing is uploaded. Then the watchdog alert. | Point `BACKUP_REMOTE` at the `crypt` remote (`lem-offsite:`) |
| `rclone` removed or broken by an upgrade | `ERROR: … rclone is not installed`, then the watchdog alert | Re-run Install step 2 |
| A file with tonight's name already exists with different content | `rclone` refuses under `--immutable`; same signals as a failed upload | Investigate. Something other than this script wrote to the bucket. |
| Bucket token leaked | None from the box | Revoke and re-issue it. The copies stay ciphertext. |
| Crypt password or salt leaked | None from the box | Treat every existing off-host copy (up to 35 days) as exposed. Create a new crypt remote with new passwords over a new path or bucket, point `BACKUP_REMOTE` at it, and let the old copies expire. |
| Crypt passwords lost | Nothing, until a restore fails | Prevented by the password-manager copy (Install step 3), and caught by the monthly drill |
