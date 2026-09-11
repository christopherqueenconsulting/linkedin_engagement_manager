"""Grade every stored outbound body against the send-path gate (issue #1968).

#1964 put `outbound_violations` in front of `send_dm_now`, so a drafted body that reads as an
assistant aside is refused at send time. What it could not do is count the ones already sitting in
the approval queues: those never reached the `Sending DM:` log line the incident was traced from,
so the incident's true size is "3 sent, plus an unknown number drafted". This script reads that
number — every `scheduled_dms.message` and `connection_requests.message` row, through the SAME
predicate the gate uses, so the scan and the gate cannot disagree.

Two surfaces: a `scheduled_dms` row is graded as `SURFACE_DM`; a `connection_requests.message`
is the invite note, which `outbound_qa` already knows as `SURFACE_INVITE_NOTE` (same checks, same
900-char budget as a DM). An invite note is NULLABLE and OPTIONAL — a bare invite has nothing to
grade — so a missing note is counted apart and never reads as a tripped row.

READ-ONLY BY DEFAULT. `--cancel` is the one write, and it is never a side effect of running the
report (`docs/` rule against a gated action riding along with a permitted one): it moves a tripped
row to `canceled` ONLY when the row is still in a live queue state (`pending` / `approved` /
`scheduled` for a DM; `pending` / `approved` for a connection request). `sent` / `failed` /
`sending` rows are the historical record and are never touched, with or without the flag.

An unreadable table is REFUSED (exit 2), never reported as zero: the readers return None on a DB
fault precisely so an empty audit cannot be mistaken for a clean one.

Runs from STDIN inside the production image — nothing here is path-relative, only `cqc_lem.*`
(installed in the image) and the standard library are imported — so the operator invokes it as:

    sudo docker exec -i celery_worker python - < scripts/audit_outbound_drafts.py
    sudo docker exec -i celery_worker python - --json < scripts/audit_outbound_drafts.py
    sudo docker exec -i celery_worker python - --cancel < scripts/audit_outbound_drafts.py

From a checkout: `PYTHONPATH=src poetry run python scripts/audit_outbound_drafts.py`. Run it
against `mysql_db` (the production container the worker is wired to), never `lem-it-mysql`.

Bodies are outbound messages to real people, so the report never prints one in full — only a
whitespace-flattened, URL/email-masked excerpt of the first 80 characters per tripped row.
"""

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

from cqc_lem.platform.db.enums import ConnectionRequestStatus, ScheduledDmStatus
from cqc_lem.utilities.ai.outbound_qa import SURFACE_DM, SURFACE_INVITE_NOTE, outbound_violations

TABLE_SCHEDULED_DMS = "scheduled_dms"
TABLE_CONNECTION_REQUESTS = "connection_requests"
TABLES = (TABLE_SCHEDULED_DMS, TABLE_CONNECTION_REQUESTS)

# The gate's own surface names. A connection request's `message` is the invite note, and the gate
# has a surface for exactly that — the DM surface would grade it identically today (same checks,
# same ceiling) but would misname it in every report line.
SURFACE_BY_TABLE = {
    TABLE_SCHEDULED_DMS: SURFACE_DM,
    TABLE_CONNECTION_REQUESTS: SURFACE_INVITE_NOTE,
}

# Where a row still counts as queued — the ONLY states `--cancel` may move. `scheduled` on a DM
# means the scanner has dispatched the send task, and `send_scheduled_dm` re-reads the row's status
# before typing, so cancelling it is honoured. `sending` on a connection request is excluded on
# purpose: the dispatch is in flight and its outcome will be written by the task, not by an audit.
LIVE_STATUSES = {
    TABLE_SCHEDULED_DMS: frozenset({
        ScheduledDmStatus.PENDING.value, ScheduledDmStatus.APPROVED.value,
        ScheduledDmStatus.SCHEDULED.value}),
    TABLE_CONNECTION_REQUESTS: frozenset({
        ConnectionRequestStatus.PENDING.value, ConnectionRequestStatus.APPROVED.value}),
}

