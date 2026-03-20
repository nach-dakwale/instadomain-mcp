"""InstaDomain MCP server -- thin HTTP client over the backend API.

Tools:
  - check_domain       : check availability + price for a single domain
  - check_domains_bulk : check up to 50 domains at once (RDAP, no pricing)
  - suggest_domains    : generate + check domain name ideas for a keyword
  - buy_domain         : initiate purchase via Stripe checkout
  - buy_domain_crypto  : initiate purchase via x402 USDC payment
  - get_domain_status  : poll order status until complete or failed
  - get_transfer_code  : get EPP auth code for transferring a domain away
  - unlock_domain      : remove registrar transfer lock from a domain
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
async def buy_domain(
    domain: str,
    first_name: str,
    last_name: str,
    email: str,
    address1: str,
    city: str,
    state: str,
    postal_code: str,
    country: str,
    phone: str,
    org_name: str = "",
) -> dict:
    """Start the purchase flow for an available domain via Stripe checkout.

    IMPORTANT: Before calling this tool, you MUST first call check_domain
    to get the price, then clearly show the user the price and get their
    explicit confirmation before proceeding. Never call buy_domain without
    the user seeing and approving the price first.

    The registrant contact details are required because the domain will be
    registered in the buyer's name (they become the legal owner). WHOIS
    privacy is enabled by default, so these details are not publicly visible.

    Creates a Stripe checkout session. Returns a checkout URL that the
    user should open in their browser to complete payment securely via
    Stripe, plus the order ID for tracking.

    Args:
        domain: The domain to purchase (e.g. "coolstartup.com").
        first_name: Registrant's first name.
        last_name: Registrant's last name.
        email: Registrant's email address.
        address1: Registrant's street address.
        city: Registrant's city.
        state: Registrant's state or province.
        postal_code: Registrant's postal/zip code.
        country: 2-letter ISO country code (e.g. "US", "GB", "DE").
        phone: Phone number in format +1.5551234567.
        org_name: Organization name (optional, leave empty for individuals).

    Returns:
        Dict with order_id, checkout_url, price_cents, and price_display.
    """
    payload = {
        "domain": domain,
        "registrant": {
            "first_name": first_name,
            "last_name": last_name,
            "email": email,
            "org_name": org_name,
            "address1": address1,
            "city": city,
            "state": state,
            "postal_code": postal_code,
            "country": country,
            "phone": phone,
        },
    }
    async with httpx.AsyncClient(base_url=BACKEND_URL, timeout=15) as client:
        resp = await client.post("/buy", json=payload)
        if resp.status_code == 400:
            return resp.json()
        resp.raise_for_status()
        return resp.json()


@mcp.tool()
async def buy_domain_crypto(
    domain: str,
    first_name: str,
    last_name: str,
    email: str,
    address1: str,
    city: str,
    state: str,
    postal_code: str,
    country: str,
    phone: str,
    org_name: str = "",
) -> dict:
    """Start the purchase flow for a domain using USDC crypto payment (x402 protocol).

    This is a 2-step process for autonomous agent payments:

    Step 1: Call this tool to get an order_id and pay_url.
    Step 2: Make an HTTP GET request to the pay_url. Your x402-enabled HTTP
    client will receive an HTTP 402 response with payment requirements, then
    automatically pay with USDC on Base. The payment and settlement happen
    via the x402 protocol (no browser or human needed).

    After payment, call get_domain_status(order_id) to poll until complete.

    Requires: An x402-compatible HTTP client with a funded USDC wallet on Base.

    The registrant contact details are required because the domain will be
    registered in the buyer's name (they become the legal owner). WHOIS
    privacy is enabled by default, so these details are not publicly visible.

    IMPORTANT: Before calling this tool, you MUST first call check_domain
    to get the price and confirm it with the user.

    Args:
        domain: The domain to purchase (e.g. "coolstartup.com").
        first_name: Registrant's first name.
        last_name: Registrant's last name.
        email: Registrant's email address.
        address1: Registrant's street address.
        city: Registrant's city.
        state: Registrant's state or province.
        postal_code: Registrant's postal/zip code.
        country: 2-letter ISO country code (e.g. "US", "GB", "DE").
        phone: Phone number in format +1.5551234567.
        org_name: Organization name (optional, leave empty for individuals).

    Returns:
        Dict with order_id, pay_url (full URL to GET with x402 client),
        price_usdc, price_cents, network, and asset contract address.
    """
    payload = {
        "domain": domain,
        "registrant": {
            "first_name": first_name,
            "last_name": last_name,
            "email": email,
            "org_name": org_name,
            "address1": address1,
            "city": city,
            "state": state,
            "postal_code": postal_code,
            "country": country,
            "phone": phone,
        },
    }
    async with httpx.AsyncClient(base_url=BACKEND_URL, timeout=15) as client:
        resp = await client.post("/buy/crypto", json=payload)
        if resp.status_code in {400, 503}:
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
async def get_transfer_code(order_id: str) -> dict:
    """Get the EPP/transfer authorization code for a completed domain purchase.

    Use this when the domain owner wants to transfer their domain to another
    registrar. The order must be in "complete" status. The auth code is
    required by the receiving registrar to authorize the transfer.

    Args:
        order_id: The order ID of a completed domain purchase.

    Returns:
        Dict with order_id, domain, and auth_code.
    """
    async with httpx.AsyncClient(base_url=BACKEND_URL, timeout=15) as client:
        resp = await client.get(f"/transfer-code/{order_id}")
        if resp.status_code in {400, 404}:
            return resp.json()
        resp.raise_for_status()
        return resp.json()


@mcp.tool()
async def unlock_domain(order_id: str) -> dict:
    """Remove the registrar transfer lock from a completed domain purchase.

    Domains are locked by default to prevent unauthorized transfers. Call
    this before initiating a transfer to another registrar. The order must
    be in "complete" status.

    Args:
        order_id: The order ID of a completed domain purchase.

    Returns:
        Dict with order_id, domain, and unlocked status.
    """
    async with httpx.AsyncClient(base_url=BACKEND_URL, timeout=15) as client:
        resp = await client.post(f"/unlock/{order_id}")
        if resp.status_code in {400, 404}:
            return resp.json()
        resp.raise_for_status()
        return resp.json()


@mcp.tool()
async def renew_domain(order_id: str) -> dict:
    """Renew a domain for 1 additional year.

    Creates a Stripe checkout session for the renewal payment. The user
    must open the checkout URL to complete payment, after which the domain
    will be renewed automatically via the registrar.

    The order must be in "complete" status (i.e., the domain was
    previously registered successfully).

    Args:
        order_id: The order ID of a completed domain purchase (e.g. "ord_abc123").

    Returns:
        Dict with order_id, checkout_url, price_cents, price_display, domain,
        and renewal_years.
    """
    async with httpx.AsyncClient(base_url=BACKEND_URL, timeout=15) as client:
        resp = await client.post(f"/renew/{order_id}")
        if resp.status_code in {400, 404}:
            return resp.json()
        resp.raise_for_status()
        return resp.json()


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
