"""`/api/billing/*` — Stripe checkout, the customer portal, and the webhook (#1154).

Same mechanic as the other routers: the auth kernel stays in `main` and is reached as
`_main.get_session_user_id`, an attribute resolved at REQUEST time, so patches aimed at
`cqc_lem.api.main.get_session_user_id` still bind what these handlers read. The billing db functions
moved with the handlers, so a patch aimed at `main` for one of THOSE has to be re-pointed here.

`from cqc_lem.api import main as _main` sits at the BOTTOM; `routers/__init__.py` explains why.
"""

import math
from datetime import datetime, timezone
from typing import Any, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from cqc_lem.api.models import ResponseModel
from cqc_lem.utilities.db import (
    add_avatar_credits,
    add_video_credits,
    get_avatar_credit_ledger_entry_by_session,
    get_early_adopter_grant,
    get_user_by_stripe_customer_id,
    get_user_subscription_info,
    get_video_credit_ledger_entry_by_session,
    update_subscription_from_stripe,
)
from cqc_lem.utilities.logger import log_info, log_warning
from cqc_lem.utilities.observability import (
    FUNNEL_CHURNED,
    FUNNEL_SUBSCRIPTION_STARTED,
    track_funnel_event,
)

router = APIRouter(prefix="/api/billing")


class CheckoutSessionRequest(BaseModel):
    """Body of `POST /billing/create-checkout-session` — the Stripe hand-off for a subscription `tier`.

    The URLs are where Stripe returns the browser afterwards.
    """

    session_token: str
    tier: str
    success_url: str
    cancel_url: str


class PortalSessionRequest(BaseModel):
    """Body of `POST /billing/create-portal-session` — a link into Stripe's own billing portal."""

    session_token: str
    return_url: str


def _early_adopter_checkout_extras(user_id: int) -> tuple[Optional[int], Optional[List[dict]]]:
    """Mirror an unfinished early-adopter trial into Stripe on conversion (issue #499): the days
    still left on the grant become the Checkout trial, and the optional launch coupon rides along.
    Only grant holders are affected — a standard trial converts exactly as it does today.

    Best-effort by design: this is a perk lookup, so any failure degrades to a normal checkout
    rather than blocking the user from paying us.
    """
    from cqc_lem.utilities.env_constants import EARLY_ADOPTER_COUPON_ID
    try:
        grant = get_early_adopter_grant(user_id)
    except Exception as e:
        log_warning("Could not read early-adopter grant for checkout", exc=e, user_id=user_id)
        return None, None
    if not grant:
        return None, None
    ends_at = grant.get("trial_ends_at")
    trial_period_days = None
    if ends_at:
        if ends_at.tzinfo is None:
            ends_at = ends_at.replace(tzinfo=timezone.utc)
        remaining = math.ceil((ends_at - datetime.now(timezone.utc)).total_seconds() / 86400)
        if remaining >= 1:
            trial_period_days = int(remaining)
    # The coupon rides with the unfinished trial, so an expired/exhausted grant carries neither —
    # otherwise a long-lapsed grant would keep discounting every future checkout.
    if trial_period_days is None:
        return None, None
    discounts = [{"coupon": EARLY_ADOPTER_COUPON_ID}] if EARLY_ADOPTER_COUPON_ID else None
    return trial_period_days, discounts


@router.post("/create-checkout-session")
def billing_create_checkout_session(request: CheckoutSessionRequest) -> ResponseModel[dict[str, Any]]:
    """Start a Stripe purchase — or, on an already-subscribed account, change the plan in place.

    The in-place branch matters: sending an active subscriber through Checkout registers a SECOND
    subscription and bills them twice. It answers `checkout_url: None, upgraded: True` because
    there is nowhere to send the browser — Stripe's `subscription.updated` webhook syncs the DB.
    A failed in-place upgrade falls back to Checkout rather than erroring.
    """
    user_id = _main.get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    subscription = get_user_subscription_info(user_id)
    stripe_customer_id = subscription.get("stripe_customer_id") if subscription else None
    if not stripe_customer_id:
        raise HTTPException(status_code=400, detail="No Stripe customer record — contact support")

    from cqc_lem.utilities.stripe_util import create_checkout_session, upgrade_subscription

    # If the user already has an active subscription, modify it in-place rather than
    # creating a new Checkout session — which would register a second subscription.
    existing_sub_id = subscription.get("stripe_subscription_id") if subscription else None
    existing_status = subscription.get("subscription_status") if subscription else None
    if existing_sub_id and existing_status in ("active", "trial"):
        upgraded = upgrade_subscription(existing_sub_id, request.tier)
        if upgraded:
            # No redirect needed — Stripe webhook will fire subscription.updated and sync DB
            return ResponseModel(status_code=200, detail={"checkout_url": None, "upgraded": True})
        log_info(
            f"In-place upgrade failed for sub={existing_sub_id}; falling back to checkout session"
        )

    trial_period_days, discounts = _early_adopter_checkout_extras(user_id)
    url = create_checkout_session(
        stripe_customer_id,
        request.tier,
        request.success_url,
        request.cancel_url,
        trial_period_days=trial_period_days,
        discounts=discounts,
    )
    if not url:
        raise HTTPException(status_code=500, detail="Could not create Stripe checkout session")
    return ResponseModel(status_code=200, detail={"checkout_url": url, "upgraded": False})