EXCERPT_CHARS = 80
NULL_LABEL = "(null)"
CANCEL_REASON_PREFIX = "outbound_qa audit (#1968): "

EXIT_OK = 0
EXIT_CANCEL_FAILED = 1
EXIT_UNREADABLE = 2

_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True)
class Finding:
    """One stored body the gate would refuse — the row's coordinates, never its full text."""

    table: str
    row_id: int
    user_id: Optional[int]
    status: str
    source: str
    checks: tuple
    excerpt: str

    @property
    def live(self) -> bool:
        """True when the row is still queued, i.e. a state `--cancel` is allowed to move."""
        return self.status in LIVE_STATUSES[self.table]


@dataclass
class TableScan:
    """What one table's pass produced: how many rows were read, and which of them tripped."""

    table: str
    scanned: int = 0
    no_note: int = 0
    findings: list = field(default_factory=list)


@dataclass
class AuditIO:
    """Every side effect the script performs, as injectable callables.

    Readers return a list of dict rows, or None when the read FAILED. Writers return True when
    the status write ran. `production_io` binds these to the db facade; tests hand in fakes.
    """

    read_scheduled_dms: Callable[[], Optional[list]]
    read_connection_requests: Callable[[], Optional[list]]
    cancel_scheduled_dm: Callable[[int], bool]
    cancel_connection_request: Callable[[int, str], bool]


def production_io(facade=None) -> AuditIO:
    """Bind the audit to the real db facade (`cqc_lem.utilities.db`).

    The cancel writers go through the existing status updaters with the proper enum, so the
    audit writes exactly what the SPA's own Cancel button writes — a connection request also
    records WHY in `failure_reason`, since that column is what the queue view shows next to
    a status.

    Args:
        facade: The module holding the readers and writers; defaults to `cqc_lem.utilities.db`.
            Injectable so a test can prove the enum binding without patching the facade.
    """
    if facade is None:
        # Imported late so `--help` and the pure planning half never touch DB configuration.
        from cqc_lem.utilities import db as facade

    def cancel_dm(dm_id: int) -> bool:
        return bool(facade.update_scheduled_dm_status(dm_id, ScheduledDmStatus.CANCELED))

    def cancel_request(request_id: int, reason: str) -> bool:
        return bool(facade.update_connection_request_status(
            request_id, ConnectionRequestStatus.CANCELED, failure_reason=reason))

    return AuditIO(
        read_scheduled_dms=facade.list_scheduled_dms_for_audit,
        read_connection_requests=facade.list_connection_requests_for_audit,
        cancel_scheduled_dm=cancel_dm,
        cancel_connection_request=cancel_request,
    )


def sanitise_excerpt(text: Optional[str], limit: int = EXCERPT_CHARS) -> str:
    """The first `limit` characters of a body, flattened and with URLs / emails masked.

    This is the only part of a body the report ever shows. It exists to let the operator
    recognise WHICH failure produced the row ("To assist you effectively, I need…"), not to
    reproduce the message, so anything that could identify the recipient is masked first.
    """
    flat = _CONTROL_RE.sub(" ", str(text or ""))
    flat = re.sub(r"\s+", " ", flat).strip()
    flat = _URL_RE.sub("<url>", flat)
    flat = _EMAIL_RE.sub("<email>", flat)
    if len(flat) <= limit:
        return flat
    return flat[:limit].rstrip() + "…"


def _label(value) -> str:
    """A NULL column as a stable report key, so a hand-written DM (source NULL) groups on its own."""
    return NULL_LABEL if value is None or str(value) == "" else str(value)


