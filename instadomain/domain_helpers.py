"""Domain lookup helpers and suggestion candidate generation.

These are extracted from the main API module so they can be shared
across route modules and mocked independently in tests.
"""
from __future__ import annotations

import asyncio

from instadomain.opensrs_client import OpenSRSClient


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
# Async wrappers that can be mocked in tests
# ---------------------------------------------------------------------------

async def _check_availability(domain: str) -> dict:
    """Check domain availability via RDAP lookup.

    Returns dict with 'available' (bool) and 'domain' (str).
    """
    from domain_lookup import check_domain
    result = await check_domain(domain)
    return {"available": result.available, "domain": result.domain}


async def _get_price(domain: str, opensrs: OpenSRSClient) -> int:
    """Get wholesale price from OpenSRS."""
    return await asyncio.to_thread(opensrs.get_price, domain)


async def _check_domains_rdap(domains: list[str]) -> list[dict]:
    """Check multiple domains via RDAP concurrently."""
    from domain_lookup import check_domains
    results = await check_domains(domains)
    return [{"available": r.available, "domain": r.domain} for r in results]
