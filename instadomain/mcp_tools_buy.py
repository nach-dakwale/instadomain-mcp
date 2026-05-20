"""MCP tools for domain purchasing (Stripe and crypto)."""
from __future__ import annotations

import httpx

from instadomain.mcp_server import BACKEND_URL, mcp


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

    Creates a Stripe checkout session. IMPORTANT: After this tool returns,
    you MUST open the checkout_url by running the shell command
    `open "<checkout_url>"` (macOS/Linux) so the payment page launches
    automatically in the user's default browser. Do NOT paste the URL as
    a link. Tell the user the page has been opened and they can complete
    payment there.

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
        data["pay_url"] = f"{BACKEND_URL}{data['pay_url']}"
        return data


@mcp.tool()
async def buy_domain_mpp(
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
    """Start the purchase flow for a domain via Stripe's Machine Payments Protocol (MPP).

    MPP lets autonomous agents pay with fiat (cards, Link) or stablecoins via
    Shared Payment Tokens, with no browser checkout. Two-step flow:

    Step 1: Call this tool to get an order_id and pay_url.
    Step 2: Make an HTTP GET request to the pay_url with an MPP-enabled HTTP
    client. The server responds with HTTP 402 + WWW-Authenticate; the client
    creates a Shared Payment Token and retries with an Authorization header.
    The server charges the SPT through Stripe and kicks off domain registration.

    After payment, call get_domain_status(order_id) to poll until complete.

    Requires: An MPP-compatible client configured to mint SPTs against the
    server's advertised Stripe Business Network profile.

    Args:
        domain: The domain to purchase (e.g. "coolstartup.com").
        first_name: Registrant's first name.
        last_name: Registrant's last name.
        email: Registrant's email address.
        address1: Registrant's street address.
        city: Registrant's city.
        state: Registrant's state or province.
        postal_code: Registrant's postal/zip code.
        country: 2-letter ISO country code.
        phone: Phone number in format +1.5551234567.
        org_name: Organization name (optional).

    Returns:
        Dict with order_id, pay_url (full URL), price_cents, price_display,
        network_id, and payment_method_types.
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
        resp = await client.post("/buy/mpp", json=payload)
        if resp.status_code in {400, 503}:
            return resp.json()
        resp.raise_for_status()
        data = resp.json()
        data["pay_url"] = f"{BACKEND_URL}{data['pay_url']}"
        return data
