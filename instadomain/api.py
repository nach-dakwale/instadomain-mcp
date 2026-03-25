"""FastAPI backend for InstaDomain domain purchase flow.

This module contains the app factory and lifespan. Route handlers live in
the routes_* modules; models live in models.py; domain lookup helpers
live in domain_helpers.py.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from instadomain.cloudflare_client import CloudflareClient
from instadomain.config import Settings
from instadomain.db import init_pool, close_pool
from instadomain.mcp_server import mcp
from instadomain.opensrs_client import OpenSRSClient
from instadomain.orders import get_stuck_dns_orders

# Re-export models so existing imports from instadomain.api keep working
from instadomain.models import BulkCheckRequest, BuyRequest, RegistrantContact  # noqa: F401

# Re-export helpers/names that tests monkeypatch on this module
from instadomain.domain_helpers import (  # noqa: F401
    _check_availability,
    _check_domains_rdap,
    _generate_candidates,
    _get_price,
)
from instadomain.encryption import decrypt  # noqa: F401
from instadomain.orders import (  # noqa: F401
    get_expiring_orders,
    get_order,
    get_pending_x402_order,
)
from instadomain.emails import send_expiration_reminder_email  # noqa: F401

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""

    app_limiter = Limiter(key_func=get_remote_address)
    mcp_http_app = mcp.http_app(path="/", transport="streamable-http")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Initialize DB pool and clients on startup; clean up on shutdown."""
        settings = Settings()
        app.state.pool = await init_pool(settings.database_url)
        opensrs_url = settings.opensrs_api_url
        opensrs_key = settings.opensrs_api_key
        if settings.x402_testnet:
            opensrs_url = settings.opensrs_test_api_url
            if settings.opensrs_test_api_key:
                opensrs_key = settings.opensrs_test_api_key
        app.state.opensrs = OpenSRSClient(
            api_key=opensrs_key,
            reseller_username=settings.opensrs_reseller_username,
            api_url=opensrs_url,
        )
        app.state.cloudflare = CloudflareClient(
            api_token=settings.cloudflare_api_token,
            account_id=settings.cloudflare_account_id,
        )
        app.state.encryption_key = settings.encryption_key

        _configure_x402(app, settings)

        # Recover orders stuck in dns_pending/setting_dns from before restart
        from instadomain.fulfillment import retry_dns_setup
        stuck_orders = await get_stuck_dns_orders(app.state.pool)
        if stuck_orders:
            logger.info(
                "Recovering %d stuck DNS orders on startup", len(stuck_orders)
            )
        for stuck_order in stuck_orders:
            asyncio.create_task(
                retry_dns_setup(
                    pool=app.state.pool,
                    order_id=stuck_order["id"],
                    opensrs=app.state.opensrs,
                    cloudflare=app.state.cloudflare,
                    encryption_key=app.state.encryption_key,
                )
            )

        async with mcp_http_app.lifespan(mcp_http_app):
            yield
        await app.state.cloudflare.close()
        await close_pool()

    app = FastAPI(title="InstaDomain", lifespan=lifespan)
    app.state.limiter = app_limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    # Mount MCP server for remote access (Streamable HTTP)
    app.mount("/mcp", mcp_http_app)

    # Include route modules
    from instadomain.routes_static import router as static_router
    from instadomain.routes_check import router as check_router
    from instadomain.routes_buy import router as buy_router
    from instadomain.routes_x402 import router as x402_router
    from instadomain.routes_manage import router as manage_router

    app.include_router(static_router)
    app.include_router(check_router)
    app.include_router(buy_router)
    app.include_router(x402_router)
    app.include_router(manage_router)

    return app


def _configure_x402(app: FastAPI, settings: Settings) -> None:
    """Set up x402 crypto payment state on the app."""
    app.state.x402_enabled = False
    app.state.x402_usdc_address = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

    if not settings.x402_wallet_address:
        return

    from x402.http import HTTPFacilitatorClient, FacilitatorConfig
    from x402 import x402ResourceServer
    from x402.mechanisms.evm.exact import ExactEvmServerScheme

    x402_network = settings.x402_network
    x402_facilitator_url = settings.x402_facilitator_url
    if settings.x402_testnet:
        x402_network = "eip155:84532"
        x402_facilitator_url = "https://x402.org/facilitator"
        app.state.x402_usdc_address = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
        logger.info("x402 TESTNET mode active (Base Sepolia)")
    else:
        app.state.x402_usdc_address = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

    facilitator = HTTPFacilitatorClient(FacilitatorConfig(url=x402_facilitator_url))
    resource_server = x402ResourceServer(facilitator_clients=[facilitator])
    resource_server.register(x402_network, ExactEvmServerScheme())
    resource_server.initialize()
    app.state.x402_resource_server = resource_server
    app.state.x402_wallet = settings.x402_wallet_address
    app.state.x402_network = x402_network
    app.state.x402_enabled = True
    app.state.x402_testnet = settings.x402_testnet
    logger.info(
        "x402 crypto payments enabled (wallet=%s)",
        settings.x402_wallet_address[:10] + "...",
    )
