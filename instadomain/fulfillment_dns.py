"""DNS setup retry logic for the fulfillment pipeline.

Handles Cloudflare zone creation, nameserver updates, DNS token creation,
and retry scheduling when any of those steps fail.
"""
from __future__ import annotations

import asyncio
import logging

from instadomain.emails import send_purchase_success_email
from instadomain.encryption import encrypt
from instadomain.orders import get_order, update_order_status

logger = logging.getLogger(__name__)

_DNS_RETRY_DELAYS_SECONDS = (30, 120, 300)


async def _send_dns_failure_alert(order_id: str, domain: str) -> None:
    """Alert the admin that DNS setup exhausted all retries."""
    from instadomain.emails import ALERT_EMAIL, _send_email_sync

    body = (
        f"DNS SETUP FAILED - MANUAL INTERVENTION REQUIRED\n\n"
        f"Order ID: {order_id}\n"
        f"Domain: {domain}\n\n"
        f"DNS setup failed after maximum retries. The domain is registered "
        f"at OpenSRS but Cloudflare zone/token setup could not be completed.\n\n"
        f"Action required: manually complete DNS setup or contact the customer."
    )
    await asyncio.to_thread(
        _send_email_sync,
        to_email=ALERT_EMAIL,
        subject=f"[ALERT] DNS setup failed: {domain} (order {order_id})",
        body=body,
    )


async def _handle_dns_retry_exhaustion(pool, order_id: str, domain: str) -> None:
    """Transition order to failed and send an alert when retries are exhausted."""
    try:
        await update_order_status(
            pool, order_id, "failed",
            error_msg="DNS setup failed after maximum retries",
        )
    except ValueError:
        logger.warning(
            "Could not transition %s to failed (already transitioned?)", order_id
        )
    try:
        await _send_dns_failure_alert(order_id, domain)
    except Exception as exc:
        logger.error("Failed to send DNS failure alert for %s: %s", order_id, exc)


def _schedule_dns_retry(
    *, pool, order_id: str, domain: str, opensrs, cloudflare,
    encryption_key: str, retry_count: int,
) -> None:
    if retry_count > len(_DNS_RETRY_DELAYS_SECONDS):
        logger.error(
            "DNS retry limit reached for %s, transitioning to failed", order_id
        )
        asyncio.create_task(_handle_dns_retry_exhaustion(pool, order_id, domain))
        return

    delay = _DNS_RETRY_DELAYS_SECONDS[retry_count - 1]
    asyncio.create_task(
        _retry_dns_setup_after_delay(
            pool=pool, order_id=order_id, opensrs=opensrs,
            cloudflare=cloudflare, encryption_key=encryption_key,
            delay_seconds=delay,
        )
    )


async def _retry_dns_setup_after_delay(
    *, pool, order_id: str, opensrs, cloudflare,
    encryption_key: str, delay_seconds: int,
) -> None:
    await asyncio.sleep(delay_seconds)
    try:
        await retry_dns_setup(
            pool=pool, order_id=order_id, opensrs=opensrs,
            cloudflare=cloudflare, encryption_key=encryption_key,
        )
    except Exception as exc:
        logger.error("Scheduled DNS retry failed for %s: %s", order_id, exc)


async def _mark_dns_pending(
    *, pool, order_id: str, domain: str, error_msg: str,
    opensrs, cloudflare, encryption_key: str, **fields,
) -> dict:
    async with pool.acquire() as conn:
        retry_count = await conn.fetchval(
            "SELECT retry_count FROM orders WHERE id = $1", order_id
        )
    if retry_count is None:
        raise ValueError(f"Order {order_id} not found")

    new_retry_count = retry_count + 1
    updated_order = await update_order_status(
        pool, order_id, "dns_pending",
        error_msg=error_msg, retry_count=new_retry_count, **fields,
    )
    _schedule_dns_retry(
        pool=pool, order_id=order_id, domain=domain,
        opensrs=opensrs, cloudflare=cloudflare,
        encryption_key=encryption_key, retry_count=new_retry_count,
    )
    return updated_order


