"""
Stripe billing integration.

Test mode:  set STRIPE_API_KEY=sk_test_... in .env
Live mode:  set STRIPE_API_KEY=sk_live_... in .env (switch when ready for production)

Create products and prices in the Stripe dashboard, then copy the price IDs
into STRIPE_PRICE_ID_STARTER / _PROFESSIONAL / _ENTERPRISE in .env.

Webhook:
  1. Run `stripe listen --forward-to localhost:8000/api/billing/webhook` during local dev
  2. Copy the printed signing secret into STRIPE_WEBHOOK_SECRET in .env
  3. In production, set the endpoint URL in your Stripe dashboard and use that secret
"""
import json
from typing import Optional

from cqc_lem.utilities.env_constants import (
    STRIPE_API_KEY,
    STRIPE_PRICE_ID_ENTERPRISE,
    STRIPE_PRICE_ID_PROFESSIONAL,
    STRIPE_PRICE_ID_STARTER,
    STRIPE_WEBHOOK_SECRET,
)
from cqc_lem.utilities.logger import myprint

# Map tier names → Stripe price IDs (populated from env at import time)
TIER_PRICE_MAP: dict[str, Optional[str]] = {
    "starter": STRIPE_PRICE_ID_STARTER,
    "professional": STRIPE_PRICE_ID_PROFESSIONAL,
    "enterprise": STRIPE_PRICE_ID_ENTERPRISE,
}


def _get_stripe():
    """Return the stripe module with the API key configured.
    Lazy import so the module loads even when STRIPE_API_KEY is unset (e.g. tests).
    """
    import stripe as _stripe
    _stripe.api_key = STRIPE_API_KEY
    return _stripe


def create_stripe_customer(email: str, user_id: int) -> Optional[str]:
    """Create a Stripe customer for a new user. Returns the customer ID or None on failure."""
    if not STRIPE_API_KEY:
        myprint("STRIPE_API_KEY not set — skipping Stripe customer creation")
        return None
    stripe = _get_stripe()
    try:
        customer = stripe.Customer.create(
            email=email,
            metadata={"user_id": str(user_id)},
        )
        myprint(f"Stripe customer created: {customer.id} for user_id={user_id}")
        return customer.id
    except Exception as e:
        myprint(f"Stripe customer creation failed for user_id={user_id}: {e}")
        return None


# Stripe rejects a subscription trial longer than 2 years.
STRIPE_MAX_TRIAL_DAYS = 730


def create_checkout_session(
    stripe_customer_id: str,
    tier: str,
    success_url: str,
    cancel_url: str,
    trial_period_days: Optional[int] = None,
    discounts: Optional[list[dict]] = None,
) -> Optional[str]:
    """Create a Stripe Checkout session for a subscription upgrade.
    Returns the checkout session URL, or None on failure.

    `trial_period_days` carries an unfinished trial (e.g. an early-adopter extension, issue #499)
    into Stripe so converting early doesn't forfeit the remaining days; `discounts` is passed
    straight through as Stripe's discounts list (e.g. `[{"coupon": "EARLYBIRD"}]`).
    """
    price_id = TIER_PRICE_MAP.get(tier)
    if not price_id:
        myprint(f"No Stripe price ID configured for tier '{tier}'")
        return None
    if not STRIPE_API_KEY:
        myprint("STRIPE_API_KEY not set — cannot create checkout session")
        return None

    extra: dict = {}
    if trial_period_days and int(trial_period_days) > 0:
        extra["subscription_data"] = {
            "trial_period_days": min(int(trial_period_days), STRIPE_MAX_TRIAL_DAYS)
        }
    if discounts:
        extra["discounts"] = discounts

    stripe = _get_stripe()
    try:
        session = stripe.checkout.Session.create(
            customer=stripe_customer_id,
            payment_method_types=["card"],
            line_items=[{"price": price_id, "quantity": 1}],
            mode="subscription",
            success_url=success_url,
            cancel_url=cancel_url,
            **extra,
        )
        return session.url
    except Exception as e:
        myprint(f"Stripe checkout session creation failed for customer={stripe_customer_id}: {e}")
        return None


def create_portal_session(stripe_customer_id: str, return_url: str) -> Optional[str]:
    """Create a Stripe Billing Portal session so users can manage their subscription.
    Returns the portal URL, or None on failure.
    """
    if not STRIPE_API_KEY:
        myprint("STRIPE_API_KEY not set — cannot create portal session")
        return None
    stripe = _get_stripe()
    try:
        session = stripe.billing_portal.Session.create(
            customer=stripe_customer_id,
            return_url=return_url,
        )
        return session.url
    except Exception as e:
        myprint(f"Stripe portal session creation failed for customer={stripe_customer_id}: {e}")
        return None


