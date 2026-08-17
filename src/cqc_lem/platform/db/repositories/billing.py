"""Every SQL statement LEM runs against the billing tables.

Split out of `cqc_lem.utilities.db` (issue #1154). The fail-soft reader contract and the
secret-sealing rules described there apply here unchanged; `cqc_lem.utilities.db`
re-exports every name below, so existing importers and patch targets keep resolving.
"""

from datetime import (
    date,
    datetime,
    timezone,
)
from typing import Optional

import mysql.connector
from mysql.connector import errorcode

from cqc_lem.platform.db import connection as _connection
from cqc_lem.platform.db.connection import db_cursor
from cqc_lem.platform.db.enums import (
    AffiliateRewardKind,
    AffiliateStatus,
    ReferralStatus,
)
from cqc_lem.utilities.logger import log_debug, log_error, log_info


def insert_cost_ledger_entry(feature: str, category: str, usd: float,
                             user_id: Optional[int] = None,
                             provider: Optional[str] = None,
                             model_tier: Optional[str] = None,
                             qty: Optional[float] = None,
                             post_id: Optional[int] = None,
                             task_name: Optional[str] = None,
                             incurred_on: Optional[date] = None) -> bool:
    """Append one spend row. `user_id` None means system/shared cost; `incurred_on` defaults to today (UTC)."""
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                """INSERT INTO cost_ledger
                       (user_id, feature, category, provider, model_tier, usd, qty, post_id, task_name, incurred_on)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (user_id, str(feature), str(category), provider, model_tier, round(float(usd), 6),
                 round(float(qty), 4) if qty is not None else None,
                 post_id, task_name, incurred_on or datetime.now(timezone.utc).date()),
            )
            return True
    except mysql.connector.Error as err:
        log_error(f"Could not insert cost_ledger entry ({category}/{feature})", exc=err)
        return False
def accrue_monthly_fixed_costs(period: date, accruals: list) -> int:
    """Write this month's fixed-cost accruals (proxy per user, infra amortization) idempotently.

    `period` is the first day of the accrued month and is stored as `incurred_on`; an accrual is a
    dict of {user_id, category, usd, provider?, feature?, qty?}. A (user_id, category, period)
    already present is skipped, so re-running the monthly task never double-charges. Returns the
    number of rows written.
    """
    if not accruals:
        return 0

    connection = _connection.get_db_connection()
    cursor = connection.cursor()
    written = 0
    try:
        for accrual in accruals:
            user_id = accrual.get("user_id")
            category = str(accrual.get("category"))
            # NULL-safe compare: system rows (user_id NULL) must still dedupe against each other.
            cursor.execute(
                "SELECT id FROM cost_ledger WHERE user_id <=> %s AND category = %s AND incurred_on = %s LIMIT 1",
                (user_id, category, period),
            )
            if cursor.fetchone():
                continue
            cursor.execute(
                """INSERT INTO cost_ledger
                       (user_id, feature, category, provider, usd, qty, incurred_on)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (user_id, str(accrual.get("feature", "system")), category, accrual.get("provider"),
                 round(float(accrual.get("usd", 0)), 6),
                 round(float(accrual["qty"]), 4) if accrual.get("qty") is not None else None,
                 period),
            )
            written += 1
        connection.commit()
        return written
    except mysql.connector.Error as err:
        log_error(f"Could not accrue monthly fixed costs for {period}", exc=err)
        return written
    finally:
        cursor.close()
        connection.close()
