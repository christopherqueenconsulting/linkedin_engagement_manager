#!/usr/bin/env bash
# GitHub App installation-token minting — the pipeline's IDENTITY, not just its permissions.
#
# Why an app and not the PAT it replaces: the PAT acts as the OWNER. That makes an owner-approval
# gate on outside contributions both unimplementable (GitHub forbids approving your own PR, so
# every agent PR would sit permanently red) and pointless (a prompt-injected agent run holding the
# owner's credential could `gh pr review --approve` an attacker's PR AS THE OWNER). With an app the
# runner can author and merge but can NEVER approve, because the app is not in the approver
# allowlist.
#
# ── KEY CUSTODY (the part that is not obvious) ────────────────────────────────────────────────
# Agent runs execute as the SAME uid as this runner, with `--dangerously-skip-permissions`. Claude
# Code's `--add-dir` scopes the FILE tools, but the Bash tool can read anything the uid can read —
# so file modes on a key owned by that uid protect nothing at all. The threat is explicit in the
# RUNBOOK's own prompt-injection section: issue text is written by strangers.
#
# So the key does not live where the agent's uid can reach it. It is root-owned in /etc/lem, and a
# root-run systemd timer (lem-gh-token.timer) mints the short-lived installation token INTO
# $BASE/state/gh-app-token for this runner to consume. The runner never holds the private key.
#
# What that buys, precisely: an agent can still steal the CACHED TOKEN — it must be readable to be
# usable — but that token expires in ~1h and carries exactly the authority the pipeline already
# has. It cannot steal the key, which is unbounded in time and survives token rotation. Bounding
# the blast radius from "forever" to "one hour" is the achievable win here; pretending file modes
# solve it is not.
#
# If the key IS readable (dev boxes, or before the timer is installed) this falls back to minting
# in-process, so the pipeline keeps working while the hardening is rolled out.
#
# Implementation notes:
#   - RS256 signed with `openssl dgst`, NOT PyJWT: venvs here are per-worktree and the last
#     `poetry install` anywhere wins, so a Python dependency in the credential path is a lottery.
#   - Cached with its expiry and reused until 5 minutes before it lapses, so only the first tick of
#     each hour pays a mint.
#   - Never logged, never echoed except by gh_app_token() itself, whose only caller assigns it.

BASE="${BASE:-/home/lem/agent-pipeline}"
# Preferred (hardened) location first, then the legacy in-BASE path for boxes not yet migrated.
GH_APP_KEY="${GH_APP_KEY:-/etc/lem/github-app.pem}"
GH_APP_KEY_LEGACY="${GH_APP_KEY_LEGACY:-$BASE/secrets/github-app.pem}"
GH_APP_TOKEN_CACHE="${GH_APP_TOKEN_CACHE:-$BASE/state/gh-app-token}"
GH_APP_TOKEN_SKEW="${GH_APP_TOKEN_SKEW:-300}"   # refresh this many seconds before expiry

_b64url() { openssl base64 -A | tr '+/' '-_' | tr -d '='; }

# Which key can we actually read? Empty when none — the normal, HARDENED case on the VPS, where
# only the root timer holds it and this runner is a token consumer.
_gh_app_key_path() {
  [ -r "$GH_APP_KEY" ] && { printf '%s' "$GH_APP_KEY"; return 0; }
  [ -r "$GH_APP_KEY_LEGACY" ] && { printf '%s' "$GH_APP_KEY_LEGACY"; return 0; }
  printf ''
}

# Mint a short-lived JWT proving we hold the app's private key. Ten-minute max life per GitHub; 9
# used here, with iat backdated 60s so a slow clock cannot make the token "future-dated".
_gh_app_jwt() {
  local now hdr pl sig key
  key="$(_gh_app_key_path)"
  [ -n "$key" ] || return 1
  [ -n "${GH_APP_ID:-}" ] || return 1
  now="$(date +%s)"
  hdr="$(printf '{"alg":"RS256","typ":"JWT"}' | _b64url)"
  pl="$(printf '{"iat":%d,"exp":%d,"iss":"%s"}' "$((now - 60))" "$((now + 540))" "$GH_APP_ID" | _b64url)"
  sig="$(printf '%s.%s' "$hdr" "$pl" | openssl dgst -sha256 -sign "$key" -binary 2>/dev/null | _b64url)"
  [ -n "$sig" ] || return 1
  printf '%s.%s.%s' "$hdr" "$pl" "$sig"
}

