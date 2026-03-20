from __future__ import annotations

import stripe

from instadomain.config import Settings

settings = Settings()


def create_checkout_session(
    domain: str,
    amount_cents: int,
    order_id: str,
) -> dict:
    """Create a Stripe Checkout session for a domain purchase.

    Returns dict with session_id, checkout_url, and payment_intent.
    """
    client = stripe.StripeClient(settings.stripe_secret_key)
    session = client.v1.checkout.sessions.create(
        params={
            "mode": "payment",
            "payment_method_types": ["card"],
            "line_items": [
                {
                    "price_data": {
                        "currency": "usd",
                        "unit_amount": amount_cents,
                        "product_data": {
                            "name": f"Domain: {domain}",
                            "description": f"1-year domain registration for {domain}",
                        },
                    },
                    "quantity": 1,
                }
            ],
            "metadata": {
                "order_id": order_id,
                "domain": domain,
            },
            "billing_address_collection": "required",
            "success_url": "https://instadomain.fly.dev/success?session_id={CHECKOUT_SESSION_ID}",
            "cancel_url": "https://instadomain.fly.dev/cancel",
            "expires_at": _expires_at_24h(),
        }
    )
    return {
        "session_id": session.id,
        "checkout_url": session.url,
        "payment_intent": session.payment_intent,
    }


def verify_webhook(payload: bytes, sig_header: str) -> dict:
    """Verify a Stripe webhook signature and return the event.

    Raises stripe.SignatureVerificationError on invalid signatures.
    """
    event = stripe.Webhook.construct_event(
        payload,
        sig_header,
        settings.stripe_webhook_secret,
    )
    return event


def process_webhook_event(event: dict) -> dict | None:
    """Extract relevant data from a Stripe webhook event.

    Returns session_id, payment_intent, and email for
    checkout.session.completed events. Returns None for other types.
    """
    if event.get("type") != "checkout.session.completed":
        return None

    session = event["data"]["object"]
    return {
        "session_id": session["id"],
        "payment_intent": session["payment_intent"],
        "email": session.get("customer_details", {}).get("email"),
    }


def issue_refund(payment_intent_id: str) -> dict:
    """Issue a full refund for a payment intent.

    Returns dict with refund_id and status.
    """
    client = stripe.StripeClient(settings.stripe_secret_key)
    refund = client.v1.refunds.create(
        params={"payment_intent": payment_intent_id}
    )
    return {
        "refund_id": refund.id,
        "status": refund.status,
    }


def _expires_at_24h() -> int:
    """Return a UNIX timestamp 24 hours from now."""
    import time

    return int(time.time()) + 86400
