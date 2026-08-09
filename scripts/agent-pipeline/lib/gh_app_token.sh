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
#     while it still has more life left than one agent run can consume (see GH_APP_TOKEN_SKEW).
#   - Never logged, never echoed to stdout except by gh_app_token() itself (whose only caller
#     assigns it), and never written anywhere but the 0600 cache.

BASE="${BASE:-/home/lem/agent-pipeline}"
# GH_APP_ID / GH_APP_INSTALLATION_ID live in secrets.env, not config.env — the private key's
# companions are secrets. Source it here rather than relying on lib/posthog.sh happening to be
# sourced first: a reorder of tick.sh's lib loop would otherwise leave GH_APP_ID unset and silently
# drop the pipeline back onto the OWNER's PAT, which is the exact property this file exists to end.
# shellcheck disable=SC1091
[ -f "$BASE/secrets.env" ] && . "$BASE/secrets.env" 2>/dev/null
GH_APP_KEY="${GH_APP_KEY:-$BASE/secrets/github-app.pem}"
GH_APP_TOKEN_CACHE="${GH_APP_TOKEN_CACHE:-$BASE/state/gh-app-token}"
# Refresh this many seconds before expiry. An installation token lives 60 min and is handed to a
# `claude -p` run that may take CLAUDE_TIMEOUT (45m) before it pushes, so the skew has to exceed a
# whole run: at 300s a tick could hand an agent a credential with five minutes left, and the run
# would do all its work and then fail to push. 50 min leaves the shortest usable token still
# outliving the longest run. Cost of the tighter window is one two-call mint per ~10 min of ticks.
GH_APP_TOKEN_SKEW="${GH_APP_TOKEN_SKEW:-3000}"

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
# this costs one call ever, not one per mint. The installation is picked BY ACCOUNT, not as `.[0]`:
# the cache is never invalidated, so a second installation appearing ahead of ours would pin the
# wrong one permanently. Falls back to the first entry when the account cannot be matched.
_gh_app_installation_id() {
  local jwt f id acct
  f="$BASE/state/gh-app-installation-id"
  acct="${GH_APP_ACCOUNT:-${SLUG:-}}"; acct="${acct%%/*}"
  if [ -z "${GH_APP_INSTALLATION_ID:-}" ] && [ -s "$f" ]; then GH_APP_INSTALLATION_ID="$(cat "$f")"; fi
  [ -n "${GH_APP_INSTALLATION_ID:-}" ] && { printf '%s' "$GH_APP_INSTALLATION_ID"; return 0; }
  jwt="$(_gh_app_jwt)" || return 1
  id="$(curl -sS --max-time 10 -H "Authorization: Bearer $jwt" -H "Accept: application/vnd.github+json" \
        https://api.github.com/app/installations 2>/dev/null \
        | jq -r --arg a "$acct" '[.[] | select((.account.login // "") == $a) | .id] | first // (.[0].id // empty)' 2>/dev/null)"
  [ -n "$id" ] || return 1
  printf '%s' "$id" > "$f"
  printf '%s' "$id"
}

# The bot login this app acts as on GitHub — "<app slug>[bot]", e.g. cqc-lem-agent-pipeline[bot].
# It is NOT cosmetic: tick.sh's trust boundary matches label appliers and issue authors by login,
# and every one of those checks was written when the pipeline was `gitchrisqueen`. Resolved from
# GET /app (the JWT already exists) and cached, because a wrong or missing login here silently
# deadlocks the lanes rather than failing loudly.
_gh_app_bot_login() {
  local jwt f slug
  f="$BASE/state/gh-app-bot-login"
  [ -n "${GH_APP_BOT_LOGIN:-}" ] && { printf '%s' "$GH_APP_BOT_LOGIN"; return 0; }
  if [ -s "$f" ]; then printf '%s' "$(cat "$f")"; return 0; fi
  jwt="$(_gh_app_jwt)" || return 1
  slug="$(curl -sS --max-time 10 -H "Authorization: Bearer $jwt" -H "Accept: application/vnd.github+json" \
          https://api.github.com/app 2>/dev/null | jq -r '.slug // empty')"
  [ -n "$slug" ] || return 1
  printf '%s[bot]' "$slug" > "$f"
  printf '%s[bot]' "$slug"
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
  local tok login
  [ "${USE_GH_APP:-0}" = "1" ] || return 1
  tok="$(gh_app_token)" || return 1
  export GH_TOKEN="$tok"
  export GH_APP_IDENTITY_ACTIVE=1
  # Best-effort: the token works without it, but the trust boundary needs the login to recognise
  # the pipeline's own writes. tick.sh warns when this comes back empty.
  login="$(_gh_app_bot_login)" && export GH_APP_BOT_LOGIN="$login"
  return 0
}