# Whitelisted rollup dimensions → the cost_ledger column each groups by. Interpolating anything
# outside this map into the SQL would be an injection vector.
COST_ROLLUP_COLUMNS = {
    "feature": "feature",
    "category": "category",
    "provider": "provider",
    "model_tier": "model_tier",
    "user": "user_id",
    "task": "task_name",
}
def get_cost_rollup(start_date, end_date, group_by: str = "feature",
                    user_id: Optional[int] = None) -> dict:
    """Summed `cost_ledger.usd` over [start_date, end_date] grouped by one dimension from
    COST_ROLLUP_COLUMNS → `{key: usd}`. Omitting `user_id` includes EVERY row, shared/system spend
    (NULL user_id) included, which is what the system-wide margin totals need. Rows with a NULL
    group value collapse into the "unknown" key so their spend is never dropped — the column is CAST
    to CHAR first so a numeric dimension (user_id) can't coerce 'unknown' into 0 and merge NULL
    rows with a real user 0.
    """
    column = COST_ROLLUP_COLUMNS.get(group_by)
    if not column:
        log_info(f"Unsupported cost rollup dimension '{group_by}'")
        return {}
    try:
        with db_cursor() as cursor:
            sql = (f"SELECT COALESCE(CAST({column} AS CHAR), 'unknown') AS rollup_key, "
                   "COALESCE(SUM(usd), 0) FROM cost_ledger WHERE incurred_on BETWEEN %s AND %s")
            params = [start_date, end_date]
            if user_id is not None:
                sql += " AND user_id = %s"
                params.append(user_id)
            cursor.execute(sql + " GROUP BY rollup_key", tuple(params))
            return {str(key): float(usd or 0) for key, usd in cursor.fetchall()}
    except mysql.connector.Error:
        return {}  # table not created yet (or unreadable) — caller reports it as unavailable
def get_daily_cost_totals(start_date, end_date) -> dict:
    """Total spend per DAY over [start_date, end_date] → `{'YYYY-MM-DD': usd}` — the trailing series
    the §E.2 spend-anomaly check scores today against. A day with no ledger rows is ABSENT rather
    than 0.0 so a ledger that only started capturing mid-window can't manufacture a zero baseline
    (and then flag the first real day of spend as an anomaly).
    """
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT incurred_on, COALESCE(SUM(usd), 0) FROM cost_ledger "
                "WHERE incurred_on BETWEEN %s AND %s GROUP BY incurred_on ORDER BY incurred_on",
                (start_date, end_date),
            )
            return {day.isoformat() if hasattr(day, "isoformat") else str(day): float(usd or 0)
                    for day, usd in cursor.fetchall()}
    except mysql.connector.Error:
        return {}  # table not created yet (or unreadable) — caller reports the check as skipped
def get_early_adopter_grant(user_id: int) -> Optional[dict]:
    """The user's early-adopter grant, or None. Read by the checkout flow so the extension mirrors
    into Stripe on conversion.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT id, user_id, cohort, trial_days, feedback_id, trial_ends_at, granted_at "
                "FROM early_adopter_grants WHERE user_id=%s",
                (user_id,),
            )
            return cursor.fetchone()
    except mysql.connector.Error as err:
        log_error("Could not fetch early-adopter grant", exc=err, user_id=user_id)
        return None
def get_early_adopter_slot_usage() -> dict:
    """`{cohort: used}` for every cohort row — what the caps are measured against."""
    try:
        with db_cursor() as cursor:
            cursor.execute("SELECT cohort, used FROM early_adopter_slots")
            return {str(cohort): int(used or 0) for cohort, used in cursor.fetchall()}
    except mysql.connector.Error as err:
        log_error("Could not read early-adopter slot usage", exc=err)
        return {}
def mark_affiliate_notice_seen(user_id: int) -> bool:
    """Record that the user has actually SEEN the enrollment notice. Default-enrollment is only
    honest if the notice was delivered, so this timestamp is the evidence — not a UI nicety.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE affiliate_enrollments SET notice_seen_at=COALESCE(notice_seen_at,%s) WHERE user_id=%s",
                (datetime.now(timezone.utc), user_id),
            )
            return True
    except mysql.connector.Error as err:
        log_error("Could not mark affiliate notice seen", exc=err, user_id=user_id)
        return False
