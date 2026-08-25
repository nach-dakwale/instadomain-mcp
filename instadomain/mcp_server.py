"""InstaDomain MCP server -- thin HTTP client over the backend API.

Tools are split across modules:
  - This file: check_domain, check_domains_bulk, suggest_domains,
    get_domain_status, get_transfer_code, unlock_domain, renew_domain
  - mcp_tools_buy.py: buy_domain, buy_domain_crypto
"""
from __future__ import annotations

import asyncio
import os

import httpx
from pydantic import BaseModel, ConfigDict, TypeAdapter

from fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BACKEND_URL = os.environ.get("INSTADOMAIN_BACKEND_URL", "https://instadomain.fly.dev")
POLL_INTERVAL_SECONDS = 3
POLL_TIMEOUT_SECONDS = 120
BULK_LIMIT = 50

# ---------------------------------------------------------------------------
# Output contracts for read-only check/suggest tools
#
# These models describe only fields proven by every real backend path
# (instadomain/routes_check.py). Unknown upstream metadata passes through
# untyped via extra="allow" rather than being invented or dropped.
# ---------------------------------------------------------------------------


class _PassthroughModel(BaseModel):
    """Strictly validate guaranteed fields and tolerate unknown metadata."""

    model_config = ConfigDict(extra="allow", strict=True)


class CheckDomainResult(_PassthroughModel):
    """Shape of GET /check/{domain}.

    ``domain`` and ``available`` are guaranteed by every path. Price fields
    are absent when the domain is taken, null when pricing fails, and set
    only on the successful-price path.
    """

    domain: str
    available: bool
    price_cents: int | None = None
    price_display: str | None = None
    wholesale_cents: int | None = None


class DomainAvailabilityItem(_PassthroughModel):
    """Per-domain item from RDAP bulk checks (no pricing).

    Only ``domain`` and ``available`` are guaranteed; affiliate
    ``register_urls`` is attached solely to available domains when enabled.
    """

    domain: str
    available: bool
    register_urls: dict[str, str] | None = None


class BulkCheckSummary(_PassthroughModel):
    total: int
    available: int
    taken: int


class BulkCheckSuccess(_PassthroughModel):
    """Shape of POST /check (RDAP bulk availability)."""

    summary: BulkCheckSummary
    available: list[DomainAvailabilityItem]
    taken: list[DomainAvailabilityItem]


class BulkLimitError(_PassthroughModel):
    """Truthful error arm when the bulk input exceeds BULK_LIMIT."""

    error: str
    limit: int


class SuggestSummary(_PassthroughModel):
    available: int
    taken: int


class SuggestDomainsSuccess(_PassthroughModel):
    """Shape of GET /suggest. Note: no ``summary.total``."""

    keyword: str
    candidates_checked: int
    summary: SuggestSummary
    available: list[DomainAvailabilityItem]
    taken: list[DomainAvailabilityItem]


# FastMCP wraps union return annotations under structuredContent.result. The
# three tools historically returned their dictionaries at the top level, so
# supply explicit object-root schemas and keep the raw validated dictionaries
# unwrapped. Validation below is strict and fail-closed; returning the original
# object preserves absent versus explicitly-null upstream fields and legacy
# text/structured payload shape.
CHECK_DOMAIN_OUTPUT_SCHEMA = CheckDomainResult.model_json_schema()
BULK_CHECK_OUTPUT_SCHEMA = {
    "type": "object",
    **TypeAdapter(BulkCheckSuccess | BulkLimitError).json_schema(),
}
SUGGEST_DOMAINS_OUTPUT_SCHEMA = SuggestDomainsSuccess.model_json_schema()


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

mcp = FastMCP("instadomain")

# ---------------------------------------------------------------------------
# Tools: check and suggest
# ---------------------------------------------------------------------------


@mcp.tool(output_schema=CHECK_DOMAIN_OUTPUT_SCHEMA)
async def check_domain(domain: str) -> dict:
    """Check if a domain is available for purchase and get its price.

    Always call this before buying. After showing the price, ask the user
    two things before proceeding:
    1. Confirm they want to purchase at that price.
    2. Which payment method they prefer:
       - "card" / "Stripe" → call buy_domain (opens Stripe checkout in browser)
       - "crypto" / "USDC" / "x402" → call buy_domain_crypto (autonomous USDC payment,
         no browser; requires Coinbase Payments MCP or another x402 wallet)
       - "MPP" / "agent pay" → call buy_domain_mpp (Stripe agent payments via
         Shared Payment Token, no browser)
    If the user has Coinbase Payments MCP configured in their session, suggest
    crypto as the default. Otherwise default to buy_domain (Stripe).

    Args:
        domain: The full domain name to check (e.g. "coolstartup.com").

    Returns:
        Dict with availability status, price in cents, and formatted price.
    """
    async with httpx.AsyncClient(base_url=BACKEND_URL, timeout=15) as client:
        resp = await client.get(f"/check/{domain}")
        resp.raise_for_status()
        data = resp.json()
        CheckDomainResult.model_validate(data)
        return data


@mcp.tool(output_schema=BULK_CHECK_OUTPUT_SCHEMA)
async def check_domains_bulk(domains: list[str]) -> dict:
    """Check availability of up to 50 domain names in one call.

    Uses fast RDAP lookups (no pricing). Returns a summary with
    total/available/taken counts plus per-domain details.

    Args:
        domains: List of domain names to check (max 50).
    """
    if len(domains) > BULK_LIMIT:
        data = {
            "error": (
                f"Too many domains: {len(domains)} provided, maximum is {BULK_LIMIT}."
            ),
            "limit": BULK_LIMIT,
        }
        BulkLimitError.model_validate(data)
        return data

    async with httpx.AsyncClient(base_url=BACKEND_URL, timeout=30) as client:
        resp = await client.post("/check", json={"domains": domains})
        resp.raise_for_status()
        data = resp.json()
        BulkCheckSuccess.model_validate(data)
        return data