# Resolve the installation id once if the owner did not pin it, cached so this costs one call ever.
_gh_app_installation_id() {
  local jwt f id
  f="$BASE/state/gh-app-installation-id"
  if [ -z "${GH_APP_INSTALLATION_ID:-}" ] && [ -s "$f" ]; then GH_APP_INSTALLATION_ID="$(cat "$f")"; fi
  [ -n "${GH_APP_INSTALLATION_ID:-}" ] && { printf '%s' "$GH_APP_INSTALLATION_ID"; return 0; }
  jwt="$(_gh_app_jwt)" || return 1
  id="$(curl -sS --max-time 10 -H "Authorization: Bearer $jwt" -H "Accept: application/vnd.github+json" \
        https://api.github.com/app/installations 2>/dev/null | jq -r '.[0].id // empty')"
  [ -n "$id" ] || return 1
  printf '%s' "$id" > "$f"
  printf '%s' "$id"
}

# Read the cache, honouring the refresh skew. Prints the token, or nothing.
_gh_app_cached_token() {
  local now exp tok
  [ -s "$GH_APP_TOKEN_CACHE" ] || return 1
  now="$(date +%s)"
  exp="$(cut -d' ' -f1 < "$GH_APP_TOKEN_CACHE" 2>/dev/null)"
  tok="$(cut -d' ' -f2- < "$GH_APP_TOKEN_CACHE" 2>/dev/null)"
  case "$exp" in ''|*[!0-9]*) return 1 ;; esac
  [ -n "$tok" ] || return 1
  [ "$now" -lt "$(( exp - GH_APP_TOKEN_SKEW ))" ] || return 1
  printf '%s' "$tok"
}

# Mint a fresh installation token and write the cache. Requires a readable key, so on a hardened
# box this is the ROOT timer's path, not the runner's.
gh_app_mint_token() {
  local jwt inst resp tok exp now
  now="$(date +%s)"
  jwt="$(_gh_app_jwt)" || return 1
  inst="$(_gh_app_installation_id)" || return 1
  resp="$(curl -sS --max-time 15 -X POST \
            -H "Authorization: Bearer $jwt" -H "Accept: application/vnd.github+json" \
            "https://api.github.com/app/installations/$inst/access_tokens" 2>/dev/null)"
  tok="$(printf '%s' "$resp" | jq -r '.token // empty')"
  [ -n "$tok" ] || return 1
  exp="$(date -d "$(printf '%s' "$resp" | jq -r '.expires_at // empty')" +%s 2>/dev/null || echo 0)"
  [ "${exp:-0}" -gt "$now" ] || exp="$(( now + 3300 ))"
  mkdir -p "$(dirname "$GH_APP_TOKEN_CACHE")"
  ( umask 077; printf '%s %s\n' "$exp" "$tok" > "$GH_APP_TOKEN_CACHE.new" )
  mv "$GH_APP_TOKEN_CACHE.new" "$GH_APP_TOKEN_CACHE"
  # When root mints for the runner, hand ownership over or the consumer cannot read it.
  [ "$(id -u)" = "0" ] && chown "${LEM_RUNNER_USER:-lem}:${LEM_RUNNER_USER:-lem}" "$GH_APP_TOKEN_CACHE" 2>/dev/null
  printf '%s' "$tok"
}

# gh_app_token -> prints a usable installation token, or nothing (rc 1).
# FAILS SOFT ON PURPOSE: callers fall back to the PAT, so a missing key or a GitHub blip degrades
# the pipeline's identity, never its ability to run.
gh_app_token() {
  local tok
  tok="$(_gh_app_cached_token)" && { printf '%s' "$tok"; return 0; }
  # No usable cache. On a hardened box the key is unreadable here and the root timer owns
  # refreshing, so the honest answer is "no token" rather than a mint that cannot succeed.
  [ -n "$(_gh_app_key_path)" ] || return 1
  gh_app_mint_token
}

# Export GH_TOKEN as the app when possible. 0 = app identity in force, 1 = keep the existing one.
gh_app_export_token() {
  local tok
  [ "${USE_GH_APP:-0}" = "1" ] || return 1
  tok="$(gh_app_token)" || return 1
  export GH_TOKEN="$tok"
  export GH_APP_IDENTITY_ACTIVE=1
  return 0
}
