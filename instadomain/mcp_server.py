"""InstaDomain MCP server -- thin HTTP client over the backend API.

Tools:
  - check_domain     : check availability + price for a domain
  - buy_domain       : initiate purchase via Stripe checkout
  - get_domain_status: poll order status until complete or failed
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

    Args:
        domain: The full domain name to check (e.g. "coolstartup.com").

    Returns:
        Dict with availability status, price in cents, and formatted price.
    """
    async with httpx.AsyncClient(base_url=BACKEND_URL, timeout=15) as client:
        resp = await client.get(f"/check/{domain}")
        resp.raise_for_status()
        return resp.json()


@mcp.tool()
async def buy_domain(domain: str) -> dict:
    """Start the purchase flow for an available domain.

    Creates a Stripe checkout session. Returns a checkout URL that the
    user should open to complete payment, plus the order ID for tracking.

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


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    """Run the MCP server (stdio transport by default)."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
