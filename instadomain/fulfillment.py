"""Fulfillment orchestrator for post-payment domain registration flow.

Sequence:
1. Register domain at OpenSRS with placeholder nameservers
2. Create Cloudflare zone -> get real nameservers
3. Update OpenSRS nameservers to Cloudflare's (non-fatal if fails)
4. Create scoped DNS token at Cloudflare
5. Encrypt token, mark order complete

Compensating transactions:
- OpenSRS registration fails -> auto-refund via Stripe, mark failed
- Cloudflare zone/token fails after registration -> mark dns_pending and retry
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from instadomain.emails import (
    send_crypto_refund_alert,
    send_refund_email,
)
from instadomain.orders import get_order, update_order_status
from instadomain.stripe_handler import issue_refund

# Re-export for existing imports and test monkeypatching
from instadomain.fulfillment_dns import (  # noqa: F401
    _schedule_dns_retry,
    complete_dns_setup as _complete_dns_setup,
    retry_dns_setup,
)
from instadomain.emails import send_purchase_success_email  # noqa: F401

logger = logging.getLogger(__name__)

# Placeholder nameservers used for initial registration before CF zone exists
_PLACEHOLDER_NS = ["ns1.instadomain.dev", "ns2.instadomain.dev"]


async def _send_refund_email_if_possible(order: dict) -> None:
    email = order.get("email")
    if not email:
        return
    domain = (
        f"{order['domain']}.{order['tld']}"
        if "." not in order["domain"]
        else order["domain"]
    )
    await send_refund_email(
        to_email=email,
        domain=domain,
        amount_cents=order.get("amount_cents"),
    )


async def fulfill_order(
    *, pool, order_id: str, opensrs, cloudflare, encryption_key: str,
) -> dict:
    """Orchestrate domain registration and DNS setup after payment.

    Args:
        pool: asyncpg connection pool
        order_id: the order to fulfill (must be in 'registering' status)
        opensrs: OpenSRSClient instance (sync methods)
        cloudflare: CloudflareClient instance (async methods)
        encryption_key: Fernet key for encrypting the CF DNS token

    Returns:
        The final order dict.
    """
    order = await get_order(pool, order_id)
    if order is None:
        raise ValueError(f"Order {order_id} not found")

    if order["status"] != "registering":
        raise ValueError(
            f"Order {order_id} is in status '{order['status']}', expected 'registering'"
        )

    domain = (
        f"{order['domain']}.{order['tld']}"
        if "." not in order["domain"]
        else order["domain"]
    )
    email = order.get("email") or "privacy@instadomain.dev"
    payment_intent = order.get("stripe_payment_intent")

    # Parse registrant contact from order (stored as JSONB)
    registrant_contact = order.get("registrant_contact")
    if isinstance(registrant_contact, str):
        import json as _json
        registrant_contact = _json.loads(registrant_contact)

    # Reject orders missing registrant contact
    if registrant_contact is None:
        logger.error("Order %s has no registrant contact, cannot register", order_id)
        return await update_order_status(
            pool, order_id, "failed",
            error_msg="Missing registrant contact information",
        )

    # Step 1: Register domain at OpenSRS
    try:
        reg_result = await asyncio.to_thread(
            opensrs.register,
            domain=domain,
            years=order.get("registration_years", 1),
            registrant_email=email,
            nameservers=_PLACEHOLDER_NS,
            registrant_contact=registrant_contact,
        )
    except Exception as exc:
        logger.error("OpenSRS registration failed for %s: %s", domain, exc)
        return await _handle_registration_failure(
            pool=pool, order_id=order_id, order=order,
            domain=domain, error_msg=f"OpenSRS registration failed: {exc}",
            payment_intent=payment_intent,
        )

    # Step 2: Transition to setting_dns and create Cloudflare zone
    expiry_str = reg_result.get("expiry")
    expiry_dt = None
    if expiry_str:
        try:
            expiry_dt = datetime.fromisoformat(expiry_str).replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            logger.warning("Could not parse expiry date: %s", expiry_str)

    await update_order_status(
        pool, order_id, "setting_dns",
        opensrs_order_id=reg_result.get("order_id", ""),
        domain_expires_at=expiry_dt,
    )
    return await _complete_dns_setup(
        pool=pool,
        order=await get_order(pool, order_id),
        opensrs=opensrs,
        cloudflare=cloudflare,
        encryption_key=encryption_key,
    )


async def _handle_registration_failure(
    *, pool, order_id: str, order: dict, domain: str,
    error_msg: str, payment_intent: str | None,
) -> dict:
    """Handle OpenSRS registration failure: refund and mark failed."""
    is_crypto = order.get("payment_method") == "x402"

    if is_crypto:
        logger.critical(
            "Registration failed AFTER crypto payment for order %s domain %s",
            order_id, domain,
        )
        amount_usdc = f"{order.get('amount_cents', 0) / 100:.2f}"
        try:
            await send_crypto_refund_alert(
                order_id=order_id, domain=domain, amount_usdc=amount_usdc,
                payer_address=order.get("x402_payer_address"),
                tx_hash=order.get("x402_tx_hash"),
                error_msg=error_msg,
            )
        except Exception as alert_exc:
            logger.error(
                "Failed to send crypto refund alert for %s: %s",
                order_id, alert_exc,
            )
    elif payment_intent:
        try:
            await asyncio.to_thread(issue_refund, payment_intent)
            logger.info("Refund issued for payment_intent=%s", payment_intent)
            await _send_refund_email_if_possible(order)
        except Exception as refund_exc:
            logger.error("Refund failed for %s: %s", payment_intent, refund_exc)

    return await update_order_status(
        pool, order_id, "failed", error_msg=error_msg,
    )
