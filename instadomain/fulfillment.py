"""Fulfillment orchestrator for post-payment domain registration flow.

Sequence:
1. Register domain at OpenSRS with placeholder nameservers
2. Create Cloudflare zone -> get real nameservers
3. Update OpenSRS nameservers to Cloudflare's (non-fatal if fails)
4. Create scoped DNS token at Cloudflare
5. Encrypt token, mark order complete

Compensating transactions:
- OpenSRS registration fails -> auto-refund via Stripe, mark failed
- Cloudflare zone fails after registration -> mark failed (domain registered, no refund)
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from instadomain.encryption import encrypt
from instadomain.orders import get_order, update_order_status
from instadomain.stripe_handler import issue_refund

logger = logging.getLogger(__name__)

# Placeholder nameservers used for initial registration before CF zone exists
_PLACEHOLDER_NS = ["ns1.instadomain.dev", "ns2.instadomain.dev"]


async def fulfill_order(
    *,
    pool,
    order_id: str,
    opensrs,
    cloudflare,
    encryption_key: str,
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

    domain = f"{order['domain']}.{order['tld']}" if "." not in order["domain"] else order["domain"]
    email = order.get("email") or "privacy@instadomain.dev"
    payment_intent = order.get("stripe_payment_intent")

    # Step 1: Register domain at OpenSRS with placeholder nameservers
    # OpenSRS client is sync — run in thread to avoid blocking the event loop
    try:
        reg_result = await asyncio.to_thread(
            opensrs.register,
            domain=domain,
            years=order.get("registration_years", 1),
            registrant_email=email,
            nameservers=_PLACEHOLDER_NS,
        )
    except Exception as exc:
        logger.error("OpenSRS registration failed for %s: %s", domain, exc)
        # Compensate: issue refund (sync — run in thread)
        if payment_intent:
            try:
                await asyncio.to_thread(issue_refund, payment_intent)
                logger.info("Refund issued for payment_intent=%s", payment_intent)
            except Exception as refund_exc:
                logger.error("Refund failed for %s: %s", payment_intent, refund_exc)

        return await update_order_status(
            pool, order_id, "failed",
            error_msg=f"OpenSRS registration failed: {exc}",
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

    try:
        zone_result = await cloudflare.create_zone(domain)
        zone_id = zone_result["zone_id"]
        nameservers = zone_result["nameservers"]
    except Exception as exc:
        logger.error("Cloudflare zone creation failed for %s: %s", domain, exc)
        # Domain is already registered, don't refund
        return await update_order_status(
            pool, order_id, "failed",
            error_msg=f"Cloudflare zone creation failed: {exc}",
        )

    # Step 3: Update nameservers at OpenSRS to point to Cloudflare
    # Non-fatal: domain works without this, CF will prompt user to update NS
    try:
        await asyncio.to_thread(opensrs.update_nameservers, domain, nameservers)
    except Exception as exc:
        logger.warning(
            "Nameserver update failed for %s (non-fatal): %s", domain, exc
        )

    # Step 4: Create scoped DNS token at Cloudflare
    try:
        dns_token = await cloudflare.create_dns_token(zone_id, domain)
    except Exception as exc:
        logger.error("DNS token creation failed for %s: %s", domain, exc)
        return await update_order_status(
            pool, order_id, "failed",
            error_msg=f"DNS token creation failed: {exc}",
        )

    # Step 5: Encrypt token and mark order complete
    encrypted_token = encrypt(dns_token, encryption_key)

    return await update_order_status(
        pool, order_id, "complete",
        cloudflare_zone_id=zone_id,
        cloudflare_api_token=encrypted_token,
        nameservers=nameservers,
    )
