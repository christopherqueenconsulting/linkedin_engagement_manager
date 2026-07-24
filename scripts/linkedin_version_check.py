#!/usr/bin/env python3
"""LinkedIn versioned-API version checker.

LinkedIn retires versioned-API versions on a rolling window (as of 2026-07 only ~7 months
are live at once). A retired ``LI_API_VERSION`` makes every ``/rest/*`` call answer
``426 NONEXISTENT_VERSION`` — which silently demotes native document posts to the legacy
ugcPost path and would break any future versioned call outright.

There is no discovery endpoint, so liveness is probed. ``GET /rest/me`` is a side-effect-free
read that answers 426 NONEXISTENT_VERSION when a version is not active, and something else
(403 without the read scope, 200 with it) when it is. An unissued FUTURE version answers 426
too, so the newest active version is simply the newest candidate that is not 426.

Policy: do NOT chase the newest version every month — a version bump can change API behaviour,
and a pin that works is worth keeping. Bump only when the pinned version is retired, or has
drifted to within ``--min-headroom`` of the oldest still-live version.

Pure planning logic is unit-tested; the probe is mocked.
"""

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Optional

PROBE_URL = "https://api.linkedin.com/rest/me"
DEFAULT_MIN_HEADROOM = 2
DEFAULT_LOOKBACK = 24
DEFAULT_LOOKAHEAD = 2
DEFAULT_ATTEMPTS = 3
ENV_KEY = "LI_API_VERSION"

# Only these prove a version is live: 200 with a read scope, 403 ACCESS_DENIED without one
# (LEM's token). 426 NONEXISTENT_VERSION is the only "not live" answer. EVERYTHING else —
# throttling, outages, an unexpected 4xx — is undecidable and must not be guessed at, or a
# transient blip would forge a live-window and drive a bad keep/bump decision.
ACTIVE_STATUSES = frozenset({200, 403})
INACTIVE_STATUSES = frozenset({426})
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


class ProbeError(RuntimeError):
    """The probe could not decide whether a version is active."""


def candidate_versions(today: datetime, lookback: int = DEFAULT_LOOKBACK,
                       lookahead: int = DEFAULT_LOOKAHEAD) -> list[str]:
    """Candidate YYYYMM versions, NEWEST first, spanning lookahead ahead .. lookback back."""
    start = today.year * 12 + (today.month - 1) + lookahead
    return [f"{(start - i) // 12:04d}{(start - i) % 12 + 1:02d}"
            for i in range(lookahead + lookback + 1)]


def is_active_response(status_code: int, body: str) -> bool:
    """True if the probe response PROVES the version is active, False if it proves it is not.

    Anything that proves neither raises ProbeError, so the planner reports action=error and
    alerts instead of inferring a window from a throttle or an outage.
    """
    if status_code in INACTIVE_STATUSES:
        return False
    if status_code in ACTIVE_STATUSES:
        return True
    if status_code == 401:
        raise ProbeError(f"LinkedIn rejected the probe token (401): {body[:200]}")
    raise ProbeError(f"undecidable probe response {status_code}: {body[:200]}")


def probe_version(token: str, version: str, timeout: int = 20,
                  attempts: int = DEFAULT_ATTEMPTS, sleep=time.sleep) -> bool:
    """Probe one version, retrying transient throttles/outages before giving up."""
    request = urllib.request.Request(PROBE_URL, headers={
        "Authorization": f"Bearer {token}",
        "LinkedIn-Version": version,
        "X-Restli-Protocol-Version": "2.0.0",
    })
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status, body = response.status, ""
        except urllib.error.HTTPError as e:
            status, body = e.code, e.read().decode("utf-8", "replace")
        except urllib.error.URLError as e:
            if attempt < attempts:
                sleep(2 * attempt)
                continue
            raise ProbeError(f"probe of {version} failed to connect: {e}") from e

        if status in RETRYABLE_STATUSES and attempt < attempts:
            sleep(2 * attempt)
            continue
        return is_active_response(status, body)
    raise ProbeError(f"probe of {version} exhausted {attempts} attempts")


def find_active_versions(token: str, candidates: list[str], probe=probe_version) -> list[str]:
    """Probe newest→oldest and stop at the first gap below the live window.

    The live window is contiguous, so once an active version has been seen, the first
    inactive one below it ends the window — no need to probe the whole lookback.
    """
    active: list[str] = []
    for version in candidates:
        if probe(token, version):
            active.append(version)
        elif active:
            break
    return sorted(active)


