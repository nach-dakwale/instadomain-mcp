"""FastAPI backend for InstaDomain domain purchase flow."""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, field_validator
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from instadomain.config import Settings
from instadomain.db import init_pool, close_pool
from instadomain.encryption import decrypt
from instadomain.fulfillment import fulfill_order
from instadomain.opensrs_client import OpenSRSClient
from instadomain.cloudflare_client import CloudflareClient
from instadomain.orders import (
    create_order,
    get_order,
    get_order_by_stripe_session,
    get_pending_x402_order,
    update_order_status,
    update_x402_settlement,
)
from instadomain.pricing import calculate_retail_cents, format_price
from instadomain.stripe_handler import (
    create_checkout_session,
    verify_webhook,
    process_webhook_event,
)
from instadomain.affiliate import add_affiliate_links
from instadomain.mcp_server import mcp

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent / "static"


# ---------------------------------------------------------------------------
# Helpers that can be mocked in tests
# ---------------------------------------------------------------------------

async def _check_availability(domain: str) -> dict:
    """Check domain availability via RDAP lookup.

    Returns dict with 'available' (bool) and 'domain' (str).
    Abstracted so tests can mock without importing domain_lookup.py
    which has its own config module.
    """
    from domain_lookup import check_domain
    result = await check_domain(domain)
    return {"available": result.available, "domain": result.domain}


async def _get_price(domain: str, opensrs: OpenSRSClient) -> int:
    """Get wholesale price from OpenSRS. Abstracted for test mocking."""
    return await asyncio.to_thread(opensrs.get_price, domain)


async def _check_domains_rdap(domains: list[str]) -> list[dict]:
    """Check multiple domains via RDAP concurrently. Returns list of dicts."""
    from domain_lookup import check_domains
    results = await check_domains(domains)
    return [{"available": r.available, "domain": r.domain} for r in results]


# ---------------------------------------------------------------------------
# Domain suggestion patterns (ported from DomainCheckr)
# ---------------------------------------------------------------------------

_SUFFIXES = [
    ".com", "app.com", ".io", ".co", ".dev", ".ai",
    "hq.com", "ly.com", "hub.com", "lab.com",
]
_PREFIXES = ["get", "my", "try", "go", "the"]


def _generate_candidates(keyword: str) -> list[str]:
    """Generate domain name ideas from a keyword using prefix/suffix patterns."""
    kw = keyword.strip().lower().replace(" ", "")
    candidates: list[str] = []

    for suffix in _SUFFIXES:
        candidates.append(f"{kw}{suffix}")

    for prefix in _PREFIXES:
        candidates.append(f"{prefix}{kw}.com")

    seen: set[str] = set()
    unique: list[str] = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            unique.append(c)

    return unique[:15]


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class BulkCheckRequest(BaseModel):
    domains: list[str]

    @field_validator("domains")
    @classmethod
    def max_fifty(cls, v: list[str]) -> list[str]:
        if len(v) > 50:
            raise ValueError("Maximum 50 domains per request")
        return v


