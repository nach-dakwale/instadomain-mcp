"""Domain management routes: status, transfer, unlock, renewal, expiration."""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Header, HTTPException, Query, Request

from instadomain.config import Settings
from instadomain.domain_helpers import _get_price
from instadomain.emails import send_expiration_reminder_email
from instadomain.encryption import decrypt
from instadomain.orders import get_expiring_orders, get_order
from instadomain.pricing import calculate_retail_cents, format_price
from instadomain.stripe_handler import create_checkout_session

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/status/{order_id}")
async def get_order_status(order_id: str, request: Request):
    """Get order status. Decrypts and returns the Cloudflare token if present."""
    pool = request.app.state.pool
    encryption_key = request.app.state.encryption_key

    order = await get_order(pool, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")

    response = {
        "order_id": order["id"],
        "domain": f"{order['domain']}.{order['tld']}",
        "status": order["status"],
        "amount_cents": order["amount_cents"],
        "created_at": (
            order["created_at"].isoformat() if order.get("created_at") else None
        ),
        "completed_at": (
            order["completed_at"].isoformat() if order.get("completed_at") else None
        ),
        "nameservers": order.get("nameservers"),
        "error_msg": order.get("error_msg"),
    }

    if order["status"] == "complete" and order.get("cloudflare_api_token"):
        try:
            token = decrypt(order["cloudflare_api_token"], encryption_key)
            response["cloudflare_token"] = token
            response["nameservers"] = order.get("nameservers")
        except Exception as exc:
            logger.error("Token decryption failed for %s: %s", order_id, exc)

    return response


@router.post("/renew/{order_id}")
async def renew_domain(order_id: str, request: Request):
    """Create a Stripe checkout session to renew a domain for 1 year."""
    pool = request.app.state.pool
    order = await get_order(pool, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    if order["status"] != "complete":
        raise HTTPException(
            status_code=400,
            detail=(
                f"Order must be in 'complete' status to renew "
                f"(current: {order['status']})"
            ),
        )

    domain = f"{order['domain']}.{order['tld']}"
    opensrs = request.app.state.opensrs

    try:
        wholesale_cents = await _get_price(domain, opensrs)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Price lookup failed: {exc}")

    tld = order["tld"]
    retail_cents = calculate_retail_cents(wholesale_cents, tld)

    try:
        checkout = await asyncio.to_thread(
            create_checkout_session, f"{domain} (renewal)", retail_cents, order_id
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Payment setup failed: {exc}")

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE orders SET renewal_stripe_session_id = $1, "
            "updated_at = now() WHERE id = $2",
            checkout["session_id"],
            order_id,
        )

    return {
        "order_id": order_id,
        "checkout_url": checkout["checkout_url"],
        "price_cents": retail_cents,
        "price_display": format_price(retail_cents),
        "domain": domain,
        "renewal_years": 1,
    }


@router.get("/expiring")
async def get_expiring(
    request: Request,
    days: int = Query(default=90, ge=1, le=365),
    x_api_key: str | None = Header(default=None),
):
    """Return all orders with domains expiring within the given days."""
    admin_key = os.environ.get("INSTADOMAIN_ADMIN_KEY", "")
    if not admin_key or x_api_key != admin_key:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")

    pool = request.app.state.pool
    orders = await get_expiring_orders(pool, days)
    settings = Settings()

    results = []
    for order in orders:
        domain = f"{order['domain']}.{order['tld']}"
        expires_at = order.get("domain_expires_at")
        days_remaining = None
        if expires_at:
            delta = expires_at - datetime.now(timezone.utc)
            days_remaining = max(0, delta.days)
        results.append({
            "order_id": order["id"],
            "domain": domain,
            "email": order.get("email"),
            "expires_at": expires_at.isoformat() if expires_at else None,
            "days_remaining": days_remaining,
            "renewal_url": f"{settings.backend_url}/renew/{order['id']}",
        })

    return {"count": len(results), "orders": results}


@router.post("/admin/send-expiration-reminders")
async def send_expiration_reminders(
    request: Request,
    days: int = Query(default=90, ge=1, le=365),
    x_api_key: str | None = Header(default=None),
):
    """Send expiration reminder emails for orders expiring within the given days."""
    admin_key = os.environ.get("INSTADOMAIN_ADMIN_KEY", "")
    if not admin_key or x_api_key != admin_key:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")

    pool = request.app.state.pool
    orders = await get_expiring_orders(pool, days)
    settings = Settings()

    sent = 0
    errors: list[dict[str, str]] = []

    for order in orders:
        domain = f"{order['domain']}.{order['tld']}"
        expires_at = order.get("domain_expires_at")
        days_remaining = None
        if expires_at:
            delta = expires_at - datetime.now(timezone.utc)
            days_remaining = max(0, delta.days)

        try:
            await send_expiration_reminder_email(
                to_email=order.get("email"),
                domain=domain,
                expires_at=expires_at.isoformat() if expires_at else "unknown",
                days_remaining=days_remaining or 0,
                renewal_url=f"{settings.backend_url}/renew/{order['id']}",
            )
            sent += 1
        except Exception as exc:
            logger.exception(
                "Failed to send expiration reminder for %s", order["id"]
            )
            errors.append({
                "order_id": order["id"],
                "domain": domain,
                "error": str(exc),
            })

    return {"count": sent, "errors": errors}


