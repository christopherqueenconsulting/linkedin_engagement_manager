# Off-host copy of the nightly DB dump

`scripts/backup.sh` runs at 03:00 UTC from the `deploy` crontab (`docs/host-crons.md`). It writes
`/opt/lem/backups/db-<stamp>.sql.gz` and keeps 7 days on the **same disk** as the database. One
disk loss today loses the database and every backup of it (`docs/OWNER_ACTION_TRACKER.md` §1.2).

This page is the design and the one-time install for a second copy off the host.

## What the code does

| Piece | Behaviour |
|---|---|
| `backup.sh` `offsite_copy` | After the local dump passes its checks, sends **only tonight's dump** with `rclone copyto --immutable <dump> <BACKUP_REMOTE><name>`. It never deletes or overwrites anything off-host. On success it writes `backups/.offsite-last-ok` (the dump's name) and logs `off-host copy OK: <name>`. |
| `BACKUP_REMOTE` unset | Logs `off-host copy: not configured (BACKUP_REMOTE unset); this dump exists only on this disk` on every run, then exits 0. This is the state until the install below. |
| `BACKUP_REMOTE` set, `rclone` missing or the upload fails | Logs `ERROR: off-host copy …` and **exits 1**. The local dump is already written and kept. |
| `stack_watchdog.sh` `check_backup_freshness` | Only when `BACKUP_REMOTE` is set in `/opt/lem/.env`: a missing `.offsite-last-ok` reports `backup:offsite:missing`, and one older than `WATCHDOG_BACKUP_AGE_HOURS` (48) reports `backup:offsite:stale:<N>h`. Both go out through the same alert path as a stale local dump. |

The old hook ran `rclone copy` of the whole backups directory, and only when both `rclone` and
`BACKUP_REMOTE` were present. Otherwise it skipped silently. That is why nobody could tell from the
log that no off-host copy existed.

## Design choices (owner)

1. **Destination.** Any rclone backend works. The recommendation is **Cloudflare R2**: the
   Cloudflare account already exists for the tunnel, so there is no new vendor or billing
   relationship. Backblaze B2 or any S3-compatible bucket is equivalent for this design.
   Size for planning: the dumps from 2026-10-02 to 2026-10-09 were 1,426,067–1,489,006 bytes each,
   so 35 days of copies is about 52 MB. Check the provider's current pricing when choosing.
2. **Encryption before it leaves the box.** The dump is plain SQL. Secret columns are AES-GCM
   ciphertext (`docs/secrets-at-rest.md`), but the rest holds member data: names, emails and post
   text. Wrap the bucket in an **rclone `crypt` remote**, so the provider stores only ciphertext and
   encrypted file names. The two crypt passwords live in deploy's `rclone.conf`. That file
   *obscures* them; it does not encrypt them. **Store both passwords in the password manager as
   well. Without them, every off-host copy is unreadable.**