def scan_table(table: str, rows: Sequence[dict]) -> TableScan:
    """Grade every row of one table with the gate's predicate on that table's surface.

    A connection request with no note (`message` NULL or blank) is counted under `no_note` and
    not graded — an invite without a note is a normal invite, and grading "" would file every
    one of them as `empty_body`. A `scheduled_dms.message` is NOT NULL by schema, so an empty
    one there IS a defect and is graded as such.
    """
    if table not in SURFACE_BY_TABLE:
        raise ValueError(f"unknown table {table!r}; expected one of {TABLES}")
    surface = SURFACE_BY_TABLE[table]
    scan = TableScan(table=table)
    for row in rows:
        scan.scanned += 1
        message = row.get("message")
        if table == TABLE_CONNECTION_REQUESTS and not str(message or "").strip():
            scan.no_note += 1
            continue
        checks = outbound_violations(message, surface=surface)
        if not checks:
            continue
        scan.findings.append(Finding(
            table=table,
            row_id=int(row["id"]),
            user_id=row.get("user_id"),
            status=str(row.get("status") or "").lower(),
            source=_label(row.get("source")),
            checks=tuple(checks),
            excerpt=sanitise_excerpt(message),
        ))
    return scan


def build_report(scans: Sequence[TableScan]) -> dict:
    """The report as plain data: totals, per-table breakdowns by status / source / check, rows.

    Every count is derived here from the scans so the text and JSON renderings can never
    disagree with each other.
    """
    by_table = {}
    check_total: Counter = Counter()
    rows = []
    for scan in scans:
        by_status: Counter = Counter()
        by_source: Counter = Counter()
        by_check: Counter = Counter()
        live = 0
        for finding in scan.findings:
            by_status[finding.status] += 1
            by_source[finding.source] += 1
            for check in finding.checks:
                by_check[check] += 1
                check_total[check] += 1
            live += int(finding.live)
            rows.append({
                "table": finding.table, "id": finding.row_id, "user_id": finding.user_id,
                "status": finding.status, "source": finding.source,
                "checks": list(finding.checks), "excerpt": finding.excerpt, "live": finding.live,
            })
        by_table[scan.table] = {
            "surface": SURFACE_BY_TABLE[scan.table],
            "scanned": scan.scanned,
            "no_note": scan.no_note,
            "tripped": len(scan.findings),
            "live": live,
            "by_status": dict(sorted(by_status.items())),
            "by_source": dict(sorted(by_source.items())),
            "by_check": dict(sorted(by_check.items())),
        }
    return {
        "scanned": sum(s.scanned for s in scans),
        "tripped": sum(len(s.findings) for s in scans),
        "live": sum(t["live"] for t in by_table.values()),
        "by_check": dict(sorted(check_total.items())),
        "by_table": by_table,
        "rows": rows,
    }


def plan_cancellations(scans: Sequence[TableScan]) -> list:
    """The findings `--cancel` is allowed to move: tripped AND still in a live queue state.

    Pure: this decides, `apply_cancellations` acts. A `sent` / `failed` / `sending` finding is
    never in the plan, so the writer cannot be handed one.
    """
    return [f for scan in scans for f in scan.findings if f.live]


def cancel_reason(finding: Finding) -> str:
    """The `failure_reason` a cancelled connection request keeps, naming the checks that fired."""
    return CANCEL_REASON_PREFIX + ", ".join(finding.checks)


def apply_cancellations(plan: Sequence[Finding], io: AuditIO) -> list:
    """Run the planned status writes, one per finding, and report each outcome.

    Returns a list of `{table, id, ok}` in plan order. A refused write is reported, not raised,
    so one bad row never hides the rest of the run.
    """
    results = []
    for finding in plan:
        if finding.table == TABLE_SCHEDULED_DMS:
            ok = io.cancel_scheduled_dm(finding.row_id)
        else:
            ok = io.cancel_connection_request(finding.row_id, cancel_reason(finding))
        results.append({"table": finding.table, "id": finding.row_id, "ok": bool(ok)})
    return results


