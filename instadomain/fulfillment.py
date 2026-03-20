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

from instadomain.emails import send_crypto_refund_alert, send_purchase_success_email, send_refund_email
from instadomain.encryption import encrypt
from instadomain.orders import get_order, update_order_status
from instadomain.stripe_handler import issue_refund

logger = logging.getLogger(__name__)

# Placeholder nameservers used for initial registration before CF zone exists
_PLACEHOLDER_NS = ["ns1.instadomain.dev", "ns2.instadomain.dev"]
_DNS_RETRY_DELAYS_SECONDS = (30, 120, 300)


async def _increment_retry_count(pool, order_id: str) -> int:
    async with pool.acquire() as conn:
        retry_count = await conn.fetchval(
            "UPDATE orders SET retry_count = retry_count + 1, updated_at = now() "
            "WHERE id = $1 RETURNING retry_count",
            order_id,
        )
    if retry_count is None:
        raise ValueError(f"Order {order_id} not found")
    return retry_count


def _schedule_dns_retry(
    *,
    pool,
    order_id: str,
    opensrs,
    cloudflare,
    encryption_key: str,
    retry_count: int,
) -> None:
    if retry_count > len(_DNS_RETRY_DELAYS_SECONDS):
        logger.error("DNS retry limit reached for %s", order_id)
        return

    delay = _DNS_RETRY_DELAYS_SECONDS[retry_count - 1]
    asyncio.create_task(
        _retry_dns_setup_after_delay(
            pool=pool,
            order_id=order_id,
            opensrs=opensrs,
            cloudflare=cloudflare,
            encryption_key=encryption_key,
            delay_seconds=delay,
        )
    )


async def _retry_dns_setup_after_delay(
    *,
    pool,
    order_id: str,
    opensrs,
    cloudflare,
    encryption_key: str,
    delay_seconds: int,
) -> None:
    await asyncio.sleep(delay_seconds)
    try:
        await retry_dns_setup(
            pool=pool,
            order_id=order_id,
            opensrs=opensrs,
            cloudflare=cloudflare,
            encryption_key=encryption_key,
        )
    except Exception as exc:
        logger.error("Scheduled DNS retry failed for %s: %s", order_id, exc)


async def _mark_dns_pending(
    *,
    pool,
    order_id: str,
    error_msg: str,
    opensrs,
    cloudflare,
    encryption_key: str,
    **fields,
) -> dict:
    retry_count = await _increment_retry_count(pool, order_id)
    updated_order = await update_order_status(
        pool,
        order_id,
        "dns_pending",
        error_msg=error_msg,
        **fields,
    )
    _schedule_dns_retry(
        pool=pool,
        order_id=order_id,
        opensrs=opensrs,
        cloudflare=cloudflare,
        encryption_key=encryption_key,
        retry_count=retry_count,
    )
    return updated_order


async def _send_refund_email_if_possible(order: dict) -> None:
    email = order.get("email")
    if not email:
        return
    domain = f"{order['domain']}.{order['tld']}" if "." not in order["domain"] else order["domain"]
    await send_refund_email(
        to_email=email,
        domain=domain,
        amount_cents=order.get("amount_cents"),
    )


