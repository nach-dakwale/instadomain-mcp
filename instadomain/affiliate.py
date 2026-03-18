"""Affiliate link generation for available domains."""
from __future__ import annotations

from urllib.parse import quote

from instadomain.config import Settings


# ---------------------------------------------------------------------------
# URL templates
# ---------------------------------------------------------------------------

_TEMPLATES: dict[str, str] = {
    "dynadot": (
        "https://www.dynadot.com/domain/search?domain={domain}&ref={affiliate_id}"
    ),
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_register_urls(domain: str, settings: Settings | None = None) -> dict[str, str]:
    """Return a {registrar: url} dict of affiliate registration links."""
    if settings is None:
        settings = Settings()
    encoded_domain = quote(domain, safe=".-")
    urls: dict[str, str] = {}
    for registrar, template in _TEMPLATES.items():
        affiliate_id = settings.dynadot_affiliate_id
        urls[registrar] = template.format(domain=encoded_domain, affiliate_id=affiliate_id)
    return urls


def add_affiliate_links(result: dict, settings: Settings | None = None) -> dict:
    """Attach register_urls to a domain result dict if the domain is available.

    Works with plain dicts (not DomainResult) to stay decoupled from
    domain_lookup.py's model.
    """
    if settings is None:
        settings = Settings()
    if not result.get("available") or not settings.enable_affiliate_links:
        return result
    result = dict(result)
    result["register_urls"] = build_register_urls(result["domain"], settings)
    return result
