#!/usr/bin/env bash
# Guided one-time provisioning for a fresh Hostinger VPS (Ubuntu 22.04/24.04).
# Run as root (or with sudo). Idempotent — safe to re-run.
#
#   scp scripts/vps_bootstrap.sh root@<vps>:/root/ && ssh root@<vps> 'bash /root/vps_bootstrap.sh'
#   # or non-interactively, pre-seeding everything:
#   REPO_URL=... CI_DEPLOY_PUBKEY="ssh-ed25519 AAAA..." ASSUME_YES=1 bash vps_bootstrap.sh
#
# What it does: installs Docker, hardens the box, clones the repo to /opt/lem,
# installs the CI deploy key, scaffolds .env, and (optionally) runs the first
# deploy. Anything it can't do for you is printed as a checklist at the end.
set -euo pipefail

# --- Config (override via env) ------------------------------------------------
DEPLOY_USER="${DEPLOY_USER:-deploy}"
APP_DIR="${APP_DIR:-/opt/lem}"
REPO_URL="${REPO_URL:-https://github.com/christopherqueenconsulting/linkedin_engagement_manager.git}"
REPO_BRANCH="${REPO_BRANCH:-main}"
SWAP_SIZE="${SWAP_SIZE:-4G}"
CI_DEPLOY_PUBKEY="${CI_DEPLOY_PUBKEY:-}"   # paste the CI public key to auto-install
ASSUME_YES="${ASSUME_YES:-0}"              # 1 = answer "yes" to every prompt
RUN_FIRST_DEPLOY="${RUN_FIRST_DEPLOY:-ask}" # ask | yes | no
MIN_CORES=4
MIN_RAM_GB=8

# --- Pretty output ------------------------------------------------------------
if [[ -t 1 ]]; then BOLD=$'\e[1m'; GRN=$'\e[32m'; YLW=$'\e[33m'; RED=$'\e[31m'; RST=$'\e[0m'
else BOLD=""; GRN=""; YLW=""; RED=""; RST=""; fi
STEP=0
step() { STEP=$((STEP+1)); echo; echo "${BOLD}${GRN}[${STEP}] $*${RST}"; }
info() { echo "    $*"; }
warn() { echo "${YLW}    ! $*${RST}"; }
err()  { echo "${RED}    x $*${RST}" >&2; }

# Prompt yes/no. Non-interactive (piped) or ASSUME_YES → use the default.
ask() {
  local prompt="$1" default="${2:-N}" reply
  if [[ "$ASSUME_YES" == "1" ]]; then reply="y"
  elif [[ -e /dev/tty ]]; then
    read -r -p "    ${prompt} [$([[ $default == Y ]] && echo Y/n || echo y/N)] " reply </dev/tty || reply=""
  else reply=""; fi
  reply="${reply:-$default}"
  [[ "$reply" =~ ^[Yy] ]]
}

[[ "$(id -u)" -eq 0 ]] || { err "Run as root (or with sudo)."; exit 1; }

# --- 0. Preflight: sizing -----------------------------------------------------
step "Preflight checks"
CORES="$(nproc)"
RAM_GB="$(awk '/MemTotal/ {printf "%d", $2/1024/1024}' /proc/meminfo)"
DISK_GB="$(df -BG --output=avail / | tail -1 | tr -dc '0-9')"
info "CPU cores: ${CORES} | RAM: ${RAM_GB} GB | free disk: ${DISK_GB} GB"
(( CORES >= MIN_CORES ))   || warn "Below recommended ${MIN_CORES} cores — Selenium/Chrome will be tight."
(( RAM_GB >= MIN_RAM_GB )) || warn "Below recommended ${MIN_RAM_GB} GB RAM — relying on swap; consider a bigger plan."
(( DISK_GB >= 20 ))        || warn "Low free disk (${DISK_GB} GB) — images + volumes may fill it."
if ! grep -qiE 'ubuntu' /etc/os-release; then warn "Not Ubuntu — apt/docker steps may differ."; fi