async def complete_dns_setup(
    *, pool, order: dict, opensrs, cloudflare, encryption_key: str,
) -> dict:
    """Run the Cloudflare zone/token setup sequence for a domain order."""
    order_id = order["id"]
    domain = (
        f"{order['domain']}.{order['tld']}"
        if "." not in order["domain"]
        else order["domain"]
    )

    if order["status"] == "dns_pending":
        order = await update_order_status(
            pool, order_id, "setting_dns", error_msg=None
        )

    zone_id = order.get("cloudflare_zone_id")
    nameservers = order.get("nameservers")

    if not zone_id:
        try:
            existing_zone = await cloudflare.get_zone_by_name(domain)
            if existing_zone:
                logger.info(
                    "Found existing Cloudflare zone for %s: %s",
                    domain, existing_zone["zone_id"],
                )
                zone_id = existing_zone["zone_id"]
                nameservers = existing_zone["nameservers"]
            else:
                zone_result = await cloudflare.create_zone(domain)
                zone_id = zone_result["zone_id"]
                nameservers = zone_result["nameservers"]
        except Exception as exc:
            logger.error("Cloudflare zone creation failed for %s: %s", domain, exc)
            return await _mark_dns_pending(
                pool=pool, order_id=order_id, domain=domain,
                error_msg=f"Cloudflare zone creation failed: {exc}",
                opensrs=opensrs, cloudflare=cloudflare,
                encryption_key=encryption_key,
            )

        order = await update_order_status(
            pool, order_id, "setting_dns",
            cloudflare_zone_id=zone_id, nameservers=nameservers, error_msg=None,
        )

    # Non-fatal: update nameservers at registrar
    if nameservers:
        try:
            await asyncio.to_thread(opensrs.unlock_domain, domain)
            await asyncio.to_thread(opensrs.update_nameservers, domain, nameservers)
        except Exception as exc:
            logger.warning(
                "Nameserver update failed for %s (non-fatal): %s", domain, exc
            )
        finally:
            try:
                await asyncio.to_thread(opensrs.lock_domain, domain)
            except Exception as exc:
                logger.warning(
                    "Re-lock failed for %s (non-fatal): %s", domain, exc
                )

    # Create or reuse DNS token
    dns_token = None
    current_order = await get_order(pool, order_id)
    if current_order and current_order.get("cloudflare_api_token"):
        logger.info(
            "Order %s already has a Cloudflare token, skipping creation", order_id
        )
        encrypted_token = current_order["cloudflare_api_token"]
        from instadomain.encryption import decrypt
        try:
            dns_token = decrypt(encrypted_token, encryption_key)
        except Exception:
            pass
    else:
        try:
            dns_token = await cloudflare.create_dns_token(zone_id, domain)
        except Exception as exc:
            logger.error("DNS token creation failed for %s: %s", domain, exc)
            return await _mark_dns_pending(
                pool=pool, order_id=order_id, domain=domain,
                error_msg=f"DNS token creation failed: {exc}",
                opensrs=opensrs, cloudflare=cloudflare,
                encryption_key=encryption_key,
                cloudflare_zone_id=zone_id, nameservers=nameservers,
            )
        encrypted_token = encrypt(dns_token, encryption_key)

    completed_order = await update_order_status(
        pool, order_id, "complete",
        cloudflare_zone_id=zone_id, cloudflare_api_token=encrypted_token,
        nameservers=nameservers, error_msg=None,
    )

    await send_purchase_success_email(
        to_email=completed_order.get("email"),
        domain=domain, dns_token=dns_token, nameservers=nameservers or [],
    )
    return completed_order


async def retry_dns_setup(
    *, pool, order_id: str, opensrs, cloudflare, encryption_key: str,
) -> dict:
    """Retry DNS setup for an order whose domain is already registered."""
    order = await get_order(pool, order_id)
    if order is None:
        raise ValueError(f"Order {order_id} not found")
    if order["status"] not in {"setting_dns", "dns_pending"}:
        logger.info(
            "Order %s is in status '%s', skipping DNS retry",
            order_id, order["status"],
        )
        return order
    return await complete_dns_setup(
        pool=pool, order=order, opensrs=opensrs,
        cloudflare=cloudflare, encryption_key=encryption_key,
    )