def get_affiliate_reward_totals(user_id: int) -> dict:
    """`{total, enrollment, referral}` granted days. `total` is the SUM over the whole ledger,
    revocations included as negatives — it is what the per-user cap is measured against, so a user
    who opts out and back in cannot use the round trip to mint days.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT kind, COALESCE(SUM(trial_days),0) AS days FROM affiliate_rewards "
                "WHERE user_id=%s GROUP BY kind",
                (user_id,),
            )
            by_kind = {str(r["kind"]): int(r["days"] or 0) for r in cursor.fetchall()}
            return {
                "total": sum(by_kind.values()),
                "enrollment": by_kind.get(str(AffiliateRewardKind.ENROLLMENT), 0),
                "referral": by_kind.get(str(AffiliateRewardKind.REFERRAL), 0),
                "revoked": by_kind.get(str(AffiliateRewardKind.REVOKED), 0),
            }
    except mysql.connector.Error as err:
        log_error("Could not read affiliate reward totals", exc=err, user_id=user_id)
        return {"total": 0, "enrollment": 0, "referral": 0, "revoked": 0}
def get_affiliate_referral_counts(user_id: int) -> dict:
    """`{pending, converted, rejected}` referrals this member has driven."""
    counts = {str(s): 0 for s in ReferralStatus}
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT status, COUNT(*) AS n FROM affiliate_referrals WHERE referrer_user_id=%s GROUP BY status",
                (user_id,),
            )
            for row in cursor.fetchall():
                counts[str(row["status"])] = int(row["n"] or 0)
            return counts
    except mysql.connector.Error as err:
        log_error("Could not read affiliate referral counts", exc=err, user_id=user_id)
        return counts
def get_affiliate_referral_for_referred(referred_user_id: int) -> Optional[dict]:
    """The one referral row a referred user can have, or None."""
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT id, referrer_user_id, referred_user_id, referral_code, status, reject_reason, "
                "created_at, converted_at FROM affiliate_referrals WHERE referred_user_id=%s",
                (referred_user_id,),
            )
            return cursor.fetchone()
    except mysql.connector.Error as err:
        log_error("Could not read affiliate referral", exc=err, user_id=referred_user_id)
        return None
def record_affiliate_referral(referrer_user_id: int, referred_user_id: int, referral_code: str,
                              status: str = 'pending',
                              reject_reason: Optional[str] = None) -> Optional[int]:
    """Attribute a new signup to a referrer. Returns the referral id, or None when one already
    exists for this referred user.

    A rejected referral is WRITTEN (status='rejected' + a reason) rather than discarded: the caller
    has already decided it doesn't pay, and a self-referral we can count is a fraud signal, while a
    self-referral we dropped is nothing. The UNIQUE key on referred_user_id is what makes a replayed
    signup a no-op rather than a second attribution.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "INSERT INTO affiliate_referrals (referrer_user_id, referred_user_id, referral_code, "
                "status, reject_reason) VALUES (%s,%s,%s,%s,%s)",
                (referrer_user_id, referred_user_id, str(referral_code), str(status), reject_reason),
            )
            return cursor.lastrowid
    except mysql.connector.Error as err:
        if err.errno == errorcode.ER_DUP_ENTRY:
            # DEBUG, not INFO: the UNIQUE key on referred_user_id is the mechanism that makes a
            # replayed signup a no-op, so this fires on working behaviour every time one replays.
            log_debug("Referral already attributed — ignoring duplicate", user_id=referred_user_id)
            return None
        log_error("Could not record affiliate referral", exc=err, user_id=referred_user_id)
        return None
def convert_affiliate_referral(referred_user_id: int) -> Optional[dict]:
    """Mark a PENDING referral converted, and return it. Returns None when there is nothing to
    convert — no referral, already converted, or rejected — so the caller's reward grant is driven
    by the rowcount rather than by a re-read that a concurrent activation could race.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE affiliate_referrals SET status=%s, converted_at=%s "
                "WHERE referred_user_id=%s AND status=%s",
                (str(ReferralStatus.CONVERTED), datetime.now(timezone.utc), referred_user_id,
                 str(ReferralStatus.PENDING)),
            )
            if cursor.rowcount != 1:
                return None
    except mysql.connector.Error as err:
        log_error("Could not convert affiliate referral", exc=err, user_id=referred_user_id)
        return None
    return get_affiliate_referral_for_referred(referred_user_id)


def _affiliate_row(row: Optional[dict]) -> Optional[dict]:
    """Normalize an enrollment row for callers: booleans as booleans, status as a plain string."""
    if not row:
        return None
    return {
        "user_id": int(row["user_id"]),
        "status": str(row["status"]),
        "referral_code": str(row.get("referral_code") or ""),
        "enrolled_at": row.get("enrolled_at"),
        "opted_out_at": row.get("opted_out_at"),
        "notice_seen_at": row.get("notice_seen_at"),
        "promo_content_opt_in": bool(row.get("promo_content_opt_in")),
        "promo_consent_at": row.get("promo_consent_at"),
        "promo_consent_version": row.get("promo_consent_version"),
    }
def get_affiliate_enrollment(user_id: int) -> Optional[dict]:
    """The user's affiliate row, or None when they have never been enrolled.

    The row here carries columns only — no `created` key. That flag exists solely on what
    `ensure_affiliate_enrollment` returns, because only the call that wrote the row can know it.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT user_id, status, referral_code, enrolled_at, opted_out_at, notice_seen_at, "
                "promo_content_opt_in, promo_consent_at, promo_consent_version "
                "FROM affiliate_enrollments WHERE user_id=%s",
                (user_id,),
            )
            return _affiliate_row(cursor.fetchone())
    except mysql.connector.Error as err:
        log_error("Could not read affiliate enrollment", exc=err, user_id=user_id)
        return None