def validate_webhook(payload: bytes, sig_header: str) -> Optional[dict]:
    """Validate a Stripe webhook event using the signing secret.
    Returns the event as a plain Python dict on success, None on failure.

    The Stripe SDK v5+ returns a StripeObject (not a dict), which doesn't
    support .get(). We validate the signature via the SDK, then parse the
    raw payload as a dict so callers can use normal dict access.
    """
    if not STRIPE_WEBHOOK_SECRET:
        myprint("STRIPE_WEBHOOK_SECRET not set — cannot validate webhook")
        return None
    # Import stripe before the try block so it's in scope for the except clause.
    # If the package is not installed, the ImportError propagates immediately rather
    # than masking it as a NameError inside the except handler.
    stripe = _get_stripe()
    SignatureVerificationError = stripe.error.SignatureVerificationError
    try:
        # Raises on invalid signature — this is the only thing we need the SDK for here
        stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
        # Return the raw payload as a plain dict for consistent .get() access
        return json.loads(payload.decode("utf-8"))
    except SignatureVerificationError as e:
        myprint(f"Stripe webhook signature invalid: {e}")
        return None
    except Exception as e:
        myprint(f"Stripe webhook validation failed: {e}")
        return None


def upgrade_subscription(stripe_subscription_id: str, tier: str) -> bool:
    """Modify an existing Stripe subscription to a new price/tier in-place.

    This prevents duplicate subscriptions when an existing subscriber upgrades
    or downgrades — instead of opening a new Checkout session, we update the
    single existing subscription's line item to the new price. Stripe handles
    proration automatically.

    Returns True on success, False on any failure (caller falls back to checkout).
    """
    price_id = TIER_PRICE_MAP.get(tier)
    if not price_id:
        myprint(f"No Stripe price ID configured for tier '{tier}' — cannot upgrade")
        return False
    if not STRIPE_API_KEY:
        myprint("STRIPE_API_KEY not set — cannot upgrade subscription")
        return False
    stripe = _get_stripe()
    try:
        sub = stripe.Subscription.retrieve(stripe_subscription_id)
        items = sub.get("items", {}).get("data", [])
        if not items:
            myprint(f"Subscription {stripe_subscription_id} has no line items — cannot upgrade")
            return False
        item_id = items[0].get("id")
        stripe.Subscription.modify(
            stripe_subscription_id,
            items=[{"id": item_id, "price": price_id}],
            proration_behavior="create_prorations",
        )
        myprint(f"Subscription {stripe_subscription_id} upgraded to tier '{tier}'")
        return True
    except Exception as e:
        myprint(f"Could not upgrade subscription {stripe_subscription_id} to '{tier}': {e}")
        return False


def get_subscription_tier_from_price(price_id: str) -> Optional[str]:
    """Reverse-map a Stripe price ID back to our tier name."""
    for tier, pid in TIER_PRICE_MAP.items():
        if pid and pid == price_id:
            return tier
    return None


# Stripe subscription status → our DB status
_STRIPE_STATUS_MAP: dict[str, str] = {
    "active": "active",
    "trialing": "trial",
    "past_due": "past_due",
    "unpaid": "past_due",
    "canceled": "cancelled",
    "incomplete": "inactive",
    "incomplete_expired": "inactive",
    "paused": "inactive",
}


def stripe_status_to_db(stripe_status: str) -> str:
    """Convert a Stripe subscription status string to our DB enum value."""
    return _STRIPE_STATUS_MAP.get(stripe_status, "inactive")


def fetch_subscription(stripe_subscription_id: str) -> Optional[dict]:
    """Retrieve a Stripe subscription object. Returns None on failure."""
    if not STRIPE_API_KEY:
        return None
    stripe = _get_stripe()
    try:
        sub = stripe.Subscription.retrieve(stripe_subscription_id)
        if isinstance(sub, dict):
            return sub
        # Stripe SDK v5+ returns StripeObject (not a dict); str() serializes it
        # to JSON, allowing callers to use plain .get() field access.
        return json.loads(str(sub))
    except Exception as e:
        myprint(f"Could not fetch Stripe subscription {stripe_subscription_id}: {e}")
        return None


# ---------------------------------------------------------------------------
# Avatar credit packages (one-time payments)
# ---------------------------------------------------------------------------

