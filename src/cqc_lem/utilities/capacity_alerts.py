"""Capacity monitoring for the shared Chrome pool and the Celery Selenium lanes (issue #552).

Phase 1 of `docs/scaling-plan.md` raised `SE_NODE_MAX_SESSIONS` 4 → 8 and the four lane
concurrencies to match (se_engage 3 + se_prepost 2 + se_outreach 2 + se_content 1). Those numbers
fit a ~10-user cohort on this box; nothing told us when the fit stopped holding.
The failure mode of an outgrown cap is not a crash — it is **tasks quietly firing late** (issue #547),
which only ever surfaced as a user complaint. This module is that missing signal.

Structured like `utilities/cost_alerts.py`: PURE evaluators first (no Redis, no network — these are
what the unit tests pin with synthetic breaches), then the sampling/collection layer, delivery, CLI.

| Check | Source | Fires when |
|---|---|---|
| `session_saturation` | `selenium-chrome` `/status` slots | busy slots ≥ X% of the cap on a sustained share of samples |
| `lane_backlog`       | broker queue depth per `se_*` lane | a lane holds ≥ N waiting messages on a sustained share of samples |
| `session_wait`       | driver-acquisition timings         | p95 time to get a Chrome session ≥ N seconds (work is queueing on the cap) |

"Sustained" is deliberate: one saturated sample is healthy utilisation of a pool you paid for, and
alerting on it would train the owner to ignore the alert. A breach on a QUARTER of a multi-day window
means the ceiling — not the workload — is the constraint, which is the thing a human must decide on.

Every check reports `ok`, `alert`, or **`skipped` with a reason**. An unreachable Redis/Grid is
unknown data, never a confident "all clear".

Delivery is a **GitHub issue** (plus the usual log + PostHog event): the decision this raises is
"raise the lanes/cap, or move to a Grid" (`scaling-plan.md` §5a/§5b), which is owner work with a
resource cost, so it belongs in the same queue as every other decision. Exactly one issue is open at
a time — a re-breach comments on it, and only after `CAPACITY_ISSUE_COOLDOWN_DAYS` of quiet, so a
sustained squeeze can't turn into a 96-issues-a-day firehose.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from datetime import datetime, timedelta, timezone
from typing import Mapping, Optional, Sequence

from cqc_lem.utilities.env_constants import (
    CAPACITY_BACKLOG_TASKS,
    CAPACITY_ISSUE_COOLDOWN_DAYS,
    CAPACITY_ISSUE_ENABLED,
    CAPACITY_MIN_SAMPLES,
    CAPACITY_SATURATION_PCT,
    CAPACITY_SUSTAINED_PCT,
    CAPACITY_WAIT_SECONDS,
    CAPACITY_WINDOW_SAMPLES,
    SELENIUM_DEBUG_NODE_HOST,
    SELENIUM_HUB_HOST,
    SELENIUM_HUB_PORT,
)
from cqc_lem.utilities.logger import log_error, log_info, log_warning

# ───────────────────────────── vocabulary ─────────────────────────────

CHECK_SATURATION = "session_saturation"
CHECK_BACKLOG = "lane_backlog"
CHECK_SESSION_WAIT = "session_wait"

STATUS_OK = "ok"
STATUS_ALERT = "alert"
STATUS_SKIPPED = "skipped"

SEVERITY_WARNING = "warning"
SEVERITY_CRITICAL = "critical"

# Rolling windows in Redis. Samples are appended by the beat task; waits by every session that
# actually opened a browser, so their cadences (and therefore their minimums) differ.
SAMPLES_KEY = "lem:capacity:samples"
WAITS_KEY = "lem:capacity:waits"
WAIT_WINDOW_SAMPLES = 500
# Acquisitions needed before a p95 means anything. Not an env knob: it is a property of the
# percentile, not of this deployment.
MIN_WAIT_SAMPLES = 20

# Marks the auto-filed issue so a re-breach finds it instead of opening a second one. Also the
# human-readable title prefix, so the issue list reads correctly.
ISSUE_TITLE_PREFIX = "perf(capacity): Selenium limits hit — review lanes/cap"
ISSUE_LABELS = ("infrastructure", "observability", "needs-human")
PLAN_DOC = "docs/scaling-plan.md"
GUARD_TEST = "tests/unit/app/test_selenium_capacity.py"


def default_thresholds() -> dict:
    """Thresholds from the environment (`CAPACITY_*`). Every caller merges over this, so a test — or
    a CLI run — can override one threshold without restating the rest."""
    return {
        "saturation_pct": CAPACITY_SATURATION_PCT,
        "sustained_pct": CAPACITY_SUSTAINED_PCT,
        "backlog_tasks": CAPACITY_BACKLOG_TASKS,
        "wait_seconds": CAPACITY_WAIT_SECONDS,
        "min_samples": CAPACITY_MIN_SAMPLES,
    }


def _check(check_id: str, status: str, alerts: Optional[Sequence[Mapping]] = None,
           reason: Optional[str] = None, **detail) -> dict:
    return {"check": check_id, "status": status, "alerts": [dict(a) for a in (alerts or [])],
            "reason": reason, **detail}


def _alert(check_id: str, severity: str, title: str, detail: str, value: Optional[float],
           threshold: Optional[float], **extra) -> dict:
    return {"check": check_id, "severity": severity, "title": title, "detail": detail,
            "value": value, "threshold": threshold, **extra}


def _round(value: Optional[float], digits: int = 4) -> Optional[float]:
    return None if value is None else round(float(value), digits)


def _severity(share: float, sustained_pct: float) -> str:
    """A breach on twice the sustained share is not a busy patch — the ceiling IS the operating
    point, and work is late for a large fraction of the window."""
    return SEVERITY_CRITICAL if share >= min(1.0, sustained_pct * 2) else SEVERITY_WARNING


def _percentile(values: Sequence[float], pct: float) -> Optional[float]:
    """Nearest-rank percentile — no interpolation, so the reported number is one that was actually
    measured (a real session wait, not an average of two)."""
    ordered = sorted(float(v) for v in values or [])
    if not ordered:
        return None
    rank = max(1, min(len(ordered), math.ceil(pct * len(ordered))))
    return ordered[rank - 1]


# ───────────────────────────── pure evaluators ─────────────────────────────

def evaluate_session_saturation(samples: Optional[Sequence[Mapping]],
                                saturation_pct: float = CAPACITY_SATURATION_PCT,
                                sustained_pct: float = CAPACITY_SUSTAINED_PCT,
                                min_samples: int = CAPACITY_MIN_SAMPLES) -> dict:
    """Busy Chrome slots against `SE_NODE_MAX_SESSIONS`, over the sample window.

    Samples with no readable cap are dropped rather than counted as 0 busy — a Grid that was down for
    an hour must not read as an hour of idle capacity."""
    usable = [s for s in (samples or [])
              if s and s.get("max_sessions") and s.get("busy_sessions") is not None]
    if len(usable) < max(min_samples, 1):
        return _check(CHECK_SATURATION, STATUS_SKIPPED, samples=len(usable),
                      reason=f"only {len(usable)} readable sample(s) — need {min_samples}")

    ratios = [float(s["busy_sessions"]) / float(s["max_sessions"]) for s in usable]
    breaches = [r for r in ratios if r >= saturation_pct]
    share = len(breaches) / len(ratios)
    cap = max(int(s["max_sessions"]) for s in usable)
    peak = max(int(s["busy_sessions"]) for s in usable)
    detail = {"samples": len(usable), "max_sessions": cap, "peak_busy": peak,
              "breach_share": _round(share), "threshold": sustained_pct,
              "saturation_pct": saturation_pct}
    if share < sustained_pct:
        return _check(CHECK_SATURATION, STATUS_OK, **detail)
    return _check(CHECK_SATURATION, STATUS_ALERT, [_alert(
        CHECK_SATURATION, _severity(share, sustained_pct),
        f"Chrome pool was ≥{saturation_pct * 100:.0f}% claimed in {share * 100:.0f}% of samples",
        f"{len(breaches)} of {len(ratios)} samples had ≥{saturation_pct * 100:.0f}% of the "
        f"{cap} session slot(s) busy (peak {peak}/{cap}). Work that needs a browser is waiting on "
        f"the cap, not on the host.",
        _round(share), sustained_pct, max_sessions=cap, peak_busy=peak,
    )], **detail)


def evaluate_lane_backlog(samples: Optional[Sequence[Mapping]],
                          backlog_tasks: int = CAPACITY_BACKLOG_TASKS,
                          sustained_pct: float = CAPACITY_SUSTAINED_PCT,
                          min_samples: int = CAPACITY_MIN_SAMPLES) -> dict:
    """Per-lane queue depth — messages sitting in the broker because no lane slot is free.

    One alert per breaching lane: the fix is per-lane (`SELENIUM_CONCURRENCY` on THAT worker, plus
    the cap in lockstep), so the alert has to say which one."""
    usable = [dict(s.get("queues") or {}) for s in (samples or []) if s and s.get("queues")]
    if len(usable) < max(min_samples, 1):
        return _check(CHECK_BACKLOG, STATUS_SKIPPED, samples=len(usable),
                      reason=f"only {len(usable)} readable sample(s) — need {min_samples}")

    lanes = sorted({lane for sample in usable for lane in sample})
    alerts, depths = [], {}
    for lane in lanes:
        observed = [int(s.get(lane) or 0) for s in usable if lane in s]
        if not observed:
            continue
        share = sum(1 for d in observed if d >= backlog_tasks) / len(observed)
        depths[lane] = {"peak": max(observed), "breach_share": _round(share)}
        if share < sustained_pct:
            continue
        alerts.append(_alert(
            CHECK_BACKLOG, _severity(share, sustained_pct),
            f"Lane `{lane}` had ≥{backlog_tasks} task(s) waiting in {share * 100:.0f}% of samples",
            f"Peak depth {max(observed)} message(s) queued behind that lane's concurrency. Tasks on "
            f"`{lane}` start late by roughly (depth ÷ concurrency) × task duration.",
            _round(share), sustained_pct, lane=lane, peak_depth=max(observed),
        ))
    return _check(CHECK_BACKLOG, STATUS_ALERT if alerts else STATUS_OK, alerts,
                  samples=len(usable), lanes=depths, backlog_tasks=backlog_tasks,
                  threshold=sustained_pct)


def evaluate_session_wait(waits: Optional[Sequence[float]],
                          wait_seconds: float = CAPACITY_WAIT_SECONDS,
                          min_samples: int = MIN_WAIT_SAMPLES) -> dict:
    """How long a task waited for a Chrome session (`selenium_util.get_docker_driver`).

    The most direct evidence that the cap is the binding constraint: a free slot answers in seconds,
    a full pool blocks the caller until one frees up. Judged on p95 rather than a share of breaches
    because the tail IS the user-visible symptom — the one task that missed its window."""
    values = [float(w) for w in (waits or []) if w is not None and float(w) >= 0]
    if len(values) < max(min_samples, 1):
        return _check(CHECK_SESSION_WAIT, STATUS_SKIPPED, samples=len(values),
                      reason=f"only {len(values)} session acquisition(s) — need {min_samples}")

    p95 = _percentile(values, 0.95)
    median = _percentile(values, 0.5)
    detail = {"samples": len(values), "p95_seconds": _round(p95, 2),
              "median_seconds": _round(median, 2), "threshold": wait_seconds}
    if p95 < wait_seconds:
        return _check(CHECK_SESSION_WAIT, STATUS_OK, **detail)
    return _check(CHECK_SESSION_WAIT, STATUS_ALERT, [_alert(
        CHECK_SESSION_WAIT, SEVERITY_CRITICAL if p95 >= wait_seconds * 2 else SEVERITY_WARNING,
        f"p95 wait for a Chrome session is {p95:.0f}s",
        f"Over {len(values)} session(s): median {median:.0f}s, p95 {p95:.0f}s against a "
        f"{wait_seconds:.0f}s budget. A free slot answers in seconds — this is queueing on the cap.",
        _round(p95, 2), wait_seconds, median_seconds=_round(median, 2),
    )], **detail)


# ───────────────────────────── pure report builder ─────────────────────────────

def build_capacity_report(samples: Optional[Sequence[Mapping]], waits: Optional[Sequence[float]],
                          thresholds: Optional[Mapping] = None,
                          window_hours: Optional[float] = None,
                          now: Optional[datetime] = None) -> dict:
    """Run all three checks over one window and collect them into a report.

    `samples` is None (not []) when Redis was unreadable; both mean "no measurements", and every
    check reports itself skipped rather than passing."""
    limits = {**default_thresholds(), **(thresholds or {})}
    checks = [
        evaluate_session_saturation(samples, limits["saturation_pct"], limits["sustained_pct"],
                                    limits["min_samples"]),
        evaluate_lane_backlog(samples, limits["backlog_tasks"], limits["sustained_pct"],
                              limits["min_samples"]),
        evaluate_session_wait(waits, limits["wait_seconds"]),
    ]
    alerts = [alert for check in checks for alert in check["alerts"]]
    stamp = (now or datetime.now(timezone.utc)).replace(microsecond=0)
    # Samples come back newest-first, so the first one carrying a host reading is the current one.
    host = next((s.get("host") for s in (samples or []) if s and s.get("host")), None)
    return {
        "generated_at": stamp.isoformat(),
        "host": host,
        "date": stamp.date().isoformat(),
        "window_hours": window_hours,
        "sample_count": len(samples or []),
        "thresholds": limits,
        "checks": checks,
        "alerts": alerts,
        "alert_count": len(alerts),
        "critical_count": sum(1 for a in alerts if a.get("severity") == SEVERITY_CRITICAL),
        "skipped_checks": [c["check"] for c in checks if c["status"] == STATUS_SKIPPED],
    }


def render_capacity_text(report: Mapping) -> str:
    """Plain-text digest — the log/CLI rendering, and the body of the GitHub comment on re-breach."""
    window = report.get("window_hours")
    lines = [f"LEM capacity check — {report.get('generated_at')} "
             f"({report.get('alert_count', 0)} alert(s), "
             f"{report.get('critical_count', 0)} critical over "
             f"{report.get('sample_count', 0)} sample(s)"
             f"{f', ~{window:g}h window' if window else ''})", ""]
    for alert in report.get("alerts") or []:
        lines.append(f"[{str(alert.get('severity', '')).upper()}] {alert.get('title')}")
        lines.append(f"    {alert.get('detail')}")
    if not report.get("alerts"):
        lines.append("No capacity thresholds breached.")
    host = report.get("host")
    if host:
        lines += ["", f"Host: load {host.get('load1')} on {host.get('cpus')} core(s), "
                      f"{host.get('mem_available_gb')} GB RAM available."]
    lines += ["", "Checks:"]
    for check in report.get("checks") or []:
        reason = f" — {check.get('reason')}" if check.get("reason") else ""
        lines.append(f"  {check.get('check')}: {check.get('status')}{reason}")
    return "\n".join(lines)


def _breaching_lanes(report: Mapping) -> list:
    return [a.get("lane") for a in (report.get("alerts") or [])
            if a.get("check") == CHECK_BACKLOG and a.get("lane")]


def render_capacity_issue_body(report: Mapping) -> str:
    """The GitHub issue body: what was measured, and the letter-pickable call the owner has to make.

    Deliberately NOT a fix proposal the pipeline can just apply — raising the cap spends real RAM on
    a shared box, which §5a/§5c say is a human decision. It gives the numbers, the invariant that
    must survive the change, and the two real options."""
    lanes = _breaching_lanes(report)
    lane_hint = (f"`{lanes[0]}`" if len(lanes) == 1
                 else ", ".join(f"`{lane}`" for lane in lanes) if lanes
                 else "the saturated lane")
    saturation = next((c for c in report.get("checks") or []
                       if c.get("check") == CHECK_SATURATION), {})
    cap = saturation.get("max_sessions")
    cap_line = f"{cap}" if cap else "the configured cap"
    return "\n".join([
        f"Auto-filed by the capacity monitor (`auto_capacity_watch`, issue #552). "
        + f"Measured over {report.get('sample_count', 0)} sample(s)"
        + (f" (~{report['window_hours']:g}h)" if report.get("window_hours") else "") + ".",
        "",
        "## What tripped",
        "",
        "```",
        render_capacity_text(report),
        "```",
        "",
        "## Why this needs you",
        "",
        f"The browser pool is a fixed {cap_line} Chrome slot(s) shared by every Selenium lane. When "
        + "it stays claimed, nothing fails — time-sensitive work (pre-post commenting, golden-hour "
        + "loops) just fires **late**. Raising it spends real RAM/CPU on a shared VPS, so it is a "
        + f"resource call, not a code fix ({PLAN_DOC} §5a/§5c).",
        "",
        "**Invariant any change must preserve:** `SE_NODE_MAX_SESSIONS` == the sum of every lane's "
        + "`SELENIUM_CONCURRENCY`, with `shm_size` ≥ cap, `cpus` ≥ cap and `memory` ≤ 8g. "
        + f"`{GUARD_TEST}` fails the build if they drift.",
        "",
        "## Decision — reply with option letters",
        "",
        "### 1. How do we add capacity?",
        f"- **A. Raise {lane_hint} by 1 and `SE_NODE_MAX_SESSIONS` in lockstep** — ~1–1.5 GB more "
        + "Chrome per slot; stays inside the 8g budget up to ~6–8 sessions"
        + (f" (host had {report['host'].get('mem_available_gb')} GB available at the time)"
           if report.get("host") else "") + ".  ✅ *recommended*",
        f"- **B. Move Chrome to a Selenium Grid (hub + N nodes)** — {PLAN_DOC} §5b Phase 2; "
        + "sessions scale horizontally (a 2nd box), but it is new infra to run.",
        "- **C. Do nothing / retune the thresholds** — accept the lateness, or raise the "
        + "`CAPACITY_*` thresholds if this reads as normal utilisation.",
        "",
        "**My recommendation: `1A`** while the cap is still under the ~6–8 session Chrome budget; "
        + "`1B` once it isn't.",
        "",
        "_Auto-filed by `utilities/capacity_alerts.py`; one issue at a time, re-breaches comment "
        + f"here after a {CAPACITY_ISSUE_COOLDOWN_DAYS}-day cooldown. Close it once the lanes are "
        + "reviewed._",
    ])


# ─────────────────────── sampling / collection ───────────────────────

def _redis():
    from cqc_lem.utilities.linkedin.rate_limit import shared_redis_client
    return shared_redis_client()


def selenium_lane_queues() -> tuple:
    """The `se_*` lanes, straight from celeryconfig so a new lane is monitored the day it lands."""
    from cqc_lem.utilities.maintenance import queue_names
    return tuple(q for q in queue_names() if q.startswith("se_"))


def _pool_slots(nodes: Sequence[Mapping], debug_host: str = SELENIUM_DEBUG_NODE_HOST) -> list:
    """The slots the LANES are sized for — i.e. every slot except the debug node's.

    The Grid overlay runs one node the lanes never size for (the watchable `selenium-node-debug`,
    on top of the pool). Counting its slot here would be a silent DESENSITISATION of the whole
    check: with 8 lane slots and a 9-slot denominator a fully-claimed pool reads 0.89, and the
    sample below the peak reads 7/9 = 0.78 — under the 0.85 saturation threshold that used to
    breach at 7/8. The signal exists to say "the cap is the constraint, buy the second box", so it
    has to be measured against the cap the lanes can actually consume.

    Matching is on the node's advertised URI host (the overlay pins it with `SE_NODE_HOST`). A Grid
    that reports no such node — the standalone, or a box that never deployed the debug node — is
    unchanged: nothing matches and every slot counts."""
    keep = []
    for node in nodes:
        uri = str(node.get("uri") or "")
        if debug_host and f"//{debug_host}:" in uri:
            continue
        keep.extend(node.get("slots") or [])
    return keep


def collect_selenium_capacity() -> Optional[dict]:
    """Busy vs total Chrome slots from the Grid `/status` endpoint. None when it is unreachable —
    which the checks report as missing samples, never as an idle pool."""
    import requests

    url = f"http://{SELENIUM_HUB_HOST}:{SELENIUM_HUB_PORT}/status"
    try:
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        nodes = (response.json().get("value") or {}).get("nodes") or []
    except Exception as e:
        log_warning("Selenium /status unreadable — capacity sample skipped", exc=e,
                    task_name="auto_capacity_watch")
        return None
    slots = _pool_slots(nodes)
    if not slots:
        return None
    return {"max_sessions": len(slots),
            "busy_sessions": sum(1 for slot in slots if slot.get("session"))}


def collect_lane_depths() -> Optional[dict]:
    """Messages waiting in the broker per Selenium lane. This counts what has NOT yet been reserved
    by a worker — exactly the backlog a lane's concurrency is failing to absorb."""
    client = _redis()
    if client is None:
        return None
    depths = {}
    for lane in selenium_lane_queues():
        try:
            depths[lane] = int(client.llen(lane) or 0)
        except Exception as e:
            log_warning(f"Could not read queue depth for {lane}", exc=e,
                        task_name="auto_capacity_watch")
    return depths or None