class BuyRequest(BaseModel):
    domain: str


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""

    # Each app gets its own limiter instance to avoid shared state in tests
    app_limiter = Limiter(key_func=get_remote_address)

    # Build MCP HTTP app (needs lifespan composed with ours)
    mcp_http_app = mcp.http_app(path="/", transport="streamable-http")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Initialize DB pool and clients on startup; clean up on shutdown."""
        settings = Settings()
        app.state.pool = await init_pool(settings.database_url)
        app.state.opensrs = OpenSRSClient(
            api_key=settings.opensrs_api_key,
            reseller_username=settings.opensrs_reseller_username,
            api_url=settings.opensrs_api_url,
        )
        app.state.cloudflare = CloudflareClient(
            api_token=settings.cloudflare_api_token,
            account_id=settings.cloudflare_account_id,
        )
        app.state.encryption_key = settings.encryption_key

        # x402 crypto payments: only enabled when wallet address is configured
        app.state.x402_enabled = False
        if settings.x402_wallet_address:
            from x402.http import HTTPFacilitatorClient, FacilitatorConfig
            from x402 import x402ResourceServer
            from x402.mechanisms.evm.exact import ExactEvmServerScheme
            facilitator = HTTPFacilitatorClient(FacilitatorConfig(url=settings.x402_facilitator_url))
            resource_server = x402ResourceServer(facilitator_clients=[facilitator])
            resource_server.register(settings.x402_network, ExactEvmServerScheme())
            resource_server.initialize()
            app.state.x402_resource_server = resource_server
            app.state.x402_wallet = settings.x402_wallet_address
            app.state.x402_network = settings.x402_network
            app.state.x402_enabled = True
            logger.info("x402 crypto payments enabled (wallet=%s)", settings.x402_wallet_address[:10] + "...")

        async with mcp_http_app.lifespan(mcp_http_app):
            yield
        await app.state.cloudflare.close()
        await close_pool()

    app = FastAPI(title="InstaDomain", lifespan=lifespan)
    app.state.limiter = app_limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    # Mount MCP server for remote access (Streamable HTTP)
    app.mount("/mcp", mcp_http_app)

    # ------------------------------------------------------------------
    # Routes
    # ------------------------------------------------------------------

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/success", response_class=HTMLResponse)
    async def success():
        return "<html><body><h1>Domain purchase successful!</h1><p>You can close this tab. Your AI assistant is tracking the order.</p></body></html>"

    @app.get("/cancel", response_class=HTMLResponse)
    async def cancel():
        return "<html><body><h1>Purchase cancelled</h1><p>No charge was made. You can close this tab.</p></body></html>"

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return (_STATIC_DIR / "index.html").read_text()

    @app.get("/llms.txt")
    async def llms_txt():
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse((_STATIC_DIR / "llms.txt").read_text())

    @app.get("/.well-known/mcp.json")
    async def mcp_json():
        from fastapi.responses import JSONResponse
        import json
        return JSONResponse(json.loads((_STATIC_DIR / "mcp.json").read_text()))

    @app.get("/terms", response_class=HTMLResponse)
    async def terms():
        return (_STATIC_DIR / "terms.html").read_text()

    @app.get("/privacy", response_class=HTMLResponse)
    async def privacy():
        return (_STATIC_DIR / "privacy.html").read_text()

    @app.get("/check/{domain}")
    @app_limiter.limit("30/minute")
    async def check_domain_endpoint(domain: str, request: Request):
        """Check domain availability and return pricing."""
        result = await _check_availability(domain)
        response = {
            "domain": result["domain"],
            "available": result["available"],
        }
        if result["available"]:
            try:
                wholesale_cents = await _get_price(domain, request.app.state.opensrs)
                tld = domain.rsplit(".", 1)[-1] if "." in domain else "com"
                retail_cents = calculate_retail_cents(wholesale_cents, tld)
                response["price_cents"] = retail_cents
                response["price_display"] = format_price(retail_cents)
                response["wholesale_cents"] = wholesale_cents
            except Exception as exc:
                logger.warning("Price lookup failed for %s: %s", domain, exc)
                response["price_cents"] = None
                response["price_display"] = None
        return response

    @app.post("/check")
    @app_limiter.limit("10/minute")
    async def check_bulk(body: BulkCheckRequest, request: Request):
        """Check availability of up to 50 domains via RDAP (no pricing)."""
        settings = Settings()
        raw_results = await _check_domains_rdap(body.domains)
        enriched = [add_affiliate_links(r, settings) for r in raw_results]
        available = [r for r in enriched if r.get("available")]
        taken = [r for r in enriched if not r.get("available")]
        return {
            "summary": {
                "total": len(enriched),
                "available": len(available),
                "taken": len(taken),
            },
            "available": available,
            "taken": taken,
        }

    @app.get("/suggest")
    @app_limiter.limit("10/minute")
    async def suggest(request: Request, keyword: str = Query(..., min_length=1, description="Keyword to build domain ideas from")):
        """Generate and check domain suggestions for a keyword."""
        settings = Settings()
        candidates = _generate_candidates(keyword)
        raw_results = await _check_domains_rdap(candidates)
        enriched = [add_affiliate_links(r, settings) for r in raw_results]
        available = [r for r in enriched if r.get("available")]
        taken = [r for r in enriched if not r.get("available")]
        return {
            "keyword": keyword,
            "candidates_checked": len(enriched),
            "summary": {
                "available": len(available),
                "taken": len(taken),
            },
            "available": available,
            "taken": taken,
        }

    @app.post("/buy")
    @app_limiter.limit("5/minute")
    async def buy_domain(body: BuyRequest, request: Request):
        """Create a Stripe checkout session and order for a domain purchase."""
        domain = body.domain.strip().lower()

        # Check availability
        result = await _check_availability(domain)
        if not result["available"]:
            raise HTTPException(status_code=400, detail=f"Domain {domain} is not available")

        # Get price
        pool = request.app.state.pool
        opensrs = request.app.state.opensrs
        try:
            wholesale_cents = await _get_price(domain, opensrs)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Price lookup failed: {exc}")

        tld = domain.rsplit(".", 1)[-1] if "." in domain else "com"
        retail_cents = calculate_retail_cents(wholesale_cents, tld)

        # Create order in DB
        order = await create_order(
            pool,
            domain=domain.rsplit(".", 1)[0] if "." in domain else domain,
            tld=tld,
            amount_cents=retail_cents,
            wholesale_cents=wholesale_cents,
            stripe_session_id=None,
        )

        # Create Stripe checkout session (sync SDK — run in thread)
        try:
            checkout = await asyncio.to_thread(
                create_checkout_session, domain, retail_cents, order["id"]
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Payment setup failed: {exc}")

        # Update order with Stripe session ID
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

    @app.get("/status/{order_id}")
    @app_limiter.limit("60/minute")
    async def get_order_status(order_id: str, request: Request):
        """Get order status. Decrypts CF token on first retrieval."""
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
            "created_at": order["created_at"].isoformat() if order.get("created_at") else None,
            "completed_at": order["completed_at"].isoformat() if order.get("completed_at") else None,
            "nameservers": order.get("nameservers"),
            "error_msg": order.get("error_msg"),
        }

        # If complete and token exists, attempt atomic first-retrieval
        if order["status"] == "complete" and order.get("cloudflare_api_token"):
            if not order.get("cloudflare_token_retrieved"):
                # Atomic: only succeeds if token hasn't been retrieved yet
                async with pool.acquire() as conn:
                    row = await conn.fetchrow(
                        "UPDATE orders SET cloudflare_token_retrieved = TRUE "
                        "WHERE id = $1 AND cloudflare_token_retrieved = FALSE "
                        "RETURNING cloudflare_api_token",
                        order["id"],
                    )
                if row:
                    try:
                        token = decrypt(row["cloudflare_api_token"], encryption_key)
                        response["cloudflare_token"] = token
                        response["nameservers"] = order.get("nameservers")
                    except Exception as exc:
                        logger.error("Token decryption failed for %s: %s", order_id, exc)
                else:
                    response["cloudflare_token"] = "already_retrieved"
            else:
                response["cloudflare_token"] = "already_retrieved"

        return response

    @app.post("/webhooks/stripe")
    async def stripe_webhook(request: Request):
        """Handle Stripe webhook events."""
        pool = request.app.state.pool
        payload = await request.body()
        sig_header = request.headers.get("stripe-signature", "")

        # Verify webhook signature
        try:
            event = verify_webhook(payload, sig_header)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Invalid signature: {exc}")

        # Process the event
        data = process_webhook_event(event)
        if data is None:
            return {"received": True}

        session_id = data["session_id"]
        payment_intent = data.get("payment_intent")
        email = data.get("email")

        # Find the order
        order = await get_order_by_stripe_session(pool, session_id)
        if order is None:
            logger.warning("Webhook: no order found for session %s", session_id)
            return {"received": True}

        # Idempotency: skip if already past pending_payment
        if order["status"] != "pending_payment":
            logger.info("Webhook: order %s already in status %s, skipping", order["id"], order["status"])
            return {"received": True}

        # Transition to registering
        try:
            await update_order_status(
                pool, order["id"], "registering",
                stripe_payment_intent=payment_intent,
                email=email,
            )
        except ValueError as exc:
            logger.error("Webhook: transition failed for %s: %s", order["id"], exc)
            return {"received": True, "error": str(exc)}

        # Kick off fulfillment in background
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

    # ------------------------------------------------------------------
    # x402 crypto payment endpoints
    # ------------------------------------------------------------------

    # USDC on Base mainnet
    _USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

    @app.post("/buy/crypto")
    @app_limiter.limit("5/minute")
    async def buy_domain_crypto(body: BuyRequest, request: Request):
        """Create an x402 order. Returns a pay_url the agent hits to pay via HTTP 402."""
        if not request.app.state.x402_enabled:
            raise HTTPException(status_code=503, detail="Crypto payments not configured")

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
            payment_method="x402",
        )

        # Convert cents to USDC (6 decimals, 1:1 with USD)
        price_usdc = f"{retail_cents / 100:.2f}"

        settings = Settings()
        return {
            "order_id": order["id"],
            "pay_url": f"/pay/{order['id']}",
            "price_usdc": price_usdc,
            "price_cents": retail_cents,
            "price_display": format_price(retail_cents),
            "network": settings.x402_network,
            "asset": _USDC_BASE,
        }

    @app.get("/pay/{order_id}")
    async def pay_x402(order_id: str, request: Request):
        """x402-paywalled endpoint. Agents pay by including X-PAYMENT header."""
        if not request.app.state.x402_enabled:
            raise HTTPException(status_code=503, detail="Crypto payments not configured")

        pool = request.app.state.pool
        rs = request.app.state.x402_resource_server

        # Look up the pending order
        order = await get_pending_x402_order(pool, order_id)
        if order is None:
            # Check if it exists at all
            existing = await get_order(pool, order_id)
            if existing is None:
                raise HTTPException(status_code=404, detail="Order not found")
            if existing.get("payment_method") != "x402":
                raise HTTPException(status_code=400, detail="Not an x402 order")
            raise HTTPException(status_code=409, detail="Order already paid or expired")

        # Build payment requirements for this order's exact price
        from x402 import ResourceConfig
        price_usd = f"{order['amount_cents'] / 100:.2f}"
        config = ResourceConfig(
            scheme="exact",
            pay_to=request.app.state.x402_wallet,
            price=price_usd,
            network=request.app.state.x402_network,
        )
        requirements_list = rs.build_payment_requirements(config)

        # If no X-PAYMENT header, return 402 with payment requirements
        payment_header = request.headers.get("x-payment")
        if not payment_header:
            pay_required = rs.create_payment_required_response(requirements_list)
            return JSONResponse(
                status_code=402,
                content=pay_required.model_dump(),
                headers={"X-PAYMENT-REQUIREMENTS": pay_required.model_dump_json()},
            )

        # Parse and verify the payment
        from x402 import parse_payment_payload
        try:
            payload = parse_payment_payload(json.loads(payment_header))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Invalid payment payload: {exc}")

        # Find matching requirements for this payment
        matched = rs.find_matching_requirements(requirements_list, payload)
        if matched is None:
            raise HTTPException(status_code=400, detail="Payment does not match requirements")

        verify_result = rs.verify_payment(payload, matched)
        if not verify_result.is_valid:
            raise HTTPException(
                status_code=402,
                detail=f"Payment verification failed: {verify_result.invalid_message}",
            )

        # Payment verified: transition order to registering
        try:
            await update_order_status(pool, order_id, "registering")
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

        # Settle the payment on-chain
        settle_result = rs.settle_payment(payload, matched)
        if settle_result.success and settle_result.transaction:
            await update_x402_settlement(pool, order_id, settle_result.transaction)

        # Kick off fulfillment (same path as Stripe webhook)
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
        headers = {}
        if settle_result.success:
            headers["X-PAYMENT-RESPONSE"] = settle_result.model_dump_json()

        return JSONResponse(content=response_body, headers=headers)

    return app