async def _complete_dns_setup(
    *,
    pool,
    order: dict,
    opensrs,
    cloudflare,
    encryption_key: str,
) -> dict:
    order_id = order["id"]
    domain = f"{order['domain']}.{order['tld']}" if "." not in order["domain"] else order["domain"]

    if order["status"] == "dns_pending":
        order = await update_order_status(pool, order_id, "setting_dns", error_msg=None)

    zone_id = order.get("cloudflare_zone_id")
    nameservers = order.get("nameservers")

    if not zone_id:
        try:
            zone_result = await cloudflare.create_zone(domain)
            zone_id = zone_result["zone_id"]
            nameservers = zone_result["nameservers"]
        except Exception as exc:
            logger.error("Cloudflare zone creation failed for %s: %s", domain, exc)
            return await _mark_dns_pending(
                pool=pool,
                order_id=order_id,
                error_msg=f"Cloudflare zone creation failed: {exc}",
                opensrs=opensrs,
                cloudflare=cloudflare,
                encryption_key=encryption_key,
            )

        order = await update_order_status(
            pool,
            order_id,
            "setting_dns",
            cloudflare_zone_id=zone_id,
            nameservers=nameservers,
            error_msg=None,
        )

    # Non-fatal: nameservers may already be applied, but retrying is harmless.
    if nameservers:
        try:
            await asyncio.to_thread(opensrs.update_nameservers, domain, nameservers)
        except Exception as exc:
            logger.warning(
                "Nameserver update failed for %s (non-fatal): %s", domain, exc
            )

    try:
        dns_token = await cloudflare.create_dns_token(zone_id, domain)
    except Exception as exc:
        logger.error("DNS token creation failed for %s: %s", domain, exc)
        return await _mark_dns_pending(
            pool=pool,
            order_id=order_id,
            error_msg=f"DNS token creation failed: {exc}",
            opensrs=opensrs,
            cloudflare=cloudflare,
            encryption_key=encryption_key,
            cloudflare_zone_id=zone_id,
            nameservers=nameservers,
        )

    encrypted_token = encrypt(dns_token, encryption_key)
    completed_order = await update_order_status(
        pool,
        order_id,
        "complete",
        cloudflare_zone_id=zone_id,
        cloudflare_api_token=encrypted_token,
        nameservers=nameservers,
        error_msg=None,
    )
    await send_purchase_success_email(
        to_email=completed_order.get("email"),
        domain=domain,
        dns_token=dns_token,
        nameservers=nameservers or [],
    )
    return completed_order


async def retry_dns_setup(
    *,
    pool,
    order_id: str,
    opensrs,
    cloudflare,
    encryption_key: str,
) -> dict:
    """Retry DNS setup for an order whose domain is already registered."""
    order = await get_order(pool, order_id)
    if order is None:
        raise ValueError(f"Order {order_id} not found")
    if order["status"] not in {"setting_dns", "dns_pending"}:
        raise ValueError(
            f"Order {order_id} is in status '{order['status']}', expected DNS retry state"
        )
    return await _complete_dns_setup(
        pool=pool,
        order=order,
        opensrs=opensrs,
        cloudflare=cloudflare,
        encryption_key=encryption_key,
    )


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

    # Parse registrant contact from order (stored as JSONB)
    registrant_contact = order.get("registrant_contact")
    if isinstance(registrant_contact, str):
        import json as _json
        registrant_contact = _json.loads(registrant_contact)

    # Reject orders missing registrant contact (e.g. pre-migration orders).
    # Falling back to DEFAULT_CONTACT would register in InstaDomain's name,
    # violating ICANN registrant accuracy requirements.
    if registrant_contact is None:
        logger.error("Order %s has no registrant contact, cannot register", order_id)
        return await update_order_status(
            pool, order_id, "failed",
            error_msg="Missing registrant contact information",
        )

    # Step 1: Register domain at OpenSRS with placeholder nameservers
    # OpenSRS client is sync -- run in thread to avoid blocking the event loop
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
        error_msg = f"OpenSRS registration failed: {exc}"
        is_crypto = order.get("payment_method") == "x402"

        if is_crypto:
            # Crypto payments are irreversible. Alert for manual refund.
            logger.critical(
                "Registration failed AFTER crypto payment for order %s domain %s",
                order_id, domain,
            )
            amount_usdc = f"{order.get('amount_cents', 0) / 100:.2f}"
            try:
                await send_crypto_refund_alert(
                    order_id=order_id,
                    domain=domain,
                    amount_usdc=amount_usdc,
                    payer_address=order.get("x402_payer_address"),
                    tx_hash=order.get("x402_tx_hash"),
                    error_msg=error_msg,
                )
            except Exception as alert_exc:
                logger.error("Failed to send crypto refund alert for %s: %s", order_id, alert_exc)
        elif payment_intent:
            # Stripe: issue automatic refund
            try:
                await asyncio.to_thread(issue_refund, payment_intent)
                logger.info("Refund issued for payment_intent=%s", payment_intent)
                await _send_refund_email_if_possible(order)
            except Exception as refund_exc:
                logger.error("Refund failed for %s: %s", payment_intent, refund_exc)

        return await update_order_status(
            pool, order_id, "failed",
            error_msg=error_msg,
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
