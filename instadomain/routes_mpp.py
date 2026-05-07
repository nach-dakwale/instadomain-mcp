"""MPP (Machine Payments Protocol) routes.

Two endpoints:

* ``POST /buy/mpp`` creates a pending order for a domain and returns the
  paywalled pay URL the agent should hit with an MPP-enabled HTTP client.
* ``GET /pay/mpp/{order_id}`` is the paywalled endpoint. Returns HTTP 402
  with a ``WWW-Authenticate`` challenge until the agent retries with a
  Shared Payment Token in the ``Authorization`` header. The token is then
  charged via Stripe and fulfillment is kicked off.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from mpp import Challenge

from instadomain.domain_helpers import _check_availability, _get_price
from instadomain.fulfillment import fulfill_order
from instadomain.models import BuyRequest
from instadomain.mpp_handler import build_mpp_server
from instadomain.orders import (
    create_order,
    get_order,
    get_pending_mpp_order,
    update_order_status,
)
from instadomain.pricing import calculate_retail_cents, format_price

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/buy/mpp")
async def buy_domain_mpp(body: BuyRequest, request: Request):
    """Create an MPP order. Returns a pay_url an agent hits with an SPT."""
    if not request.app.state.mpp_enabled:
        raise HTTPException(status_code=503, detail="MPP payments not configured")

    domain = body.domain.strip().lower()
    result = await _check_availability(domain)
    if not result["available"]:
        raise HTTPException(
            status_code=400, detail=f"Domain {domain} is not available"
        )

    pool = request.app.state.pool
    opensrs = request.app.state.opensrs

    try:
        registrar_available = await asyncio.to_thread(
            opensrs.check_availability, domain
        )
    except Exception as exc:
        logger.warning("OpenSRS availability check failed for %s: %s", domain, exc)
        raise HTTPException(
            status_code=502,
            detail=f"Could not verify domain availability with registrar: {exc}",
        )
    if not registrar_available:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Domain {domain} appeared available but the registrar reports it "
                "is not. No payment was created."
            ),
        )

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
        payment_method="mpp",
        registrant_contact=body.registrant.model_dump(),
    )

    return {
        "order_id": order["id"],
        "pay_url": f"/pay/mpp/{order['id']}",
        "price_cents": retail_cents,
        "price_display": format_price(retail_cents),
        "network_id": request.app.state.mpp_network_id,
        "payment_method_types": request.app.state.mpp_payment_method_types,
    }


@router.get("/pay/mpp/{order_id}")
async def pay_mpp(order_id: str, request: Request):
    """MPP paywalled endpoint: returns 402 challenge or charges the SPT."""
    if not request.app.state.mpp_enabled:
        raise HTTPException(status_code=503, detail="MPP payments not configured")

    pool = request.app.state.pool
    existing = await get_order(pool, order_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Order not found")
    if existing.get("payment_method") != "mpp":
        raise HTTPException(status_code=400, detail="Not an MPP order")
    if existing["status"] != "pending_payment":
        raise HTTPException(
            status_code=409,
            detail=(
                f"Order {order_id} is in status '{existing['status']}' "
                "and cannot be paid again"
            ),
        )

    order = await get_pending_mpp_order(pool, order_id)
    if order is None:
        raise HTTPException(status_code=409, detail="Order already paid or expired")

    mpp = build_mpp_server(
        stripe_secret_key=request.app.state.stripe_secret_key,
        network_id=request.app.state.mpp_network_id,
        secret_key=request.app.state.mpp_secret_key,
        realm=request.app.state.mpp_realm,
        payment_method_types=request.app.state.mpp_payment_method_types,
    )

    amount_dollars = f"{order['amount_cents'] / 100:.2f}"
    domain_full = f"{order['domain']}.{order['tld']}"

    try:
        result = await mpp.charge(
            authorization=request.headers.get("authorization"),
            amount=amount_dollars,
            description=f"Domain registration: {domain_full}",
            extra={"order_id": order_id, "domain": domain_full},
        )
    except Exception as exc:
        logger.warning("MPP charge failed for %s: %s", order_id, exc)
        raise HTTPException(status_code=402, detail=f"Payment failed: {exc}")

    if isinstance(result, Challenge):
        return JSONResponse(
            status_code=402,
            content={"error": "Payment required", "order_id": order_id},
            headers={"WWW-Authenticate": result.to_www_authenticate(mpp.realm)},
        )

    _credential, receipt = result

    try:
        await update_order_status(pool, order_id, "registering")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    asyncio.create_task(
        fulfill_order(
            pool=pool,
            order_id=order_id,
            opensrs=request.app.state.opensrs,
            cloudflare=request.app.state.cloudflare,
            encryption_key=request.app.state.encryption_key,
        )
    )

    return JSONResponse(
        content={"status": "paid", "order_id": order_id},
        headers={"Authentication-Info": receipt.to_payment_receipt()},
    )
