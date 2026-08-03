# VPS Go-Live Checklist (manual actions)

Things **you** must do by hand to bring the live/dev VPS online. The repo
provides the automation (`scripts/`, compose overlay, CI); this list is the
human glue — accounts, secrets, DNS, approvals. Work top to bottom.

Legend: 🖱️ click-ops in a web console · ⌨️ run a command · ⏱️ rough time.

---

## 0. Prerequisites (before buying the VPS) ⏱️ ~30 min
- [ ] 🖱️ Own a domain in **Cloudflare** (free plan is fine). Note the zone, e.g. `example.com`.
- [ ] 🖱️ Decide subdomains: `app`, `flower`, `litellm`, `vnc` (defaults in the docs).
- [ ] 🖱️ Confirm **GitHub** repo admin access (you'll set secrets + branch protection).
- [ ] 🖱️ Have **production** credentials ready: LinkedIn OAuth app, live Stripe keys, SendGrid, OpenAI/OpenRouter/Ollama, Pexels/Replicate/RunwayML, PostHog, CapSolver.

## 1. Buy + access the VPS ⏱️ ~15 min
- [ ] 🖱️ Buy a **Hostinger KVM 8** (8 vCPU / 16 GB) — or KVM 4 minimum. Pick Ubuntu 24.04.
- [ ] 🖱️ Add your SSH public key in the Hostinger panel (or note the root password).
- [ ] ⌨️ Confirm access: `ssh root@<vps-ip>`

## 2. Provision the box ⏱️ ~10 min (mostly automated)
- [ ] ⌨️ Copy the bootstrap script up and run it (installs Docker, creates the
      `deploy` user, clones the repo to `/opt/lem`, firewall, SSH hardening,
      log rotation, swap):
      ```
      scp scripts/vps_bootstrap.sh root@<vps-ip>:/root/
      ssh root@<vps-ip> 'REPO_URL=https://github.com/christopherqueenconsulting/linkedin_engagement_manager.git bash /root/vps_bootstrap.sh'
      ```
- [ ] ⌨️ Generate a **CI deploy keypair** (do this on your laptop) **before**
      running bootstrap so you can pre-seed the public key:
      ```
      ssh-keygen -t ed25519 -C "lem-ci-deploy" -f ~/.ssh/lem_ci_deploy -N ""
      ```
      Then the bootstrap command above can install it automatically by passing
      `CI_DEPLOY_PUBKEY="$(cat ~/.ssh/lem_ci_deploy.pub)"` in front of `bash ...`.
      (If you already ran bootstrap, add it manually:)
      ```
      ssh root@<vps-ip> "install -d -m700 -o deploy -g deploy /home/deploy/.ssh && \
        echo '$(cat ~/.ssh/lem_ci_deploy.pub)' >> /home/deploy/.ssh/authorized_keys && \
        chown deploy:deploy /home/deploy/.ssh/authorized_keys && chmod 600 /home/deploy/.ssh/authorized_keys"
      ```
- [ ] ⌨️ Verify: `ssh -i ~/.ssh/lem_ci_deploy deploy@<vps-ip> 'docker ps'`

## 3. Server secrets (`/opt/lem/.env`) ⏱️ ~20 min
- [ ] ⌨️ `ssh deploy@<vps-ip>` then `cd /opt/lem && cp .env.prod.example .env`
- [ ] ⌨️ Fill in every `CHANGE_ME` in `.env` (DB passwords, ADMIN_SECRET,
      API_ACCESS_TOKENS, all API keys, live Stripe, SendGrid). Use strong random
      values: `openssl rand -hex 32`.
- [ ] ⌨️ `chmod 600 .env`
- [ ] 📝 Keep **`API_ACCESS_TOKENS`** on the server only. Since issue #950 it is a
      **non-browser** credential (scripts, Postman, `/api/admin/*`); the SPA authenticates
      on its session cookie, so nothing in the build has to match it and you can rotate it
      here alone.
- [ ] ⌨️ Sanity check: `./scripts/check_env.sh`

## 4. Cloudflare Tunnel + Access ⏱️ ~25 min
- [ ] 🖱️ Cloudflare **Zero Trust → Networks → Tunnels → Create tunnel** (Cloudflared), name it `lem`.
- [ ] 📝 Copy the tunnel **token** → paste into `TUNNEL_TOKEN` in `/opt/lem/.env`.
- [ ] 🖱️ Add **Public Hostnames** on the tunnel:
      - `app.<domain>` → `http://web_app:8000`
      - `flower.<domain>` → `http://flower:8555`
      - `litellm.<domain>` → `http://litellm:4000`
      - `vnc.<domain>` → `http://selenium-chrome:7900`
- [ ] 🖱️ **Zero Trust → Access → Applications**: add self-hosted apps for
      `flower`, `litellm`, `vnc` subdomains, policy = allow only your email/SSO.
- [ ] 🖱️ (Optional) Enable WAF / rate limiting on `app.<domain>`.

## 5. LinkedIn OAuth ⏱️ ~10 min
- [ ] 🖱️ LinkedIn Developer Console → your app → **Auth** → add redirect URL:
      `https://app.<domain>/auth/linkedin/callback`
- [ ] ⌨️ Ensure `LI_REDIRECT_URL` in `/opt/lem/.env` matches exactly, and `NGROK_PLAN=off`.

## 6. GitHub repo configuration ⏱️ ~20 min
- [ ] ⌨️ **Unblock the CI workflow files** (the PR could not push them — token
      lacked the scope). Once, on your laptop:
      ```
      gh auth refresh -h github.com -s workflow
      git -C <repo> push        # pushes the held-back "ci(deploy)" commit
      ```
- [ ] 🖱️ **Settings → Secrets and variables → Actions → New secret** (repo):
      - `VPS_HOST` = `<vps-ip>`
      - `VPS_USER` = `deploy`
      - `VPS_SSH_KEY` = contents of `~/.ssh/lem_ci_deploy` (the **private** key)
      - `GHCR_PAT` = a Personal Access Token with `read:packages` (for the VPS pull)
      - (verify existing: `GITGUARDIAN_API_KEY`, `ANTHROPIC_API_KEY`, `CODECOV_TOKEN`)
      - *(no `UI_API_TOKEN` — retired in #950; the SPA ships no LEM secret)*
- [ ] 🖱️ **Settings → Environments → New environment** `production` → add yourself
      as a **Required reviewer** (gates the deploy job).
- [ ] 🖱️ **Settings → Branches → Add branch ruleset** for `main`: require PR +
      ≥1 review + status checks: `CI / Unit Tests`,
      `CI / Integration Test w/ Coverage`, `CodeQL Security Analysis`,
      `GitGuardian Security Scan`.
- [ ] 🖱️ **Settings → Actions → General**: ensure Actions can write packages
      (GHCR push uses the built-in token).

## 7. First deploy ⏱️ ~15 min
- [ ] 🖱️ Merge PR #126 (after the workflow commit is pushed in step 6).
- [ ] 🖱️ Merge the **release-please** PR it opens → creates tag `vX.Y.Z` →
      `Build & Deploy Release` runs → approve the `production` gate.
      _Or_ bootstrap manually first:
      `ssh deploy@<vps-ip> 'cd /opt/lem && ./scripts/deploy.sh latest'`
- [ ] ⌨️ Verify: `curl https://app.<domain>/health` → `{"status":"healthy"}`
- [ ] 🖱️ Load `https://app.<domain>` (SPA), then `flower.<domain>` (should force
      Cloudflare Access login).
- [ ] ⌨️ Confirm API gate: `curl https://app.<domain>/api/posts` → 401;
      with `-H "Authorization: Bearer <token>"` → not 401.

## 8. Backups + ops ⏱️ ~10 min
- [ ] ⌨️ Add the nightly backup cron on the VPS:
      ```
      ssh deploy@<vps-ip> 'crontab -l 2>/dev/null; echo "0 3 * * * cd /opt/lem && ./scripts/backup.sh >> logs/backup.log 2>&1" | crontab -'
      ```
- [ ] 🖱️ (Optional) Configure `rclone` + `BACKUP_REMOTE` for off-box backups (Cloudflare R2 / S3).
- [ ] 🖱️ (Optional) External uptime monitor on `https://app.<domain>/health`.
- [ ] ⌨️ Test rollback once: `ssh deploy@<vps-ip> 'cd /opt/lem && ./scripts/rollback.sh <prev-tag>'`

---

### Rollback / redeploy later
GitHub → Actions → **Redeploy / Rollback VPS** → run with a tag (tick `rollback`
to skip migrations). Or on the box: `cd /opt/lem && ./scripts/rollback.sh <tag>`.

See `docs/DEPLOYMENT.md` for the full runbook and troubleshooting table.
