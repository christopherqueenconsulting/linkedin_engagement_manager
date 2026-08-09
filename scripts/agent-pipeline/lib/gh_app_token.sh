#!/usr/bin/env bash
# GitHub App installation-token minting — the pipeline's IDENTITY, not just its permissions.
#
# Why an app and not the PAT it replaces: the PAT acts as the OWNER. That makes the
# Contribution Gate (a required check demanding owner approval on outside contributions) both
# unimplementable and pointless — GitHub forbids approving your own PR, so every agent PR would
# be permanently red; and a prompt-injected agent run holding the owner's credential could
# `gh pr review --approve` an attacker's PR *as the owner*, greening the very gate that exists
# to stop it. With an app, the runner can author and merge but can NEVER approve, because the
# app is not in the approver allowlist. One bug is then no longer sufficient to reach prod.
#
# Secondary win: installation tokens expire in ~1h, so a leaked token has an hour of life
# instead of until someone notices — and app-authored PRs DO trigger workflows (unlike a
# GITHUB_TOKEN-authored PR, which lands in `action_required` and never runs CI — the trap that
# made release-please PRs sit unbuilt until the token split of #319).
#
# Implementation notes:
#   - RS256 is signed with `openssl dgst`, NOT PyJWT. The box's venvs are per-worktree and the
#     one `poetry install` that ran last wins, so a python dependency here would be a lottery.
#     openssl is in the base image and stable.
#   - The minted token is cached in $BASE/state/gh-app-token (0600) with its expiry, and reused
#     until 5 minutes before it lapses. A tick therefore costs zero extra API calls in the
#     common case; only the first tick of each hour pays the two-call mint.
#   - Never logged, never echoed to stdout except by gh_app_token() itself (whose only caller
#     assigns it), and never written anywhere but the 0600 cache.

BASE="${BASE:-/home/lem/agent-pipeline}"
GH_APP_KEY="${GH_APP_KEY:-$BASE/secrets/github-app.pem}"
GH_APP_TOKEN_CACHE="${GH_APP_TOKEN_CACHE:-$BASE/state/gh-app-token}"
GH_APP_TOKEN_SKEW="${GH_APP_TOKEN_SKEW:-300}"   # refresh this many seconds before expiry

_b64url() { openssl base64 -A | tr '+/' '-_' | tr -d '='; }

# Mint a short-lived JWT proving we hold the app's private key. Ten-minute max life per GitHub;
# 9 used here, with the iat backdated 60s so a slow clock on this box cannot make it "future".
_gh_app_jwt() {
  local now hdr pl sig
  [ -r "$GH_APP_KEY" ] || return 1
  [ -n "${GH_APP_ID:-}" ] || return 1
  now="$(date +%s)"
  hdr="$(printf '{"alg":"RS256","typ":"JWT"}' | _b64url)"
  pl="$(printf '{"iat":%d,"exp":%d,"iss":"%s"}' "$((now - 60))" "$((now + 540))" "$GH_APP_ID" | _b64url)"
  sig="$(printf '%s.%s' "$hdr" "$pl" | openssl dgst -sha256 -sign "$GH_APP_KEY" -binary 2>/dev/null | _b64url)"
  [ -n "$sig" ] || return 1
  printf '%s.%s.%s' "$hdr" "$pl" "$sig"
}

# Resolve the installation id once if the owner did not pin it. Cached into the same state dir so
# this costs one call ever, not one per mint.
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

# gh_app_token -> prints a usable installation token, or nothing (rc 1) when the app is not
# configured/reachable. FAILS SOFT ON PURPOSE: every caller falls back to the PAT, so a missing
# key or a GitHub blip degrades the pipeline's identity, never its ability to run.
gh_app_token() {
  local now exp tok jwt inst resp
  now="$(date +%s)"

  # Cache format: "<expiry_epoch> <token>" — one line, 0600.
  if [ -s "$GH_APP_TOKEN_CACHE" ]; then
    exp="$(cut -d' ' -f1 < "$GH_APP_TOKEN_CACHE" 2>/dev/null)"
    tok="$(cut -d' ' -f2- < "$GH_APP_TOKEN_CACHE" 2>/dev/null)"
    case "$exp" in ''|*[!0-9]*) exp=0 ;; esac
    if [ -n "$tok" ] && [ "$now" -lt "$(( exp - GH_APP_TOKEN_SKEW ))" ]; then
      printf '%s' "$tok"; return 0
    fi
  fi

  jwt="$(_gh_app_jwt)" || return 1
  inst="$(_gh_app_installation_id)" || return 1
  resp="$(curl -sS --max-time 15 -X POST \
            -H "Authorization: Bearer $jwt" -H "Accept: application/vnd.github+json" \
            "https://api.github.com/app/installations/$inst/access_tokens" 2>/dev/null)"
  tok="$(printf '%s' "$resp" | jq -r '.token // empty')"
  [ -n "$tok" ] || return 1
  # expires_at is ISO-8601; fall back to now+55m if it is unparseable rather than caching forever.
  exp="$(date -d "$(printf '%s' "$resp" | jq -r '.expires_at // empty')" +%s 2>/dev/null || echo 0)"
  [ "${exp:-0}" -gt "$now" ] || exp="$(( now + 3300 ))"

  mkdir -p "$(dirname "$GH_APP_TOKEN_CACHE")"
  ( umask 077; printf '%s %s\n' "$exp" "$tok" > "$GH_APP_TOKEN_CACHE.new" )
  mv "$GH_APP_TOKEN_CACHE.new" "$GH_APP_TOKEN_CACHE"
  printf '%s' "$tok"
}

# Export GH_TOKEN as the app when possible. Returns 0 when the app identity is in force, 1 when
# the caller should keep whatever credential it already had.
gh_app_export_token() {
  local tok
  [ "${USE_GH_APP:-0}" = "1" ] || return 1
  tok="$(gh_app_token)" || return 1
  export GH_TOKEN="$tok"
  export GH_APP_IDENTITY_ACTIVE=1
  return 0
}