@router.post("/create-portal-session")
def billing_create_portal_session(request: PortalSessionRequest) -> ResponseModel[dict[str, Any]]:
    """A one-time link into Stripe's billing portal, where payment methods and cancellation live.

    An account with no Stripe customer record is a 400, not an empty portal — there is nothing for
    the user to manage and a dead link would look like a broken page.
    """
    user_id = _main.get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    subscription = get_user_subscription_info(user_id)
    stripe_customer_id = subscription.get("stripe_customer_id") if subscription else None
    if not stripe_customer_id:
        raise HTTPException(status_code=400, detail="No Stripe customer record — contact support")

    from cqc_lem.utilities.stripe_util import create_portal_session
    url = create_portal_session(stripe_customer_id, request.return_url)
    if not url:
        raise HTTPException(status_code=500, detail="Could not create Stripe portal session")
    return ResponseModel(status_code=200, detail={"portal_url": url})


def _track_billing_funnel(event: str, stripe_customer_id: str, **props) -> None:
    """Funnel event for a Stripe lifecycle webhook. The webhook carries no UTMs — PostHog holds them
    on the person from the `$set_once` written at signup — so only the plan facts ride along here.
    The customer→user lookup is guarded: analytics must never fail a billing webhook, because Stripe
    would retry it and the subscription state is already committed.
    """
    try:
        user = get_user_by_stripe_customer_id(stripe_customer_id) or {}
        track_funnel_event(event, user_id=user.get("id"),
                           distinct_id=f"stripe_{stripe_customer_id}",
                           stripe_customer_id=stripe_customer_id, **props)
    except Exception as e:
        log_warning(f"Could not track billing funnel event '{event}'", exc=e)


