"""Every SQL statement LEM runs against the billing tables.

Split out of `cqc_lem.utilities.db` (issue #1154). The fail-soft reader contract and the
secret-sealing rules described there apply here unchanged; `cqc_lem.utilities.db`
re-exports every name below, so existing importers and patch targets keep resolving.
"""

from datetime import (
    date,
    datetime,
    timedelta,
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


def cost_ledger_available() -> bool:
    """True when the durable cost_ledger table exists. The margin report uses this to say whether a
    $0 spend figure means "nothing spent" or "not capturing yet".
    """
    try:
        with db_cursor() as cursor:
            cursor.execute("SHOW TABLES LIKE 'cost_ledger'")
            return cursor.fetchone() is not None
    except mysql.connector.Error:
        return False
def get_user_cost(user_id: int, start_date, end_date) -> dict:
    """One user's spend over a window, grouped by cost category (llm/media/proxy/infra/...)."""
    return get_cost_rollup(start_date, end_date, group_by="category", user_id=user_id)
# Cohorts are tried in order: P0 (the hand-picked launch group) fills first, then P1. Capacities
# come from env at call time so the caps can be retuned without a migration or a code change.
EARLY_ADOPTER_COHORTS = ("P0", "P1")
# Statuses an extension may act on. A paying ('active'/'past_due') or churned ('cancelled') user is
# not on a trial, so extending one would either be a no-op or silently reopen a closed account.
# The subscription statuses for which `users.trial_ends_at` is a live date rather than a leftover:
# a paid or cancelled account carries an old value that must never be extended or quoted back.
TRIAL_EXTENDABLE_STATUSES = ("trial", "inactive")
def _as_naive_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """MySQL DATETIME columns come back naive; our own timestamps are UTC-aware. Normalize both to
    naive-UTC so they can be compared without a TypeError.
    """
    if dt is None or dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)
def extend_trial_for_user(user_id: int, feedback_id: Optional[int] = None) -> dict:
    """Claim an early-adopter cohort slot and extend the user's trial to
    `trial_started_at + EARLY_ADOPTER_TRIAL_DAYS` (issue #499).

    The caller owns the review gate; this owns atomicity. Everything below runs in ONE transaction:
    the slot claim is a single conditional UPDATE (its rowcount IS the claim result, so two
    concurrent requests can never both take the last slot), and the unique `user_id` on
    early_adopter_grants means a duplicate request rolls the whole thing back — including the
    counter — rather than burning a second slot.

    Returns a dict the API can hand straight to the SPA:
      granted, reason, cohort, trial_days, trial_ends_at
    where reason is one of granted | already_granted | slots_exhausted | not_on_trial |
    user_not_found | error.
    """
    from cqc_lem.utilities.env_constants import (
        EARLY_ADOPTER_P0_SLOTS,
        EARLY_ADOPTER_P1_SLOTS,
        EARLY_ADOPTER_TRIAL_DAYS,
        FREE_TRIAL_DAYS,
    )
    capacities = {"P0": EARLY_ADOPTER_P0_SLOTS, "P1": EARLY_ADOPTER_P1_SLOTS}

    def _result(granted: bool, reason: str, cohort: Optional[str] = None,
                trial_days: int = FREE_TRIAL_DAYS, trial_ends_at: Optional[datetime] = None) -> dict:
        return {"granted": granted, "reason": reason, "cohort": cohort,
                "trial_days": trial_days, "trial_ends_at": trial_ends_at}

    connection = _connection.get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        connection.start_transaction()

        cursor.execute(
            "SELECT cohort, trial_days, trial_ends_at FROM early_adopter_grants WHERE user_id=%s FOR UPDATE",
            (user_id,),
        )
        existing = cursor.fetchone()
        if existing:
            connection.rollback()
            return _result(True, "already_granted", existing["cohort"],
                           int(existing["trial_days"]), existing["trial_ends_at"])

        cursor.execute(
            "SELECT subscription_status, trial_started_at, trial_ends_at FROM users WHERE id=%s FOR UPDATE",
            (user_id,),
        )
        user = cursor.fetchone()
        if not user:
            connection.rollback()
            return _result(False, "user_not_found")
        if user["subscription_status"] not in TRIAL_EXTENDABLE_STATUSES:
            connection.rollback()
            return _result(False, "not_on_trial")

        claimed: Optional[str] = None
        for cohort in EARLY_ADOPTER_COHORTS:
            capacity = int(capacities.get(cohort, 0))
            if capacity <= 0:
                continue
            cursor.execute(
                "UPDATE early_adopter_slots SET used = used + 1 WHERE cohort=%s AND used < %s",
                (cohort, capacity),
            )
            if cursor.rowcount == 1:
                claimed = cohort
                break
        if not claimed:
            connection.rollback()
            return _result(False, "slots_exhausted")

        started = _as_naive_utc(user["trial_started_at"]) or datetime.now(timezone.utc).replace(tzinfo=None)
        new_end = started + timedelta(days=EARLY_ADOPTER_TRIAL_DAYS)
        current_end = _as_naive_utc(user["trial_ends_at"])
        # An extension must never shorten a trial the user already has.
        if current_end and current_end > new_end:
            new_end = current_end

        cursor.execute(
            "INSERT INTO early_adopter_grants (user_id, cohort, trial_days, feedback_id, trial_ends_at) "
            "VALUES (%s,%s,%s,%s,%s)",
            (user_id, claimed, EARLY_ADOPTER_TRIAL_DAYS, feedback_id, new_end),
        )
        cursor.execute(
            "UPDATE users SET trial_started_at=%s, trial_ends_at=%s, subscription_status='trial', "
            "subscription_tier=COALESCE(subscription_tier,'free_trial') WHERE id=%s",
            (started, new_end, user_id),
        )
        connection.commit()
        log_info("Early-adopter trial granted", user_id=user_id)
        return _result(True, "granted", claimed, EARLY_ADOPTER_TRIAL_DAYS, new_end)
    except mysql.connector.Error as err:
        connection.rollback()
        if err.errno == errorcode.ER_DUP_ENTRY:
            # Two concurrent requests for the same user; the rollback released the slot this one took.
            existing = get_early_adopter_grant(user_id)
            if existing:
                return _result(True, "already_granted", existing["cohort"],
                               int(existing["trial_days"]), existing["trial_ends_at"])
        log_error("Could not extend trial", exc=err, user_id=user_id)
        return _result(False, "error")
    finally:
        cursor.close()
        connection.close()