def collect_host_headroom() -> Optional[dict]:
    """Load average + available RAM straight from `/proc` — no new dependency.

    CONTEXT, never an alert: the host has never been the constraint (§1), but whether "raise the cap"
    is even a safe answer depends on the RAM left, so the review issue has to carry it."""
    try:
        with open("/proc/loadavg") as handle:
            load1 = float(handle.read().split()[0])
        available_kb = 0
        with open("/proc/meminfo") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    available_kb = int(line.split()[1])
                    break
    except Exception:
        return None
    return {"load1": round(load1, 2), "cpus": os.cpu_count(),
            "mem_available_gb": round(available_kb / (1024 * 1024), 1)}


def record_sample(now: Optional[datetime] = None) -> Optional[dict]:
    """Take one capacity sample and append it to the rolling window in Redis (trimmed to
    `CAPACITY_WINDOW_SAMPLES`). Returns the sample, or None when nothing could be read/stored."""
    capacity = collect_selenium_capacity()
    queues = collect_lane_depths()
    if capacity is None and queues is None:
        return None
    stamp = (now or datetime.now(timezone.utc)).replace(microsecond=0)
    sample = {"ts": stamp.isoformat(), **(capacity or {}), "queues": queues or {},
              "host": collect_host_headroom()}
    client = _redis()
    if client is None:
        return sample
    try:
        client.lpush(SAMPLES_KEY, json.dumps(sample))
        client.ltrim(SAMPLES_KEY, 0, max(CAPACITY_WINDOW_SAMPLES, 1) - 1)
    except Exception as e:
        log_warning("Could not store capacity sample", exc=e, task_name="auto_capacity_watch")
    return sample


