"""
domain_lookup.py - Core RDAP lookup logic for domain availability checking.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional

import httpx
from pydantic import BaseModel

from config import settings


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


class DomainResult(BaseModel):
    domain: str
    available: bool
    registrar: Optional[str] = None
    created: Optional[str] = None
    expires: Optional[str] = None
    nameservers: Optional[list[str]] = None
    register_urls: Optional[dict[str, str]] = None
    checked_at: str = ""

    def model_post_init(self, __context: object) -> None:
        if not self.checked_at:
            self.checked_at = datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# RDAP endpoint selection
# ---------------------------------------------------------------------------

_VERISIGN_TLDS = {"com", "net"}
_PIR_TLDS = {"org"}


def _rdap_url(domain: str) -> str:
    """Return the appropriate RDAP URL for the given domain."""
    tld = domain.rsplit(".", 1)[-1].lower()
    if tld in _VERISIGN_TLDS:
        return f"https://rdap.verisign.com/{tld}/v1/domain/{domain}"
    if tld in _PIR_TLDS:
        return f"https://rdap.publicinterestregistry.org/rdap/domain/{domain}"
    return f"https://rdap.org/domain/{domain}"


# ---------------------------------------------------------------------------
# RDAP response parsing helpers
# ---------------------------------------------------------------------------


def _parse_events(events: list[dict]) -> tuple[Optional[str], Optional[str]]:
    """Extract registration and expiry dates from RDAP events array."""
    created: Optional[str] = None
    expires: Optional[str] = None
    for ev in events:
        action = ev.get("eventAction", "")
        date = ev.get("eventDate", "")
        if action == "registration":
            created = date
        elif action == "expiration":
            expires = date
    return created, expires


def _parse_nameservers(ns_list: list[dict]) -> list[str]:
    return [ns.get("ldhName", "").lower() for ns in ns_list if ns.get("ldhName")]


def _parse_registrar(entities: list[dict]) -> Optional[str]:
    """Find the registrar entity (role == 'registrar') and return its name."""
    for entity in entities:
        roles = entity.get("roles", [])
        if "registrar" in roles:
            vcard = entity.get("vcardArray", [])
            # vcardArray = ["vcard", [[prop, params, type, value], ...]]
            if isinstance(vcard, list) and len(vcard) >= 2:
                for prop in vcard[1]:
                    if prop[0] == "fn":
                        return prop[3]
            # Fall back to publicIds
            for pid in entity.get("publicIds", []):
                if pid.get("type") == "IANA Registrar ID":
                    pass  # prefer name over ID
            name = entity.get("handle") or entity.get("objectClassName")
            return name
    return None


# ---------------------------------------------------------------------------
# Core lookup
# ---------------------------------------------------------------------------


async def check_domain(domain: str, client: Optional[httpx.AsyncClient] = None) -> DomainResult:
    """Check a single domain via RDAP. Returns DomainResult."""
    domain = domain.strip().lower()
    url = _rdap_url(domain)

    _close_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=settings.rdap_timeout, follow_redirects=True)

    try:
        resp = await client.get(url)
    except httpx.RequestError as exc:
        # Network error — treat as unknown but not available
        return DomainResult(
            domain=domain,
            available=False,
            registrar=f"RDAP error: {exc}",
        )
    finally:
        if _close_client:
            await client.aclose()

    if resp.status_code == 404:
        return DomainResult(domain=domain, available=True)

    if resp.status_code == 200:
        data = resp.json()
        events = data.get("events", [])
        created, expires = _parse_events(events)
        nameservers = _parse_nameservers(data.get("nameservers", []))
        registrar = _parse_registrar(data.get("entities", []))
        return DomainResult(
            domain=domain,
            available=False,
            registrar=registrar,
            created=created,
            expires=expires,
            nameservers=nameservers or None,
        )

    # Any other status (403, 429, 5xx, etc.) — treat as unknown/taken to be safe
    return DomainResult(
        domain=domain,
        available=False,
        registrar=f"RDAP returned HTTP {resp.status_code}",
    )


async def check_domains(domains: list[str]) -> list[DomainResult]:
    """Check multiple domains concurrently, limited to max_concurrent_lookups."""
    semaphore = asyncio.Semaphore(settings.max_concurrent_lookups)
    async with httpx.AsyncClient(timeout=settings.rdap_timeout, follow_redirects=True) as client:

        async def _bounded(domain: str) -> DomainResult:
            async with semaphore:
                return await check_domain(domain, client=client)

        return list(await asyncio.gather(*[_bounded(d) for d in domains]))