3. **Restoring off-host also needs `LEM_SECRET_KEY`.** The encrypted columns decrypt only with
   it, and its off-box backup is a separate open item (#1095). An off-host dump with no off-box key
   restores everything except stored LinkedIn sessions and tokens.
4. **Retention lives in the bucket, not on the box.** Set a lifecycle rule that expires objects
   after **35 days**, and leave the local 7 days as they are. If the provider offers object or
   bucket lock, set a retention of at least 30 days. The box's credential can write objects, so
   without a lock, someone holding a stolen credential could delete the history. The script itself
   never deletes (`--immutable`, single-file `copyto`).
5. **Credential scope.** Use one token, scoped to this one bucket, with object read and write
   only. No account-level keys.

## Install (owner, once)

The copy goes live only after this PR ships in a release, because the cron runs the
deployed-tag checkout at `/opt/lem`.

0. **Check the release is live.** `grep -c offsite_copy /opt/lem/scripts/backup.sh` prints `2` or
   more. After the next 03:00 run, `tail -3 /opt/lem/logs/backup.log` shows `off-host copy: not
   configured`.
1. **Provider console.** Create a private bucket (e.g. `lem-db-backups`), add the 35-day lifecycle
   rule and the lock if one is offered, and create the bucket-scoped token. Note the endpoint, key
   ID and secret straight into rclone in step 3. Do not paste them anywhere else.
2. **As root.** Install rclone and check the flag the script uses:
   ```sh
   apt-get install -y rclone
   rclone version
   rclone help flags | grep -c -- '--immutable'   # must print 1 or more
   ```
3. **As the deploy user.** Run `rclone config` and create two remotes:
   - `lem-store`: type `s3`, provider per step 1, the token from step 1, and the provider's endpoint.
   - `lem-offsite`: type `crypt`, remote `lem-store:lem-db-backups`, standard filename encryption.
     Let rclone generate both passwords, then **copy both into the password manager now**.

   Then run `chmod 600 ~/.config/rclone/rclone.conf` and `rclone lsd lem-store:`. The bucket must
   be listed.
4. **As the deploy user, enable it.**
   ```sh
   cd /opt/lem
   cp -a .env .env.bak-$(date -u +%Y%m%d)
   echo 'BACKUP_REMOTE=lem-offsite:' >> .env
   ```
   Set it in `.env`, not in the crontab environment. The watchdog reads `.env` only.
   From this moment the watchdog reports `backup:offsite:missing` until step 5 writes the marker,
   so run step 5 straight away.
5. **As the deploy user, take one run now.**
   ```sh
   cd /opt/lem && ./scripts/backup.sh >> logs/backup.log 2>&1; echo "rc=$?"
   tail -4 logs/backup.log      # expect: off-host copy OK: db-<stamp>.sql.gz
   rclone lsl lem-offsite:      # expect the same name, size within a few bytes of the local file
   cat backups/.offsite-last-ok
   ```
6. **Restore drill.** Do this now, then monthly. A copy is a backup only once a restore from it
   has worked:
   ```sh
   name=$(cat /opt/lem/backups/.offsite-last-ok)
   mkdir -p /tmp/restore-drill && rclone copyto "lem-offsite:$name" "/tmp/restore-drill/$name"
   gzip -t "/tmp/restore-drill/$name" && echo gzip-ok
   docker run -d --rm --name restore-drill -e MYSQL_ROOT_PASSWORD=drill -e MYSQL_DATABASE=drill mysql:8
   # wait until `docker logs restore-drill 2>&1 | grep -c 'ready for connections'` prints 2
   gunzip -c "/tmp/restore-drill/$name" | docker exec -i -e MYSQL_PWD=drill restore-drill mysql -uroot drill
   docker exec -e MYSQL_PWD=drill restore-drill mysql -uroot -N -e \
     "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='drill'"
   docker stop restore-drill && rm -rf /tmp/restore-drill
   ```
   The table count must match production's. That production count is a read-only query of
   `information_schema.tables` for `linkedin_manager` on `mysql_db`. The drill container is
   throwaway and never touches `mysql_db`.

## Rollback

- Restore the `.env` backup from step 4 (or delete the `BACKUP_REMOTE=` line). `backup.sh` goes
  back to logging `not configured`, and the watchdog stops checking the marker.
- Revoke the token in the provider console. Copies already in the bucket expire under the
  lifecycle rule.
- The code change needs no rollback. With `BACKUP_REMOTE` unset it does nothing but log.

## Failure modes and who sees them

| Failure | Signal | Owner action |
|---|---|---|
| Upload fails (network, provider, revoked token) | `ERROR: off-host copy failed` in `logs/backup.log`, cron exit 1. After 48 h the watchdog alerts `backup:offsite:stale:<N>h` | Run `rclone lsd lem-store:` as deploy. Re-issue the token if it was revoked. |
| `rclone` removed or broken by an upgrade | `ERROR: … rclone is not installed`, then the watchdog alert | Re-run step 2 |
| A file with tonight's name already exists with different content | `rclone` refuses under `--immutable`; same signals as a failed upload | Investigate. Something other than this script wrote to the bucket. |
| Crypt passwords lost | Nothing, until a restore fails | Prevented by step 3's password-manager copy, and caught by the monthly drill |