@router.post("/webhook")
async def billing_webhook(request: Request) -> dict:
    """Stripe's lifecycle webhook — public, because Stripe holds no session.

    The SIGNATURE is the credential, and an invalid one is a 400 before anything is read.

    It answers `{"received": True}` for everything it decided not to act on, deliberately: Stripe
    retries a non-2xx, so an unrecognised event, a missing customer or an already-granted purchase
    must ACK rather than error. Credit grants are idempotent on the checkout session id for the
    same reason. A refund only deducts on a FULL refund — a partial one does not map onto whole
    credits.
    """
    payload = await request.body()
    sig_header = request.headers.get("Stripe-Signature", "")

    from cqc_lem.utilities.stripe_util import (
        get_subscription_tier_from_price,
        stripe_status_to_db,
        validate_webhook,
    )
    event = validate_webhook(payload, sig_header)
    if event is None:
        raise HTTPException(status_code=400, detail="Invalid Stripe webhook signature")

    event_type = event.get("type", "")
    data = event.get("data", {}).get("object", {})
    log_info(f"Stripe webhook received: {event_type}")

    # --- Subscription lifecycle events ---
    if event_type in (
        "customer.subscription.created",
        "customer.subscription.updated",
    ):
        stripe_customer_id = data.get("customer")
        stripe_subscription_id = data.get("id")
        if not stripe_customer_id:
            log_info(f"Webhook {event_type} missing customer field — skipping")
            return {"received": True}
        sub_status = data.get("status", "")
        db_status = stripe_status_to_db(sub_status)

        # Determine tier from the first line item's price
        price_id = None
        items = data.get("items", {}).get("data", [])
        if items:
            price_id = items[0].get("price", {}).get("id")
        tier = get_subscription_tier_from_price(price_id) if price_id else None

        # Period end (Unix timestamp → datetime)
        period_end_ts = data.get("current_period_end")
        period_end = (
            datetime.fromtimestamp(period_end_ts, tz=timezone.utc) if period_end_ts else None
        )

        log_info(
            f"Subscription {stripe_subscription_id}: stripe_status={sub_status} "
            f"→ db_status={db_status}, tier={tier}, period_end={period_end}"
        )
        update_subscription_from_stripe(
            stripe_customer_id, db_status, tier, stripe_subscription_id, period_end
        )
        # Only `created` — `updated` fires on every plan/status change and would double-count.
        if event_type == "customer.subscription.created":
            _track_billing_funnel(
                FUNNEL_SUBSCRIPTION_STARTED, stripe_customer_id, tier=tier, status=db_status,
                stripe_subscription_id=stripe_subscription_id,
            )

    elif event_type == "customer.subscription.deleted":
        stripe_customer_id = data.get("customer")
        stripe_subscription_id = data.get("id")
        if not stripe_customer_id:
            log_info(f"Webhook {event_type} missing customer field — skipping")
            return {"received": True}
        log_info(f"Subscription {stripe_subscription_id} deleted for customer {stripe_customer_id}")
        # tier=None preserves the historical tier in the DB
        update_subscription_from_stripe(
            stripe_customer_id, "cancelled", None, stripe_subscription_id
        )
        _track_billing_funnel(FUNNEL_CHURNED, stripe_customer_id, reason="subscription_deleted",
                              stripe_subscription_id=stripe_subscription_id)

    # --- Invoice / payment events (fired on every billing cycle renewal) ---
    elif event_type == "invoice.payment_succeeded":
        stripe_customer_id = data.get("customer")
        stripe_subscription_id = data.get("subscription")
        if not stripe_customer_id:
            log_info(f"Webhook {event_type} missing customer field — skipping")
            return {"received": True}
        if stripe_subscription_id:
            log_info(
                f"Invoice payment succeeded for customer={stripe_customer_id}, "
                f"subscription={stripe_subscription_id} — marking active"
            )
            # Re-fetch the subscription to get the current tier and period end
            from cqc_lem.utilities.stripe_util import fetch_subscription
            sub = fetch_subscription(stripe_subscription_id)
            if sub:
                price_id = None
                items = sub.get("items", {}).get("data", [])
                if items:
                    price_id = items[0].get("price", {}).get("id")
                tier = get_subscription_tier_from_price(price_id) if price_id else None
                period_end_ts = sub.get("current_period_end")
                period_end = datetime.fromtimestamp(period_end_ts, tz=timezone.utc) if period_end_ts else None
                update_subscription_from_stripe(
                    stripe_customer_id, "active", tier, stripe_subscription_id, period_end
                )

    elif event_type == "invoice.payment_failed":
        stripe_customer_id = data.get("customer")
        stripe_subscription_id = data.get("subscription")
        if not stripe_customer_id:
            log_info(f"Webhook {event_type} missing customer field — skipping")
            return {"received": True}
        if stripe_subscription_id:
            log_info(
                f"Invoice payment FAILED for customer={stripe_customer_id}, "
                f"subscription={stripe_subscription_id} — marking past_due"
            )
            # tier=None preserves existing tier; status → past_due
            update_subscription_from_stripe(
                stripe_customer_id, "past_due", None, stripe_subscription_id
            )

    elif event_type == "checkout.session.completed":
        meta = data.get("metadata", {})
        if meta.get("type") == "avatar_credits":
            # Only credit on confirmed card payment — async methods (e.g. bank transfer)
            # may fire this event before funds clear.
            if data.get("payment_status") != "paid":
                log_info(
                    f"checkout.session.completed: payment_status={data.get('payment_status')} "
                    f"— not yet paid, skipping credit grant"
                )
                return {"received": True}

            stripe_customer_id = data.get("customer")
            stripe_session_id = data.get("id")
            credits = int(meta.get("credits", 0))
            package = meta.get("package", "unknown")

            if not stripe_customer_id or credits <= 0:
                log_info("Avatar credits webhook: missing customer or zero credits — skipping")
                return {"received": True}

            # Idempotency: Stripe may retry — skip if credits already granted for this session.
            if get_avatar_credit_ledger_entry_by_session(stripe_session_id):
                log_info(f"Avatar credits already granted for session={stripe_session_id} — skipping duplicate")
                return {"received": True}

            user_row = get_user_by_stripe_customer_id(stripe_customer_id)
            if user_row:
                add_avatar_credits(
                    user_row["id"],
                    credits,
                    f"purchase_{package}",
                    stripe_session_id,
                )
                log_info(
                    f"Added {credits} avatar credit(s) for user_id={user_row['id']} "
                    f"via session={stripe_session_id}"
                )
            else:
                log_info(f"Avatar credits webhook: no user found for customer={stripe_customer_id}")

        elif meta.get("type") == "video_credits":
            if data.get("payment_status") != "paid":
                log_info(f"video_credits checkout: payment_status={data.get('payment_status')} — skipping")
                return {"received": True}
            stripe_customer_id = data.get("customer")
            stripe_session_id = data.get("id")
            credits = int(meta.get("credits", 0))
            package = meta.get("package", "unknown")
            if not stripe_customer_id or credits <= 0:
                log_info("Video credits webhook: missing customer or zero credits — skipping")
                return {"received": True}
            if get_video_credit_ledger_entry_by_session(stripe_session_id):
                log_info(f"Video credits already granted for session={stripe_session_id} — skipping duplicate")
                return {"received": True}
            user_row = get_user_by_stripe_customer_id(stripe_customer_id)
            if user_row:
                add_video_credits(user_row["id"], credits, f"purchase_{package}", stripe_session_id)
                log_info(f"Added {credits} video credit(s) for user_id={user_row['id']} "
                         f"via session={stripe_session_id}")
            else:
                log_info(f"Video credits webhook: no user found for customer={stripe_customer_id}")

    elif event_type == "charge.refunded":
        payment_intent_id = data.get("payment_intent")
        stripe_customer_id = data.get("customer")
        amount = data.get("amount", 0)
        amount_refunded = data.get("amount_refunded", 0)

        if not payment_intent_id or not stripe_customer_id:
            log_info("charge.refunded: missing payment_intent or customer — skipping")
            return {"received": True}

        # Only deduct credits for a full refund — partial refunds don't map cleanly to credits.
        if amount_refunded < amount:
            log_info(
                f"charge.refunded: partial refund ({amount_refunded}/{amount} cents) "
                f"for customer={stripe_customer_id} — no credit adjustment"
            )
            return {"received": True}

        # Find the checkout session that generated this charge to check its metadata.
        from cqc_lem.utilities.stripe_util import get_checkout_session_by_payment_intent
        session = get_checkout_session_by_payment_intent(payment_intent_id)
        if not session:
            log_info(f"charge.refunded: no checkout session found for payment_intent={payment_intent_id}")
            return {"received": True}

        session_meta = session.get("metadata", {})
        credit_type = session_meta.get("type")
        if credit_type not in ("avatar_credits", "video_credits"):
            log_info("charge.refunded: not a credits charge — ignoring")
            return {"received": True}

        # Route to the right ledger based on what was purchased.
        if credit_type == "avatar_credits":
            entry_fn, add_fn, label = get_avatar_credit_ledger_entry_by_session, add_avatar_credits, "avatar"
        else:
            entry_fn, add_fn, label = get_video_credit_ledger_entry_by_session, add_video_credits, "video"

        stripe_session_id = session.get("id")
        original_entry = entry_fn(stripe_session_id)
        if not original_entry:
            log_info(f"charge.refunded: no {label} credit ledger entry for session={stripe_session_id} — nothing to deduct")
            return {"received": True}

        user_row = get_user_by_stripe_customer_id(stripe_customer_id)
        if not user_row:
            log_info(f"charge.refunded: no user found for customer={stripe_customer_id}")
            return {"received": True}

        credits_to_deduct = original_entry["delta"]
        add_fn(user_row["id"], -credits_to_deduct, f"refund_{stripe_session_id}", stripe_session_id=None)
        log_info(
            f"Deducted {credits_to_deduct} {label} credit(s) for user_id={user_row['id']} "
            f"due to full refund of session={stripe_session_id}"
        )

    else:
        log_info(f"Stripe webhook event ignored: {event_type}")

    return {"received": True}




from cqc_lem.api import main as _main  # noqa: E402  — last; see the module docstring