def record_session_wait(seconds: float) -> None:
    """Record how long ONE `get_docker_driver()` call waited for its Chrome session. Called from the
    Selenium hot path, so it never raises and never blocks on a slow Redis (2s socket timeout)."""
    try:
        client = _redis()
        if client is None:
            return
        client.lpush(WAITS_KEY, json.dumps(
            {"ts": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
             "seconds": round(float(seconds), 2)}))
        client.ltrim(WAITS_KEY, 0, WAIT_WINDOW_SAMPLES - 1)
    except Exception:
        # Instrumentation must never cost a browser session.
        pass


def _load_json_list(key: str, limit: int) -> Optional[list]:
    client = _redis()
    if client is None:
        return None
    try:
        raw = client.lrange(key, 0, max(limit, 1) - 1)
    except Exception as e:
        log_warning(f"Could not read {key} from Redis", exc=e, task_name="auto_capacity_watch")
        return None
    rows = []
    for item in raw or []:
        try:
            rows.append(json.loads(item.decode() if isinstance(item, bytes) else item))
        except Exception:
            continue
    return rows


def load_samples(limit: int = CAPACITY_WINDOW_SAMPLES) -> Optional[list]:
    return _load_json_list(SAMPLES_KEY, limit)


def load_waits(limit: int = WAIT_WINDOW_SAMPLES) -> Optional[list]:
    rows = _load_json_list(WAITS_KEY, limit)
    return None if rows is None else [row.get("seconds") for row in rows
                                      if row.get("seconds") is not None]


