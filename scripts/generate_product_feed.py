#!/usr/bin/env python3
"""Generate the InstaDomain Stripe product catalog feed (CSV).

One row per supported TLD. Stripe requires this feed so MPP-enabled agents
can discover what InstaDomain sells. Domain registrations don't have a
fixed SKU per name, so each row represents *registration of a name in
that TLD* with a representative starting price; `disable_checkout=true`
routes agents to instadomain.fly.dev to pick a specific name and run the
real MPP/Stripe purchase.
"""
from __future__ import annotations

import csv
from pathlib import Path

HOMEPAGE = "https://instadomain.fly.dev/"
IMAGE = "https://instadomain.fly.dev/og.png"

# (tld, starting_price_usd, short_description)
TLDS: list[tuple[str, str, str]] = [
    ("com", "18.12", "1-year .com domain registration with WHOIS privacy and Cloudflare DNS hosting included."),
    ("net", "18.12", "1-year .net domain registration with WHOIS privacy and Cloudflare DNS hosting included."),
    ("org", "18.12", "1-year .org domain registration with WHOIS privacy and Cloudflare DNS hosting included."),
    ("dev", "20.99", "1-year .dev domain registration. HTTPS-only TLD, ideal for developer tooling. WHOIS privacy and Cloudflare DNS included."),
    ("app", "20.99", "1-year .app domain registration. HTTPS-only TLD, ideal for application landing pages. WHOIS privacy and Cloudflare DNS included."),
    ("io", "52.49", "1-year .io domain registration. Popular with startups and tech products. WHOIS privacy and Cloudflare DNS included."),
    ("co", "35.99", "1-year .co domain registration. Compact alternative to .com. WHOIS privacy and Cloudflare DNS included."),
    ("ai", "98.99", "1-year .ai domain registration. The default TLD for AI-native products. WHOIS privacy and Cloudflare DNS included."),
    ("xyz", "12.99", "1-year .xyz domain registration. Affordable, generic TLD. WHOIS privacy and Cloudflare DNS included."),
    ("me", "24.99", "1-year .me domain registration. Personal-brand TLD. WHOIS privacy and Cloudflare DNS included."),
]

COLUMNS = [
    "id",
    "mpn",
    "title",
    "description",
    "link",
    "image_link",
    "price",
    "availability",
    "brand",
    "condition",
    "inventory_not_tracked",
    "disable_checkout",
    "product_category",
]


def main() -> None:
    out = Path(__file__).resolve().parent.parent / "stripe_product_feed.csv"
    with out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        for tld, price, description in TLDS:
            writer.writerow({
                "id": f"domain-{tld}",
                "mpn": f"INSTADOMAIN-{tld.upper()}-1Y",
                "title": f".{tld} domain registration",
                "description": description,
                "link": HOMEPAGE,
                "image_link": IMAGE,
                "price": f"{price} USD",
                "availability": "in_stock",
                "brand": "InstaDomain",
                "condition": "new",
                "inventory_not_tracked": "true",
                "disable_checkout": "true",
                "product_category": "Internet > Domain Names > Domain Registration",
            })
    print(f"Wrote {out} ({len(TLDS)} rows)")


if __name__ == "__main__":
    main()