# --- 1. Base packages ---------------------------------------------------------
step "Installing base packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y -qq
apt-get install -y -qq ca-certificates curl git ufw gnupg acl
info "ok"

# --- 2. Docker Engine + Compose v2 --------------------------------------------
step "Installing Docker Engine + Compose"
if ! command -v docker >/dev/null 2>&1; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -y -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  info "installed $(docker --version)"
else
  info "already present: $(docker --version)"
fi
systemctl enable --now docker >/dev/null 2>&1 || true

step "Configuring Docker (log rotation + live-restore)"
mkdir -p /etc/docker
cat > /etc/docker/daemon.json <<'JSON'
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "20m", "max-file": "5" },
  "live-restore": true
}
JSON
systemctl restart docker
info "ok"

# --- 3. Deploy user -----------------------------------------------------------
step "Creating ${DEPLOY_USER} user"
if ! id "$DEPLOY_USER" >/dev/null 2>&1; then
  useradd -m -s /bin/bash "$DEPLOY_USER"
  info "created"
else
  info "already exists"
fi
usermod -aG docker "$DEPLOY_USER"

# --- 4. CI deploy key ---------------------------------------------------------
step "CI deploy SSH key"
SSH_DIR="/home/${DEPLOY_USER}/.ssh"
AUTH_KEYS="${SSH_DIR}/authorized_keys"
install -d -m 700 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "$SSH_DIR"
touch "$AUTH_KEYS"
if [[ -n "$CI_DEPLOY_PUBKEY" ]]; then
  if grep -qF "$CI_DEPLOY_PUBKEY" "$AUTH_KEYS" 2>/dev/null; then
    info "key already authorized"
  else
    echo "$CI_DEPLOY_PUBKEY" >> "$AUTH_KEYS"
    info "installed provided CI public key"
  fi
else
  warn "No CI_DEPLOY_PUBKEY provided — add the CI public key to ${AUTH_KEYS} later"
  warn "(it becomes the GitHub VPS_SSH_KEY secret's matching public half)."
fi
chown -R "$DEPLOY_USER:$DEPLOY_USER" "$SSH_DIR"
chmod 600 "$AUTH_KEYS"

# --- 5. Repo -----------------------------------------------------------------
step "Cloning repo to ${APP_DIR} (branch ${REPO_BRANCH})"
mkdir -p "$APP_DIR"
if [[ ! -d "${APP_DIR}/.git" ]]; then
  git clone --branch "$REPO_BRANCH" "$REPO_URL" "$APP_DIR"
  info "cloned"
else
  git -C "$APP_DIR" fetch --quiet origin "$REPO_BRANCH" && git -C "$APP_DIR" checkout --quiet "$REPO_BRANCH" || true
  info "already cloned (fetched latest ${REPO_BRANCH})"
fi
mkdir -p "${APP_DIR}/logs"
chown -R "$DEPLOY_USER:$DEPLOY_USER" "$APP_DIR"
# Some containers run as root, others as celeryworker (uid 1000), but they
# share the bind-mounted logs dir and a single daily logfile. A default ACL
# lets uid 1000 write even when a root service creates the file first
# (otherwise the celery workers crash-loop on PermissionError).
if command -v setfacl >/dev/null 2>&1; then
  setfacl -R  -m u:1000:rwX "${APP_DIR}/logs" || true
  setfacl -dR -m u:1000:rwX "${APP_DIR}/logs" || true
fi

# --- 6. Firewall + SSH hardening ---------------------------------------------
step "Firewall (SSH-only inbound; Cloudflare Tunnel dials out)"
ufw allow OpenSSH >/dev/null
ufw --force enable >/dev/null
info "ufw enabled"

step "Hardening SSH (key-only auth, no root login)"
sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config
systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || true
warn "Confirm you can SSH as ${DEPLOY_USER} with a key BEFORE closing this session."