def _window_hours(samples: Optional[Sequence[Mapping]]) -> Optional[float]:
    stamps = sorted(str(s.get("ts")) for s in (samples or []) if s and s.get("ts"))
    if len(stamps) < 2:
        return None
    try:
        span = datetime.fromisoformat(stamps[-1]) - datetime.fromisoformat(stamps[0])
    except ValueError:
        return None
    return round(span.total_seconds() / 3600.0, 1)


def collect_capacity_report(thresholds: Optional[Mapping] = None,
                            sample_now: bool = True) -> dict:
    """Take a fresh sample (unless the caller only wants to read history) and evaluate the window."""
    if sample_now:
        record_sample()
    samples = load_samples()
    return build_capacity_report(samples, load_waits(), thresholds=thresholds,
                                 window_hours=_window_hours(samples))


# ─────────────────────── GitHub delivery ───────────────────────

def _cooldown_cutoff(now: Optional[datetime] = None) -> datetime:
    return (now or datetime.now(timezone.utc)) - timedelta(days=max(CAPACITY_ISSUE_COOLDOWN_DAYS, 0))


ISSUE_LOOKUP_PAGES = 3


def find_open_capacity_issue(max_pages: int = ISSUE_LOOKUP_PAGES) -> Optional[dict]:
    """The already-open auto-filed capacity issue, if any.

    Matched on the TITLE marker and deliberately NOT filtered by label: a fine-grained PAT silently
    drops labels from the create payload (issue #598), and a lookup that can't find the issue it just
    filed would open a fresh one every 15 minutes. An issue a human renamed is treated as new —
    better a second issue than a silent alert."""
    from cqc_lem.utilities.feedback.issue_service import github_request

    for page in range(1, max(max_pages, 1) + 1):
        data = github_request("GET", f"issues?state=open&per_page=100&page={page}")
        if not isinstance(data, list) or not data:
            return None
        for issue in data:
            if isinstance(issue, dict) and str(issue.get("title", "")).startswith(ISSUE_TITLE_PREFIX):
                return issue
        if len(data) < 100:
            return None
    return None


