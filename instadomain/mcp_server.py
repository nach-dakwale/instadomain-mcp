"""InstaDomain MCP server -- thin HTTP client over the backend API.

Tools:
  - check_domain      : check availability + price for a single domain
  - check_domains_bulk: check up to 50 domains at once (RDAP, no pricing)
  - suggest_domains   : generate + check domain name ideas for a keyword
  - buy_domain        : initiate purchase via Stripe checkout
  - get_domain_status : poll order status until complete or failed
"""
from __future__ import annotations

import asyncio
import os

import httpx

from fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BACKEND_URL = os.environ.get("INSTADOMAIN_BACKEND_URL", "https://instadomain.fly.dev")
POLL_INTERVAL_SECONDS = 3
POLL_TIMEOUT_SECONDS = 120
BULK_LIMIT = 50

# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

mcp = FastMCP("instadomain")

# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def check_domain(domain: str) -> dict:
    """Check if a domain is available for purchase and get its price.

    Always call this before buy_domain. Show the user the price_display
    value (e.g. "$18.12") and confirm they want to proceed before buying.

    Args:
        domain: The full domain name to check (e.g. "coolstartup.com").

    Returns:
        Dict with availability status, price in cents, and formatted price.
        If available, includes price_cents and price_display for the
        1-year registration cost.
    """
    async with httpx.AsyncClient(base_url=BACKEND_URL, timeout=15) as client:
        resp = await client.get(f"/check/{domain}")
        resp.raise_for_status()
        return resp.json()


@mcp.tool()
async def buy_domain(domain: str) -> dict:
    """Start the purchase flow for an available domain.

    IMPORTANT: Before calling this tool, you MUST first call check_domain
    to get the price, then clearly show the user the price and get their
    explicit confirmation before proceeding. Never call buy_domain without
    the user seeing and approving the price first.

    Creates a Stripe checkout session. Returns a checkout URL that the
    user should open in their browser to complete payment securely via
    Stripe, plus the order ID for tracking.

    Args:
        domain: The domain to purchase (e.g. "coolstartup.com").

    Returns:
        Dict with order_id, checkout_url, price_cents, and price_display.
    """
    async with httpx.AsyncClient(base_url=BACKEND_URL, timeout=15) as client:
        resp = await client.post("/buy", json={"domain": domain})
        if resp.status_code == 400:
            return resp.json()  # domain not available — return error gracefully
        resp.raise_for_status()
        return resp.json()


@mcp.tool()
async def buy_domain_crypto(domain: str) -> dict:
    """Start the purchase flow for a domain using USDC crypto payment (x402 protocol).

    This is a 2-step process for autonomous agent payments:

    Step 1: Call this tool to get an order_id and pay_url.
    Step 2: Make an HTTP GET request to the pay_url. Your x402-enabled HTTP
    client will receive an HTTP 402 response with payment requirements, then
    automatically pay with USDC on Base. The payment and settlement happen
    via the x402 protocol (no browser or human needed).

    After payment, call get_domain_status(order_id) to poll until complete.

    Requires: An x402-compatible HTTP client with a funded USDC wallet on Base.

    IMPORTANT: Before calling this tool, you MUST first call check_domain
    to get the price and confirm it with the user.

    Args:
        domain: The domain to purchase (e.g. "coolstartup.com").

    Returns:
        Dict with order_id, pay_url (full URL to GET with x402 client),
        price_usdc, price_cents, network, and asset contract address.
    """
    async with httpx.AsyncClient(base_url=BACKEND_URL, timeout=15) as client:
        resp = await client.post("/buy/crypto", json={"domain": domain})
        if resp.status_code == 400:
            return resp.json()
        resp.raise_for_status()
        data = resp.json()
        # Make pay_url absolute so the agent can hit it directly
        data["pay_url"] = f"{BACKEND_URL}{data['pay_url']}"
        return data


@mcp.tool()
async def get_domain_status(order_id: str) -> dict:
    """Get the status of a domain purchase order.

    Polls the backend every 3 seconds (up to 120 seconds) until the order
    reaches a terminal state (complete or failed). Returns the final order
    status including nameservers and DNS token if available.

    Args:
        order_id: The order ID returned from buy_domain (e.g. "ord_abc123").

    Returns:
        Dict with order status, domain, nameservers, and CF DNS token if complete.
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
async def check_domains_bulk(domains: list[str]) -> dict:
    """Check availability of up to 50 domain names in one call.

    Uses fast RDAP lookups (no pricing). Returns a summary with
    total/available/taken counts plus per-domain details and affiliate
    registration links for available domains.

    Args:
        domains: List of domain names to check (max 50).
    """
    if len(domains) > BULK_LIMIT:
        return {
            "error": f"Too many domains: {len(domains)} provided, maximum is {BULK_LIMIT}.",
            "limit": BULK_LIMIT,
        }

    async with httpx.AsyncClient(base_url=BACKEND_URL, timeout=30) as client:
        resp = await client.post("/check", json={"domains": domains})
        resp.raise_for_status()
        return resp.json()


@mcp.tool()
async def suggest_domains(keyword: str) -> dict:
    """Generate domain name ideas from a keyword and check their availability.

    Uses common prefix/suffix patterns to generate 10-15 domain candidates
    across .com, .io, .ai, .dev, .co and checks all of them via fast RDAP
    lookups. Returns available domains with affiliate registration links.

    Args:
        keyword: A keyword or short business name (e.g. "taskflow").
    """
    async with httpx.AsyncClient(base_url=BACKEND_URL, timeout=30) as client:
        resp = await client.get("/suggest", params={"keyword": keyword})
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    """Run the MCP server (stdio transport by default)."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
