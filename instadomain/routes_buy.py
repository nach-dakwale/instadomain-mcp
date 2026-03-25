"""Purchase routes: Stripe checkout and webhook handling."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from instadomain.domain_helpers import _check_availability, _get_price
from instadomain.emails import send_renewal_failure_alert
from instadomain.fulfillment import fulfill_order
from instadomain.models import BuyRequest
from instadomain.orders import (
    create_order,
    get_order_by_stripe_session,
    update_domain_expiry,
    update_order_status,
)
from instadomain.pricing import calculate_retail_cents, format_price
from instadomain.stripe_handler import (
    create_checkout_session,
    issue_refund,
    process_webhook_event,
    verify_webhook,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/buy")
async def buy_domain(body: BuyRequest, request: Request):
    """Create a Stripe checkout session and order for a domain purchase."""
    domain = body.domain.strip().lower()

    result = await _check_availability(domain)
    if not result["available"]:
        raise HTTPException(status_code=400, detail=f"Domain {domain} is not available")

    pool = request.app.state.pool
    opensrs = request.app.state.opensrs
    try:
        wholesale_cents = await _get_price(domain, opensrs)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Price lookup failed: {exc}")

    tld = domain.rsplit(".", 1)[-1] if "." in domain else "com"
    retail_cents = calculate_retail_cents(wholesale_cents, tld)

    order = await create_order(
        pool,
        domain=domain.rsplit(".", 1)[0] if "." in domain else domain,
        tld=tld,
        amount_cents=retail_cents,
        wholesale_cents=wholesale_cents,
        stripe_session_id=None,
        registrant_contact=body.registrant.model_dump(),
    )

    try:
        checkout = await asyncio.to_thread(
            create_checkout_session, domain, retail_cents, order["id"]
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Payment setup failed: {exc}")

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE orders SET stripe_session_id = $1 WHERE id = $2",
            checkout["session_id"],
            order["id"],
        )

    return {
        "order_id": order["id"],
        "checkout_url": checkout["checkout_url"],
        "price_cents": retail_cents,
        "price_display": format_price(retail_cents),
    }


@router.post("/webhooks/stripe")
async def stripe_webhook(request: Request):
    """Handle Stripe webhook events."""
    pool = request.app.state.pool
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    try:
        event = verify_webhook(payload, sig_header)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid signature: {exc}")

    data = process_webhook_event(event)
    if data is None:
        return {"received": True}

    session_id = data["session_id"]
    payment_intent = data.get("payment_intent")
    email = data.get("email")

    order = await get_order_by_stripe_session(pool, session_id)
    if order is None:
        logger.warning("Webhook: no order found for session %s", session_id)
        event_created = event.get("created", 0)
        event_age_seconds = datetime.now(timezone.utc).timestamp() - event_created
        if event_age_seconds < 300:
            return JSONResponse(
                status_code=500,
                content={"error": "Order not found yet, retrying"},
            )
        return {"received": True}

    # Handle renewal payments: order is already "complete"
    if order["status"] == "complete":
        return await _handle_renewal_webhook(
            request, pool, order, session_id, payment_intent
        )

    # Idempotency: skip if already past pending_payment
    if order["status"] != "pending_payment":
        logger.info(
            "Webhook: order %s already in status %s, skipping",
            order["id"], order["status"],
        )
        return {"received": True}

    try:
        await update_order_status(
            pool, order["id"], "registering",
            stripe_payment_intent=payment_intent,
            email=email,
        )
    except ValueError as exc:
        logger.error("Webhook: transition failed for %s: %s", order["id"], exc)
        return {"received": True, "error": str(exc)}

    asyncio.create_task(
        fulfill_order(
            pool=pool,
            order_id=order["id"],
            opensrs=request.app.state.opensrs,
            cloudflare=request.app.state.cloudflare,
            encryption_key=request.app.state.encryption_key,
        )
    )

    return {"received": True, "order_id": order["id"]}


async def _handle_renewal_webhook(
    request: Request,
    pool,
    order: dict,
    session_id: str,
    payment_intent: str | None,
) -> dict:
    """Process a Stripe webhook for a domain renewal payment."""
    logger.info("Webhook: processing renewal for order %s", order["id"])

    if order.get("renewal_stripe_session_id") != session_id:
        logger.info(
            "Webhook: renewal session %s does not match stored %s, skipping (duplicate)",
            session_id, order.get("renewal_stripe_session_id"),
        )
        return {
            "received": True, "order_id": order["id"],
            "renewal": True, "duplicate": True,
        }

    domain = f"{order['domain']}.{order['tld']}"
    opensrs = request.app.state.opensrs

    expires_at = order.get("domain_expires_at")
    if expires_at is None:
        logger.error("Renewal failed for %s: domain_expires_at is NULL", domain)
        if payment_intent:
            try:
                await asyncio.to_thread(issue_refund, payment_intent)
            except Exception as refund_exc:
                logger.error("Refund also failed for %s: %s", payment_intent, refund_exc)
        asyncio.create_task(send_renewal_failure_alert(
            order_id=order["id"], domain=domain,
            error_msg="domain_expires_at is NULL, could not determine currentexpirationyear",
        ))
        return {
            "received": True, "order_id": order["id"],
            "renewal": True, "error": "missing expiry",
        }

    current_expiry_year = expires_at.year
    try:
        renew_result = await asyncio.to_thread(
            opensrs.renew_domain, domain, current_expiry_year, 1
        )
        expiry_str = renew_result.get("expiry")
        if expiry_str:
            try:
                expiry_dt = datetime.fromisoformat(expiry_str).replace(
                    tzinfo=timezone.utc
                )
                await update_domain_expiry(pool, order["id"], expiry_dt)
            except (ValueError, TypeError):
                logger.warning("Could not parse renewal expiry: %s", expiry_str)

        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE orders SET renewal_stripe_session_id = NULL, "
                "updated_at = now() WHERE id = $1",
                order["id"],
            )
        logger.info("Renewal successful for %s", domain)
    except Exception as exc:
        logger.error("Renewal failed for %s: %s", domain, exc)
        if payment_intent:
            try:
                await asyncio.to_thread(issue_refund, payment_intent)
            except Exception as refund_exc:
                logger.error("Refund also failed for %s: %s", payment_intent, refund_exc)
        asyncio.create_task(send_renewal_failure_alert(
            order_id=order["id"], domain=domain, error_msg=str(exc),
        ))
    return {"received": True, "order_id": order["id"], "renewal": True}