def _apply_issue_labels(number: int) -> None:
    """Re-apply the labels after creation. A fine-grained PAT drops `labels` in the CREATE payload
    (issue #598) — and an unlabelled capacity issue never reaches the `needs-human` queue. Idempotent,
    so it stays harmless once #598 fixes this at the source."""
    from cqc_lem.utilities.feedback.issue_service import github_request

    github_request("POST", f"issues/{int(number)}/labels", {"labels": list(ISSUE_LABELS)})


def _updated_within_cooldown(issue: Mapping, now: Optional[datetime] = None) -> bool:
    stamp = str(issue.get("updated_at") or "")
    if not stamp:
        return False
    try:
        updated = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return False
    return updated > _cooldown_cutoff(now)


def file_capacity_issue(report: Mapping, now: Optional[datetime] = None) -> dict:
    """Open ONE issue for a sustained breach, or comment on the existing one after the cooldown.

    The cooldown is read off the issue's own `updated_at` rather than local state, so it holds across
    deploys and container restarts (and a human's comment counts as recent activity — they are
    already looking at it)."""
    from cqc_lem.utilities.feedback.issue_service import (
        comment_on_issue, create_github_issue, github_token,
    )

    if not report.get("alerts"):
        return {"action": "none", "reason": "no alerts"}
    if not CAPACITY_ISSUE_ENABLED:
        return {"action": "disabled", "reason": "CAPACITY_ISSUE_ENABLED is off"}
    if not github_token():
        log_warning("Capacity alert not filed — no FEEDBACK_GITHUB_TOKEN/GITHUB_TOKEN set",
                    api_provider="github", task_name="auto_capacity_watch")
        return {"action": "skipped", "reason": "no GitHub token"}

    existing = find_open_capacity_issue()
    if existing:
        number = int(existing.get("number") or 0)
        if _updated_within_cooldown(existing, now):
            return {"action": "cooldown", "issue": number}
        commented = comment_on_issue(number, "Still breaching — latest capacity check:\n\n```\n"
                                             f"{render_capacity_text(report)}\n```")
        return {"action": "commented" if commented else "failed", "issue": number}

    title = f"{ISSUE_TITLE_PREFIX} ({report.get('date')})"
    number = create_github_issue(title, render_capacity_issue_body(report), list(ISSUE_LABELS))
    if number is None:
        return {"action": "failed", "reason": "GitHub issue creation failed"}
    _apply_issue_labels(number)
    log_info(f"Filed capacity review issue #{number}", api_provider="github",
             task_name="auto_capacity_watch")
    return {"action": "filed", "issue": number}