AVATAR_CREDIT_PACKAGES: dict[str, dict] = {
    "starter": {"credits": 1,  "amount_cents": 500,  "label": "1 training"},
    "value":   {"credits": 3,  "amount_cents": 1000, "label": "3 trainings — save 33%"},
    "pro":     {"credits": 8,  "amount_cents": 2500, "label": "8 trainings — save 37%"},
    "max":     {"credits": 15, "amount_cents": 4000, "label": "15 trainings — save 47%"},
}


def create_avatar_credits_checkout(
    stripe_customer_id: str,
    package: str,
    success_url: str,
    cancel_url: str,
) -> Optional[str]:
    """Create a one-time Stripe Checkout session for avatar training credits.
    Uses inline price_data so no Stripe products need to be pre-created.
    Returns the checkout URL, or None on failure.
    """
    pkg = AVATAR_CREDIT_PACKAGES.get(package)
    if not pkg:
        myprint(f"Unknown avatar credit package '{package}'")
        return None
    if not STRIPE_API_KEY:
        myprint("STRIPE_API_KEY not set — cannot create avatar credits checkout")
        return None

    stripe = _get_stripe()
    try:
        session = stripe.checkout.Session.create(
            customer=stripe_customer_id,
            payment_method_types=["card"],
            line_items=[
                {
                    "price_data": {
                        "currency": "usd",
                        "unit_amount": pkg["amount_cents"],
                        "product_data": {
                            "name": f"Avatar Training Credits — {pkg['label']}",
                            "description": (
                                f"Use these credits to train a personalized AI avatar "
                                f"on Replicate. Each credit = 1 training job."
                            ),
                        },
                    },
                    "quantity": 1,
                }
            ],
            mode="payment",
            success_url=success_url,
            cancel_url=cancel_url,
            metadata={
                "type": "avatar_credits",
                "package": package,
                "credits": str(pkg["credits"]),
            },
        )
        return session.url
    except Exception as e:
        myprint(f"Avatar credits checkout failed for customer={stripe_customer_id}: {e}")
        return None


# Premium video credits (~$2/credit). 1 credit = 1 veo3.1_fast video; veo3.1 = 3 credits.
VIDEO_CREDIT_PACKAGES: dict[str, dict] = {
    "small":  {"credits": 5,   "amount_cents": 1200,  "label": "5 premium videos"},
    "medium": {"credits": 15,  "amount_cents": 3000,  "label": "15 premium videos — save 17%"},
    "large":  {"credits": 40,  "amount_cents": 7000,  "label": "40 premium videos — save 27%"},
    "max":    {"credits": 100, "amount_cents": 15000, "label": "100 premium videos — save 38%"},
}


def create_video_credits_checkout(
    stripe_customer_id: str,
    package: str,
    success_url: str,
    cancel_url: str,
) -> Optional[str]:
    """Create a one-time Stripe Checkout session for premium video credits.
    Uses inline price_data so no Stripe products need to be pre-created.
    Returns the checkout URL, or None on failure.
    """
    pkg = VIDEO_CREDIT_PACKAGES.get(package)
    if not pkg:
        myprint(f"Unknown video credit package '{package}'")
        return None
    if not STRIPE_API_KEY:
        myprint("STRIPE_API_KEY not set — cannot create video credits checkout")
        return None

    stripe = _get_stripe()
    try:
        session = stripe.checkout.Session.create(
            customer=stripe_customer_id,
            payment_method_types=["card"],
            line_items=[
                {
                    "price_data": {
                        "currency": "usd",
                        "unit_amount": pkg["amount_cents"],
                        "product_data": {
                            "name": f"Premium Video Credits — {pkg['label']}",
                            "description": (
                                "Use these credits to generate premium AI videos "
                                "(Veo realism + native audio). 1 credit per premium video."
                            ),
                        },
                    },
                    "quantity": 1,
                }
            ],
            mode="payment",
            success_url=success_url,
            cancel_url=cancel_url,
            metadata={
                "type": "video_credits",
                "package": package,
                "credits": str(pkg["credits"]),
            },
        )
        return session.url
    except Exception as e:
        myprint(f"Video credits checkout failed for customer={stripe_customer_id}: {e}")
        return None


def get_checkout_session_by_payment_intent(payment_intent_id: str) -> Optional[dict]:
    """Return the first Checkout Session associated with a PaymentIntent, or None."""
    if not STRIPE_API_KEY:
        return None
    stripe = _get_stripe()
    try:
        sessions = stripe.checkout.Session.list(payment_intent=payment_intent_id, limit=1)
        data = sessions.get("data", [])
        return data[0] if data else None
    except Exception as e:
        myprint(f"Could not look up checkout session for payment_intent={payment_intent_id}: {e}")
        return None