def decide(current: Optional[str], active: list[str],
           min_headroom: int = DEFAULT_MIN_HEADROOM) -> dict:
    """Plan an action from the pinned version and the live window."""
    if not active:
        return {"action": "error", "target": current,
                "reason": "no active LinkedIn API version found — probe or token is broken"}

    active = sorted(active)
    newest = active[-1]

    if not current:
        return {"action": "urgent-bump", "target": newest,
                "reason": f"no {ENV_KEY} configured; pinning to newest active {newest}"}

    if current not in active:
        return {"action": "urgent-bump", "target": newest,
                "reason": f"{current} is retired (426 NONEXISTENT_VERSION); newest active is {newest}"}

    headroom = active.index(current)  # how many still-live versions are older than the pin
    if headroom < min_headroom:
        return {"action": "bump", "target": newest, "headroom": headroom,
                "reason": (f"{current} is {headroom} version(s) from the oldest live version "
                           f"({active[0]}) — bumping to {newest} before it retires")}

    return {"action": "none", "target": current, "headroom": headroom,
            "reason": f"{current} is active with {headroom} version(s) of headroom"}


def rewrite_env_text(text: str, version: str, key: str = ENV_KEY) -> tuple[str, bool]:
    """Replace a KEY=value line in a .env-style file. Returns (text, changed)."""
    pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
    if not pattern.search(text):
        return text, False
    new_text = pattern.sub(f"{key}={version}", text)
    return new_text, new_text != text


def rewrite_default_text(text: str, version: str, key: str = ENV_KEY) -> tuple[str, bool]:
    """Replace the default_value in the env_constants.py get_constant_from_env(KEY, ...) call."""
    pattern = re.compile(
        rf"(get_constant_from_env\(\s*'{re.escape(key)}'\s*,\s*default_value\s*=\s*')(\d+)(')")
    if not pattern.search(text):
        return text, False
    new_text = pattern.sub(rf"\g<1>{version}\g<3>", text)
    return new_text, new_text != text


def _access_token(user_id: int) -> Optional[str]:
    from cqc_lem.utilities.db import get_user_access_token
    return get_user_access_token(user_id)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan-json", action="store_true", help="probe and emit a JSON plan")
    parser.add_argument("--current", help=f"currently configured {ENV_KEY}")
    parser.add_argument("--token", help="LinkedIn access token (default: read user's from the DB)")
    parser.add_argument("--user-id", type=int, default=1, help="user whose token probes with")
    parser.add_argument("--min-headroom", type=int, default=DEFAULT_MIN_HEADROOM)
    parser.add_argument("--lookback", type=int, default=DEFAULT_LOOKBACK)
    parser.add_argument("--rewrite-repo-defaults", metavar="ROOT",
                        help="update .env.example + env_constants.py in a repo checkout")
    parser.add_argument("--version", help="version to write with --rewrite-repo-defaults")
    args = parser.parse_args(argv)

    if args.rewrite_repo_defaults:
        if not args.version:
            parser.error("--rewrite-repo-defaults requires --version")
        changed = []
        targets = [(f"{args.rewrite_repo_defaults}/.env.example", rewrite_env_text),
                   (f"{args.rewrite_repo_defaults}/src/cqc_lem/utilities/env_constants.py",
                    rewrite_default_text)]
        for path, rewrite in targets:
            try:
                with open(path, encoding="utf-8") as handle:
                    original = handle.read()
            except OSError as e:
                print(f"skip {path}: {e}", file=sys.stderr)
                continue
            updated, did_change = rewrite(original, args.version)
            if did_change:
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(updated)
                changed.append(path)
        print(json.dumps({"changed": changed}))
        return 0

    if not args.plan_json:
        parser.error("nothing to do — pass --plan-json or --rewrite-repo-defaults")

    token = args.token or _access_token(args.user_id)
    if not token:
        print(json.dumps({"action": "error",
                          "reason": f"no LinkedIn access token for user {args.user_id}"}))
        return 1

    candidates = candidate_versions(datetime.now(timezone.utc), lookback=args.lookback)
    try:
        active = find_active_versions(token, candidates)
    except ProbeError as e:
        print(json.dumps({"action": "error", "reason": str(e)}))
        return 1

    plan = decide(args.current, active, min_headroom=args.min_headroom)
    plan["current"] = args.current
    plan["active"] = active
    plan["newest_active"] = active[-1] if active else None
    print(json.dumps(plan))
    return 0


if __name__ == "__main__":
    sys.exit(main())
