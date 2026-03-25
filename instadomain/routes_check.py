"""Domain check and suggestion routes."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Query, Request

from instadomain.affiliate import add_affiliate_links
from instadomain.config import Settings
from instadomain.domain_helpers import (
    _check_availability,
    _check_domains_rdap,
    _generate_candidates,
    _get_price,
)
from instadomain.models import BulkCheckRequest
from instadomain.pricing import calculate_retail_cents, format_price

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/check/{domain}")
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


@router.post("/check")
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


@router.get("/suggest")
async def suggest(
    request: Request,
    keyword: str = Query(
        ..., min_length=1, description="Keyword to build domain ideas from"
    ),
):
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