def send_capacity_alerts(report: Optional[Mapping] = None) -> dict:
    """Deliver breaches: a structured log line each (`log_error` for critical — the logger forwards
    those to PostHog), one `capacity_alert` PostHog event each, and the GitHub issue. Delivery
    failures are logged, never raised: a beat run must not die over a GitHub hiccup."""
    from cqc_lem.utilities.observability import track_capacity_alert

    report = report if report is not None else collect_capacity_report()
    alerts = list(report.get("alerts") or [])

    for alert in alerts:
        message = f"Capacity alert [{alert.get('check')}]: {alert.get('title')}"
        if alert.get("severity") == SEVERITY_CRITICAL:
            log_error(message, task_name="auto_capacity_watch")
        else:
            log_warning(message, task_name="auto_capacity_watch")

    tracked = False
    try:
        for alert in alerts:
            track_capacity_alert(alert, generated_at=report.get("generated_at"))
        tracked = True
    except Exception as e:
        log_warning("Capacity alert PostHog capture failed", exc=e,
                    task_name="auto_capacity_watch")

    try:
        filed = file_capacity_issue(report)
    except Exception as e:
        log_error("Capacity alert GitHub filing failed", exc=e, api_provider="github",
                  task_name="auto_capacity_watch")
        filed = {"action": "failed", "reason": str(e)}

    log_info(f"Capacity check {report.get('generated_at')}: {len(alerts)} alert(s), "
             f"{report.get('critical_count', 0)} critical over {report.get('sample_count', 0)} "
             f"sample(s), skipped {report.get('skipped_checks') or []} "
             f"(github={filed.get('action')}, tracked={tracked})",
             task_name="auto_capacity_watch")
    return {"alerts": len(alerts), "tracked": tracked, "github": filed, "report": report}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="LEM Selenium/lane capacity monitor")
    parser.add_argument("--json", action="store_true", help="print the full report as JSON")
    parser.add_argument("--sample", action="store_true",
                        help="take a fresh sample before evaluating (the beat task always does)")
    parser.add_argument("--deliver", action="store_true",
                        help="deliver breaches (log + PostHog + GitHub issue)")
    args = parser.parse_args(argv)

    report = collect_capacity_report(sample_now=args.sample)
    if args.deliver:
        send_capacity_alerts(report)
    print(json.dumps(report) if args.json else render_capacity_text(report))
    # Exit 2 on a breach so a cron/CI caller can act on it without parsing the output.
    return 2 if report.get("alert_count") else 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