def _affiliate_baseline_trial_end(cursor, user_id: int, started: datetime) -> datetime:
    """The trial end a revoked enrollment bonus may never take the user below: their standard trial,
    any early-adopter grant (#499), and every referral day they EARNED. Only the enrollment bonus is
    contingent on status; nothing else the user holds is.
    """
    from cqc_lem.utilities.env_constants import FREE_TRIAL_DAYS
    baseline = started + timedelta(days=FREE_TRIAL_DAYS)
    cursor.execute("SELECT trial_ends_at FROM early_adopter_grants WHERE user_id=%s", (user_id,))
    grant = cursor.fetchone()
    grant_end = _as_naive_utc(grant["trial_ends_at"]) if grant else None
    if grant_end and grant_end > baseline:
        baseline = grant_end
    cursor.execute(
        "SELECT COALESCE(SUM(trial_days),0) AS days FROM affiliate_rewards WHERE user_id=%s AND kind=%s",
        (user_id, str(AffiliateRewardKind.REFERRAL)),
    )
    earned = cursor.fetchone()
    return baseline + timedelta(days=max(0, int((earned or {}).get("days") or 0)))
def grant_affiliate_trial_days(user_id: int, days: int, kind: str,
                               referral_id: Optional[int] = None,
                               reason: Optional[str] = None) -> dict:
    """Extend the user's trial by `days` and write the matching ledger row, in ONE transaction.

    Capped twice: by `AFFILIATE_MAX_REWARD_DAYS` against the user's own ledger sum (a partial grant
    is granted, not refused — the user gets what is left under the ceiling), and by the ENUM'd
    `kind`. Only trialling users are extended; a paying subscriber has no trial to lengthen, and
    silently paying them in a currency they can't spend would look like a granted reward in the UI.

    Returns `{granted, reason, days, total_days, trial_ends_at}` where reason is one of
    granted | already_granted | capped | not_on_trial | user_not_found | disabled | error.
    """
    from cqc_lem.utilities.marketing.affiliate import grantable_days, program_enabled

    def _result(granted: bool, why: str, days_granted: int = 0, total: int = 0,
                ends_at: Optional[datetime] = None) -> dict:
        return {"granted": granted, "reason": why, "days": days_granted,
                "total_days": total, "trial_ends_at": ends_at}

    if not program_enabled():
        return _result(False, "disabled")
    if int(days) <= 0:
        return _result(False, "capped")

    connection = _connection.get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        connection.start_transaction()
        cursor.execute(
            "SELECT COALESCE(SUM(trial_days),0) AS total FROM affiliate_rewards WHERE user_id=%s FOR UPDATE",
            (user_id,),
        )
        already = int((cursor.fetchone() or {}).get("total") or 0)

        # One enrollment bonus at a time: the migration's UNIQUE key only constrains referral rows
        # (repeated NULLs are legal), so the "already granted" check for the status bonus is held
        # here, inside the same transaction that would pay it.
        if str(kind) == str(AffiliateRewardKind.ENROLLMENT):
            cursor.execute(
                "SELECT COALESCE(SUM(trial_days),0) AS net FROM affiliate_rewards "
                "WHERE user_id=%s AND kind IN (%s,%s) FOR UPDATE",
                (user_id, str(AffiliateRewardKind.ENROLLMENT), str(AffiliateRewardKind.REVOKED)),
            )
            if int((cursor.fetchone() or {}).get("net") or 0) > 0:
                connection.rollback()
                return _result(True, "already_granted", 0, already)

        payable = grantable_days(already, int(days))
        if payable <= 0:
            connection.rollback()
            return _result(False, "capped", 0, already)

        cursor.execute(
            "SELECT subscription_status, trial_started_at, trial_ends_at FROM users WHERE id=%s FOR UPDATE",
            (user_id,),
        )
        user = cursor.fetchone()
        if not user:
            connection.rollback()
            return _result(False, "user_not_found")
        if user["subscription_status"] not in TRIAL_EXTENDABLE_STATUSES:
            connection.rollback()
            return _result(False, "not_on_trial", 0, already)

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        started = _as_naive_utc(user["trial_started_at"]) or now
        # Extend from whichever is later: a trial that already lapsed is extended from TODAY, or the
        # reward would land entirely in the past and read as nothing happening.
        current_end = _as_naive_utc(user["trial_ends_at"]) or now
        new_end = max(current_end, now) + timedelta(days=payable)

        cursor.execute(
            "INSERT INTO affiliate_rewards (user_id, referral_id, kind, trial_days, reason, trial_ends_at) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (user_id, referral_id, str(kind), payable, reason, new_end),
        )
        cursor.execute(
            "UPDATE users SET trial_started_at=%s, trial_ends_at=%s, subscription_status='trial', "
            "subscription_tier=COALESCE(subscription_tier,'free_trial') WHERE id=%s",
            (started, new_end, user_id),
        )
        connection.commit()
        log_info(f"Affiliate reward granted: +{payable} trial days ({kind})", user_id=user_id)
        return _result(True, "granted", payable, already + payable, new_end)
    except mysql.connector.Error as err:
        connection.rollback()
        if err.errno == errorcode.ER_DUP_ENTRY:
            # A concurrent activation already paid this referral. Not an error — the invariant held.
            return _result(True, "already_granted")
        log_error("Could not grant affiliate trial days", exc=err, user_id=user_id)
        return _result(False, "error")
    finally:
        cursor.close()
        connection.close()
