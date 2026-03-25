"""x402 crypto payment routes: order creation, payment verification, and settlement."""
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from instadomain.domain_helpers import _check_availability, _get_price
from instadomain.fulfillment import fulfill_order
from instadomain.models import BuyRequest
from instadomain.orders import (
    create_order,
    get_order,
    get_pending_x402_order,
    update_order_status,
    update_x402_settlement,
)
from instadomain.pricing import calculate_retail_cents, format_price

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/buy/crypto")
async def buy_domain_crypto(body: BuyRequest, request: Request):
    """Create an x402 order. Returns a pay_url the agent hits to pay via HTTP 402."""
    if not request.app.state.x402_enabled:
        raise HTTPException(status_code=503, detail="Crypto payments not configured")

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
        logger.info(
            "Pre-validation failed: %s shows available via RDAP but unavailable at registrar",
            domain,
        )
        raise HTTPException(
            status_code=409,
            detail=(
                f"Domain {domain} appeared available but the registrar reports it "
                "is not. This can happen with recently expired or reserved domains. "
                "No payment was created."
            ),
        )
    logger.info("Pre-validation passed for %s: registrar confirms available", domain)

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
        payment_method="x402",
        registrant_contact=body.registrant.model_dump(),
    )

    price_usdc = f"{retail_cents / 100:.2f}"

    return {
        "order_id": order["id"],
        "pay_url": f"/pay/{order['id']}",
        "price_usdc": price_usdc,
        "price_cents": retail_cents,
        "price_display": format_price(retail_cents),
        "network": request.app.state.x402_network,
        "asset": request.app.state.x402_usdc_address,
    }


@router.get("/pay/{order_id}")
async def pay_x402(order_id: str, request: Request):
    """x402-paywalled endpoint. Agents pay by including X-PAYMENT header."""
    if not request.app.state.x402_enabled:
        raise HTTPException(status_code=503, detail="Crypto payments not configured")

    pool = request.app.state.pool
    rs = request.app.state.x402_resource_server

    existing = await get_order(pool, order_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Order not found")
    if existing.get("payment_method") != "x402":
        raise HTTPException(status_code=400, detail="Not an x402 order")
    if existing["status"] != "pending_payment":
        raise HTTPException(
            status_code=409,
            detail=(
                f"Order {order_id} is in status '{existing['status']}' "
                "and cannot be paid again"
            ),
        )

    order = await get_pending_x402_order(pool, order_id)
    if order is None:
        raise HTTPException(status_code=409, detail="Order already paid or expired")

    from x402 import ResourceConfig
    price_usd = f"{order['amount_cents'] / 100:.2f}"
    config = ResourceConfig(
        scheme="exact",
        pay_to=request.app.state.x402_wallet,
        price=price_usd,
        network=request.app.state.x402_network,
    )
    requirements_list = rs.build_payment_requirements(config)

    payment_header = request.headers.get("x-payment")
    if not payment_header:
        pay_required = rs.create_payment_required_response(requirements_list)
        return JSONResponse(
            status_code=402,
            content=pay_required.model_dump(),
            headers={"X-PAYMENT-REQUIREMENTS": pay_required.model_dump_json()},
        )

    from x402 import parse_payment_payload
    try:
        payload = parse_payment_payload(json.loads(payment_header))
    except Exception as exc:
        raise HTTPException(
            status_code=400, detail=f"Invalid payment payload: {exc}"
        )

    matched = rs.find_matching_requirements(requirements_list, payload)
    if matched is None:
        raise HTTPException(
            status_code=400, detail="Payment does not match requirements"
        )

    verify_result = await rs.verify_payment(payload, matched)
    if not verify_result.is_valid:
        raise HTTPException(
            status_code=402,
            detail=f"Payment verification failed: {verify_result.invalid_message}",
        )

    settle_result = await rs.settle_payment(payload, matched)
    if not settle_result.success:
        await update_order_status(
            pool, order_id, "failed",
            error_msg=f"Settlement failed: {getattr(settle_result, 'error', 'unknown')}",
        )
        raise HTTPException(
            status_code=402,
            detail=f"Payment settlement failed: {getattr(settle_result, 'error', 'unknown')}",
        )

    if settle_result.transaction:
        payer_address = (
            getattr(settle_result, "payer", None)
            or getattr(verify_result, "payer", None)
        )
        await update_x402_settlement(
            pool, order_id, settle_result.transaction, payer_address=payer_address,
        )

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

    response_body = {"status": "paid", "order_id": order_id}
    headers = {"X-PAYMENT-RESPONSE": settle_result.model_dump_json()}

    return JSONResponse(content=response_body, headers=headers)