# --- 7. Swap ------------------------------------------------------------------
step "Swap (${SWAP_SIZE}) for memory-heavy Selenium/Chrome"
if [[ ! -f /swapfile ]]; then
  fallocate -l "$SWAP_SIZE" /swapfile || dd if=/dev/zero of=/swapfile bs=1M count="$(( ${SWAP_SIZE%G} * 1024 ))"
  chmod 600 /swapfile
  mkswap /swapfile >/dev/null
  swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  info "created and enabled"
else
  info "swapfile already present"
fi

# --- 8. Env scaffold ----------------------------------------------------------
step "Environment file (${APP_DIR}/.env)"
ENV_FILE="${APP_DIR}/.env"
if [[ -f "$ENV_FILE" ]]; then
  info ".env already exists — leaving it untouched"
else
  cp "${APP_DIR}/.env.prod.example" "$ENV_FILE"
  chown "$DEPLOY_USER:$DEPLOY_USER" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  info "created from .env.prod.example (chmod 600)"
  warn "Fill in every CHANGE_ME value (DB, ADMIN_SECRET, API_ACCESS_TOKENS, TUNNEL_TOKEN, API keys, live Stripe)."
  if ask "Open ${ENV_FILE} in an editor now?" N; then
    "${EDITOR:-nano}" "$ENV_FILE" </dev/tty >/dev/tty 2>&1 || true
  fi
fi

# Validate keys present (non-fatal — secrets may still be placeholders).
if [[ -x "${APP_DIR}/scripts/check_env.sh" ]]; then
  if "${APP_DIR}/scripts/check_env.sh" "$ENV_FILE" "${APP_DIR}/.env.prod.example" >/dev/null 2>&1; then
    info "check_env: all required keys present"
  else
    warn "check_env: some keys are missing — run ${APP_DIR}/scripts/check_env.sh to see which."
  fi
fi

# --- 9. Optional first deploy -------------------------------------------------
step "First deploy"
do_deploy=false
case "$RUN_FIRST_DEPLOY" in
  yes) do_deploy=true ;;
  no)  do_deploy=false ;;
  *)   if grep -q 'CHANGE_ME' "$ENV_FILE" 2>/dev/null; then
         warn "Skipping — .env still has CHANGE_ME placeholders. Finish secrets first."
       elif ask "Run ./scripts/deploy.sh latest now (as ${DEPLOY_USER})?" N; then
         do_deploy=true
       fi ;;
esac
if $do_deploy; then
  info "Deploying latest…"
  su - "$DEPLOY_USER" -c "cd '${APP_DIR}' && ./scripts/deploy.sh latest" || err "Deploy failed — see output above."
else
  info "Deferred. Run later:  su - ${DEPLOY_USER} -c 'cd ${APP_DIR} && ./scripts/deploy.sh latest'"
fi

# --- Summary ------------------------------------------------------------------
cat <<EOF

${BOLD}${GRN}Bootstrap complete.${RST} Remaining manual steps (see docs/SETUP_CHECKLIST.md):
  ${CI_DEPLOY_PUBKEY:+✓}${CI_DEPLOY_PUBKEY:-•} CI deploy key on ${DEPLOY_USER}@host  ${CI_DEPLOY_PUBKEY:+(done)}
  • Fill secrets in ${ENV_FILE} (esp. TUNNEL_TOKEN, API_ACCESS_TOKENS)
  • Create the Cloudflare Tunnel + Access policies; map app/flower/litellm/vnc
  • LinkedIn redirect → https://app.<domain>/auth/linkedin/callback
  • GitHub: secrets (VPS_HOST/USER/SSH_KEY, GHCR_PAT),
    'production' environment, branch protection on main
  • Add nightly backup cron:
      su - ${DEPLOY_USER} -c 'crontab -l 2>/dev/null; echo "0 3 * * * cd ${APP_DIR} && ./scripts/backup.sh >> logs/backup.log 2>&1" | crontab -'
EOF