def revoke_affiliate_enrollment_bonus(user_id: int) -> dict:
    """Return an opted-out user to their standard trial: subtract the enrollment bonus still standing
    and write the negative ledger row, in one transaction.

    Never takes the trial below `_affiliate_baseline_trial_end` — the standard trial, any
    early-adopter grant, and every referral day the user EARNED all survive an opt-out. That is what
    keeps "your trial returns to the standard N days" true rather than punitive.
    """
    def _result(revoked: bool, why: str, days: int = 0,
                ends_at: Optional[datetime] = None) -> dict:
        return {"revoked": revoked, "reason": why, "days": days, "trial_ends_at": ends_at}

    connection = _connection.get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        connection.start_transaction()
        cursor.execute(
            "SELECT COALESCE(SUM(trial_days),0) AS net FROM affiliate_rewards "
            "WHERE user_id=%s AND kind IN (%s,%s) FOR UPDATE",
            (user_id, str(AffiliateRewardKind.ENROLLMENT), str(AffiliateRewardKind.REVOKED)),
        )
        standing = int((cursor.fetchone() or {}).get("net") or 0)
        if standing <= 0:
            connection.rollback()
            return _result(False, "nothing_to_revoke")

        cursor.execute(
            "SELECT trial_started_at, trial_ends_at FROM users WHERE id=%s FOR UPDATE",
            (user_id,),
        )
        user = cursor.fetchone()
        if not user:
            connection.rollback()
            return _result(False, "user_not_found")

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        started = _as_naive_utc(user["trial_started_at"]) or now
        current_end = _as_naive_utc(user["trial_ends_at"]) or now
        baseline = _affiliate_baseline_trial_end(cursor, user_id, started)
        new_end = max(current_end - timedelta(days=standing), baseline)

        cursor.execute(
            "INSERT INTO affiliate_rewards (user_id, referral_id, kind, trial_days, reason, trial_ends_at) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (user_id, None, str(AffiliateRewardKind.REVOKED), -standing, "opted_out", new_end),
        )
        cursor.execute("UPDATE users SET trial_ends_at=%s WHERE id=%s", (new_end, user_id))
        connection.commit()
        log_info(f"Affiliate enrollment bonus revoked: -{standing} trial days", user_id=user_id)
        return _result(True, "revoked", standing, new_end)
    except mysql.connector.Error as err:
        connection.rollback()
        log_error("Could not revoke affiliate enrollment bonus", exc=err, user_id=user_id)
        return _result(False, "error")
    finally:
        cursor.close()
        connection.close()