def ensure_affiliate_enrollment(user_id: int, status: str = 'enrolled',
                                referral_code: Optional[str] = None) -> Optional[dict]:
    """Create the user's affiliate row if it doesn't exist, then return it.

    Idempotent by INSERT IGNORE rather than read-then-write: two requests racing on a first page
    load must not produce two rows or a duplicate-key 500. An existing row is never re-statused
    here — an opted-out user staying opted out is the entire point of the opt-out.

    The returned row carries `created` — whether THIS call is the one that enrolled them. Every
    Account page load calls this, so it is the only way the caller can emit an enrollment event once
    instead of on every render. It is a synthetic key, not a column: `get_affiliate_enrollment`
    never sets it, and nothing that serializes the row to a client reads it (`affiliate_state`
    builds its payload field by field).

    On a DB error this returns None, so a caller can never read `created=False` from a write that
    did not happen — the row will not exist either, and the next call re-inserts it.
    """
    code = str(referral_code or user_id)
    connection = _connection.get_db_connection()
    cursor = connection.cursor()
    created = False
    try:
        cursor.execute(
            "INSERT IGNORE INTO affiliate_enrollments (user_id, status, referral_code, enrolled_at) "
            "VALUES (%s,%s,%s,%s)",
            (user_id, str(status), code,
             datetime.now(timezone.utc) if str(status) == str(AffiliateStatus.ENROLLED) else None),
        )
        created = cursor.rowcount == 1
        connection.commit()
    except mysql.connector.Error as err:
        log_error("Could not create affiliate enrollment", exc=err, user_id=user_id)
        return None
    finally:
        cursor.close()
        connection.close()
    row = get_affiliate_enrollment(user_id)
    if row is not None:
        row["created"] = created
    return row
def set_affiliate_status(user_id: int, enrolled: bool) -> Optional[dict]:
    """Opt the user in or out of (A) affiliate status. Immediate — the caller reflects the resulting
    trial length back to the user, and the reward side (grant/revoke of the enrollment bonus) is the
    caller's separate, ledgered step so a status flip can never silently move money.
    """
    status = AffiliateStatus.ENROLLED if enrolled else AffiliateStatus.OPTED_OUT
    now = datetime.now(timezone.utc)
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE affiliate_enrollments SET status=%s, "
                "enrolled_at=IF(%s, COALESCE(enrolled_at,%s), enrolled_at), "
                "opted_out_at=IF(%s, opted_out_at, %s) WHERE user_id=%s",
                (str(status), enrolled, now, enrolled, now, user_id),
            )
    except mysql.connector.Error as err:
        log_error("Could not update affiliate status", exc=err, user_id=user_id)
        return None
    return get_affiliate_enrollment(user_id)
def set_affiliate_promo_opt_in(user_id: int, enabled: bool, consent_version: str) -> Optional[dict]:
    """(B) — whether LEM may publish promotional content about LEM from the user's own LinkedIn
    account. Enabling stamps the consent timestamp AND the version of the copy consented to;
    disabling clears both, so a re-enable can never inherit an old consent record.
    """
    now = datetime.now(timezone.utc) if enabled else None
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE affiliate_enrollments SET promo_content_opt_in=%s, promo_consent_at=%s, "
                "promo_consent_version=%s WHERE user_id=%s",
                (1 if enabled else 0, now, str(consent_version) if enabled else None, user_id),
            )
    except mysql.connector.Error as err:
        log_error("Could not update affiliate promo consent", exc=err, user_id=user_id)
        return None
    return get_affiliate_enrollment(user_id)