@mcp.tool(output_schema=SUGGEST_DOMAINS_OUTPUT_SCHEMA)
async def suggest_domains(keyword: str) -> dict:
    """Generate domain name ideas from a keyword and check their availability.

    Args:
        keyword: A keyword or short business name (e.g. "taskflow").
    """
    async with httpx.AsyncClient(base_url=BACKEND_URL, timeout=30) as client:
        resp = await client.get("/suggest", params={"keyword": keyword})
        resp.raise_for_status()
        data = resp.json()
        SuggestDomainsSuccess.model_validate(data)
        return data


# ---------------------------------------------------------------------------
# Tools: status and management
# ---------------------------------------------------------------------------


@mcp.tool()
async def get_domain_status(order_id: str) -> dict:
    """Get the status of a domain purchase order.

    Polls the backend every 3 seconds (up to 120 seconds) until the order
    reaches a terminal state (complete or failed).

    Args:
        order_id: The order ID returned from buy_domain (e.g. "ord_abc123").
    """
    terminal_statuses = {"complete", "failed", "expired"}
    deadline = asyncio.get_running_loop().time() + POLL_TIMEOUT_SECONDS

    async with httpx.AsyncClient(base_url=BACKEND_URL, timeout=15) as client:
        while True:
            resp = await client.get(f"/status/{order_id}")
            resp.raise_for_status()
            data = resp.json()

            if data.get("status") in terminal_statuses:
                return data

            if asyncio.get_running_loop().time() >= deadline:
                data["_poll_timeout"] = True
                data["_message"] = (
                    f"Order {order_id} did not reach a terminal state "
                    f"within {POLL_TIMEOUT_SECONDS}s. Current status: {data.get('status')}"
                )
                return data

            await asyncio.sleep(POLL_INTERVAL_SECONDS)


@mcp.tool()
async def request_transfer_code(order_id: str) -> dict:
    """Start the transfer verification flow by sending a code to the registrant's email.

    Always call this before get_transfer_code or unlock_domain.
    Then ask the user to check their email and provide the 6-digit code,
    then call verify_transfer_code to get a transfer_token.

    Args:
        order_id: The order ID of a completed domain purchase.
    """
    async with httpx.AsyncClient(base_url=BACKEND_URL, timeout=15) as client:
        resp = await client.post(f"/transfer/request-code/{order_id}")
        if resp.status_code in {400, 404}:
            return resp.json()
        resp.raise_for_status()
        return resp.json()


@mcp.tool()
async def verify_transfer_code(order_id: str, code: str) -> dict:
    """Verify the email code and get a transfer token valid for 15 minutes.

    Call this after request_transfer_code and the user provides their code.
    Pass the returned transfer_token to get_transfer_code or unlock_domain.

    Args:
        order_id: The order ID of a completed domain purchase.
        code: The 6-digit code from the verification email.
    """
    async with httpx.AsyncClient(base_url=BACKEND_URL, timeout=15) as client:
        resp = await client.post(
            f"/transfer/verify-code/{order_id}", json={"code": code}
        )
        if resp.status_code in {400, 404}:
            return resp.json()
        resp.raise_for_status()
        return resp.json()


@mcp.tool()
async def get_transfer_code(order_id: str, transfer_token: str) -> dict:
    """Get the EPP/transfer authorization code for a completed domain purchase.

    Requires a transfer_token from verify_transfer_code.

    Args:
        order_id: The order ID of a completed domain purchase.
        transfer_token: Token returned by verify_transfer_code.
    """
    async with httpx.AsyncClient(base_url=BACKEND_URL, timeout=15) as client:
        resp = await client.get(
            f"/transfer-code/{order_id}",
            headers={"x-transfer-token": transfer_token},
        )
        if resp.status_code in {400, 401, 404}:
            return resp.json()
        resp.raise_for_status()
        return resp.json()


@mcp.tool()
async def unlock_domain(order_id: str, transfer_token: str) -> dict:
    """Remove the registrar transfer lock. Requires a transfer_token from verify_transfer_code.

    Args:
        order_id: The order ID of a completed domain purchase.
        transfer_token: Token returned by verify_transfer_code.
    """
    async with httpx.AsyncClient(base_url=BACKEND_URL, timeout=15) as client:
        resp = await client.post(
            f"/unlock/{order_id}",
            headers={"x-transfer-token": transfer_token},
        )
        if resp.status_code in {400, 401, 404}:
            return resp.json()
        resp.raise_for_status()
        return resp.json()


@mcp.tool()
async def renew_domain(order_id: str) -> dict:
    """Renew a domain for 1 additional year.

    Creates a Stripe checkout session for the renewal payment.

    Args:
        order_id: The order ID of a completed domain purchase (e.g. "ord_abc123").
    """
    async with httpx.AsyncClient(base_url=BACKEND_URL, timeout=15) as client:
        resp = await client.post(f"/renew/{order_id}")
        if resp.status_code in {400, 404}:
            return resp.json()
        resp.raise_for_status()
        return resp.json()


# Import buy tools so they register on the same mcp instance
import instadomain.mcp_tools_buy  # noqa: E402, F401


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    """Run the MCP server (stdio transport by default)."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
