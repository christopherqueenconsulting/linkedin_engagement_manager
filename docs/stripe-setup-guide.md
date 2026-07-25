# Stripe Billing Setup Guide

LEM uses Stripe for subscription management. This guide covers everything from creating products to wiring up webhooks — for both local development and production.

---

## Why not Stripe CLI inside Docker?

A common instinct is to run `stripe listen` inside the Docker stack. Don't — it generates a **new ephemeral signing secret on every startup**, making it impossible to pre-configure `STRIPE_WEBHOOK_SECRET` in `.env`. The approach below gives you a **fixed, reusable secret** that survives restarts.

---

## Step 1 — Create a Stripe account and get your API key

1. Sign up at [stripe.com](https://stripe.com) (test mode is free and has no card requirements).
2. In the Dashboard: **Developers** → **API keys**
3. Copy the **Secret key**:
   - Test: `sk_test_...` (safe for local dev — no real charges)
   - Live: `sk_live_...` (use only when you go to production)

```env
STRIPE_API_KEY=sk_test_...
```

---

## Step 2 — Create subscription products and prices

1. Dashboard → **Product catalog** → **+ Add product**
2. Create three products:

| Product name | Price | Billing |
|---|---|---|
| LEM Starter | $29 | Monthly recurring |
| LEM Professional | $79 | Monthly recurring |
| LEM Enterprise | $199 | Monthly recurring |

3. After saving each product, click the price row and copy its **Price ID** (`price_1Abc...`):

```env
STRIPE_PRICE_ID_STARTER=price_1Abc...
STRIPE_PRICE_ID_PROFESSIONAL=price_1Def...
STRIPE_PRICE_ID_ENTERPRISE=price_1Ghi...
```

> These two steps are enough to make the **Upgrade Your Plan** buttons work and redirect users to Stripe Checkout. The webhook (Step 3) is required for the subscription to activate in LEM's database after payment.

---

## Step 3 — Register the webhook endpoint

The webhook endpoint (`/api/billing/webhook`) tells LEM when a subscription has been created, upgraded, or cancelled. Register it once in the Stripe Dashboard and you get a fixed signing secret that never changes.

### Local development (ngrok)

LEM already runs ngrok to expose the local server. Use the same domain for Stripe that you registered for LinkedIn OAuth.

1. Start LEM (`./run.sh`) and note your ngrok URL, e.g. `https://your-domain.ngrok-free.app`
2. Dashboard → **Developers** → **Webhooks** → **+ Add endpoint**
3. **Endpoint URL**: use your **stable custom ngrok domain** (e.g. `https://your-custom-domain.ngrok-free.dev/api/billing/webhook`), NOT a temporary `ngrok-free.app` URL — those change on every restart and will break webhook delivery.
4. **Events to listen for** — select all five:
   - `customer.subscription.created`
   - `customer.subscription.updated`
   - `customer.subscription.deleted`
   - `invoice.payment_succeeded`
   - `invoice.payment_failed`
5. Click **Add endpoint**, then open the endpoint detail page
6. Copy the **Signing secret** (`whsec_...`):

```env
STRIPE_WEBHOOK_SECRET=whsec_...
```

> This secret is tied to the registered URL, not to any running process. It stays valid indefinitely as long as the endpoint registration exists in the Dashboard. You do not need to re-generate it when you restart Docker.

### Production

Same steps as above, using your production domain:

1. Dashboard → **Developers** → **Webhooks** → **+ Add endpoint**
2. **Endpoint URL**: `https://your-production-domain.com/api/billing/webhook`
3. Same three events as above
4. Copy the **Signing secret** and add it to your production environment

---

## Step 4 — Free trial setup (optional)

`FREE_TRIAL_DAYS` controls how many days a new user gets before they need to upgrade. Default is 14:

```env
FREE_TRIAL_DAYS=14
```

New users created via the email PIN flow are automatically assigned a trial subscription when their account is created.

### Early-adopter extended trial (issue #499)

Early adopters can trade a public review for a longer trial. `POST /api/trial/extend` grants it only
when the user has a `feedback` row with `source='review'`, and only while a cohort slot is free —
slots are claimed atomically, so the caps are hard caps. When both cohorts are full the user simply
keeps the standard `FREE_TRIAL_DAYS` trial.

```env
EARLY_ADOPTER_TRIAL_ENABLED=False   # off until you open the program
EARLY_ADOPTER_TRIAL_DAYS=60
EARLY_ADOPTER_P0_SLOTS=25           # first cohort; 0 closes it
EARLY_ADOPTER_P1_SLOTS=100          # fills after P0
EARLY_ADOPTER_COUPON_ID=            # optional Stripe coupon ID applied on conversion
```

When a grant holder converts, the days still left on the extension are carried into Checkout as
`trial_period_days` (plus the coupon, if set), so upgrading early doesn't forfeit the remaining
trial. Users without a grant convert exactly as before.

---

## Complete `.env` Stripe block

```env
STRIPE_API_KEY=sk_test_...
STRIPE_WEBHOOK_SECRET=whsec_...
STRIPE_PRICE_ID_STARTER=price_1Abc...
STRIPE_PRICE_ID_PROFESSIONAL=price_1Def...
STRIPE_PRICE_ID_ENTERPRISE=price_1Ghi...
FREE_TRIAL_DAYS=14
EARLY_ADOPTER_TRIAL_ENABLED=False
EARLY_ADOPTER_TRIAL_DAYS=60
EARLY_ADOPTER_P0_SLOTS=25
EARLY_ADOPTER_P1_SLOTS=100
EARLY_ADOPTER_COUPON_ID=
```

---

## End-to-end payment flow (what happens when a user upgrades)

```
User clicks "Upgrade Your Plan"
  → POST /api/billing/create-checkout-session
  → FastAPI creates a Stripe Checkout session (needs STRIPE_API_KEY + price IDs)
  → Browser redirects to stripe.com/checkout/...
  → User enters card details on Stripe's hosted page
  → Payment succeeds
  → Stripe redirects user back to /account?upgraded=1
  → Stripe also POSTs to /api/billing/webhook (needs STRIPE_WEBHOOK_SECRET)
  → FastAPI validates signature and updates user's subscription_status in MySQL
  → User now has an active subscription
```

---

## Switching from test to production

1. Replace `sk_test_...` with `sk_live_...` in `STRIPE_API_KEY`
2. Re-create the products and prices in Stripe's **live mode** (test-mode price IDs don't work in live mode)
3. Update `STRIPE_PRICE_ID_*` with the live price IDs
4. Register a new webhook endpoint using your production URL (live mode has separate webhooks from test mode)
5. Update `STRIPE_WEBHOOK_SECRET` with the live webhook signing secret

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Upgrade button returns "Could not start checkout" | `STRIPE_API_KEY` invalid, or `STRIPE_PRICE_ID_*` not set / set to placeholder values |
| Checkout redirects but subscription never activates | `STRIPE_WEBHOOK_SECRET` wrong or webhook endpoint not registered |
| Webhook returns 400 "Invalid Stripe webhook signature" | `STRIPE_WEBHOOK_SECRET` doesn't match the registered endpoint's signing secret |
| User sees "Billing not yet configured" | `stripe_customer_id` is null in DB — Stripe customer creation failed at sign-up time, likely due to missing `STRIPE_API_KEY` when the user first logged in |