def render_text(report: dict, plan: Sequence[Finding], cancel_results: Optional[list]) -> str:
    """The human rendering of `build_report`'s data, plus what `--cancel` did or would do."""
    lines = [
        "Outbound-draft audit (issue #1968) — predicate: cqc_lem.utilities.ai.outbound_qa"
        ".outbound_violations",
        f"scanned: {report['scanned']}   tripped: {report['tripped']}   "
        f"still in a live queue state: {report['live']}",
    ]
    for table, stats in report["by_table"].items():
        lines.append("")
        lines.append(f"[{table}] surface={stats['surface']} scanned={stats['scanned']} "
                     f"tripped={stats['tripped']} live={stats['live']}"
                     + (f" no_note={stats['no_note']}" if stats["no_note"] else ""))
        for name in ("by_status", "by_source", "by_check"):
            if stats[name]:
                lines.append(f"  {name}: " + ", ".join(f"{k}={v}" for k, v in stats[name].items()))
    if report["by_check"]:
        lines.append("")
        lines.append("checks fired (all tables): "
                     + ", ".join(f"{k}={v}" for k, v in report["by_check"].items()))
    if report["rows"]:
        lines.append("")
        lines.append("tripped rows (excerpt is the first 80 chars, flattened, URLs/emails masked):")
        for row in report["rows"]:
            lines.append(f"  {row['table']}#{row['id']} user={row['user_id']} status={row['status']} "
                         f"source={row['source']} checks={','.join(row['checks'])} "
                         f"live={'yes' if row['live'] else 'no'} :: {row['excerpt']!r}")
    lines.append("")
    if cancel_results is None:
        lines.append(f"read-only run: {len(plan)} row(s) would be cancelled by --cancel; "
                     "nothing was written.")
    else:
        done = sum(1 for r in cancel_results if r["ok"])
        failed = [r for r in cancel_results if not r["ok"]]
        lines.append(f"--cancel: {done}/{len(cancel_results)} row(s) moved to canceled.")
        for r in failed:
            lines.append(f"  FAILED to cancel {r['table']}#{r['id']}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    """The CLI. `prog` is pinned because `python -` reports argv[0] as '-'."""
    parser = argparse.ArgumentParser(
        prog="audit_outbound_drafts.py",
        description="Scan scheduled_dms and connection_requests for bodies the outbound gate "
                    "would refuse. Read-only unless --cancel is given.")
    parser.add_argument("--cancel", action="store_true",
                        help="Move tripped rows that are still queued (pending/approved/"
                             "scheduled DMs; pending/approved connection requests) to "
                             "'canceled'. sent/failed/sending rows are never touched.")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="Emit the report (and any cancel results) as JSON on stdout.")
    return parser


def run(args: argparse.Namespace, io: AuditIO, out=None) -> int:
    """Read, grade, report, and — only under `--cancel` — write.

    Returns the process exit code: 2 when either table could not be read (nothing is reported
    and nothing is written, even with `--cancel`), 1 when a requested cancel did not land, 0
    otherwise.
    """
    out = out or sys.stdout
    dm_rows = io.read_scheduled_dms()
    request_rows = io.read_connection_requests()
    unreadable = [t for t, rows in ((TABLE_SCHEDULED_DMS, dm_rows),
                                    (TABLE_CONNECTION_REQUESTS, request_rows)) if rows is None]
    if unreadable:
        sys.stderr.write(f"refusing to report: could not read {', '.join(unreadable)} — an "
                         "unreadable table is not an empty one. Nothing was written.\n")
        return EXIT_UNREADABLE

    scans = [scan_table(TABLE_SCHEDULED_DMS, dm_rows),
             scan_table(TABLE_CONNECTION_REQUESTS, request_rows)]
    report = build_report(scans)
    plan = plan_cancellations(scans)
    cancel_results = apply_cancellations(plan, io) if args.cancel else None

    if args.as_json:
        payload = {
            "report": report,
            "cancel": {
                "requested": bool(args.cancel),
                "planned": [{"table": f.table, "id": f.row_id} for f in plan],
                "results": cancel_results,
            },
        }
        print(json.dumps(payload, indent=2, default=str), file=out)
    else:
        print(render_text(report, plan, cancel_results), file=out)

    if cancel_results is not None and any(not r["ok"] for r in cancel_results):
        return EXIT_CANCEL_FAILED
    return EXIT_OK


def main(argv: Optional[Sequence[str]] = None, io: Optional[AuditIO] = None) -> int:
    """Entry point: parse the flags, bind production I/O unless one was injected, run."""
    args = build_parser().parse_args(argv)
    return run(args, io or production_io())


if __name__ == "__main__":
    sys.exit(main())
