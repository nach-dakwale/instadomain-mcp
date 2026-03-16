from __future__ import annotations

# Wholesale cost thresholds (cents) -- domains priced at or below this get
# a flat markup; above this they get a percentage markup instead.
WHOLESALE_THRESHOLDS: dict[str, int] = {
    "com": 1000,
    "net": 1000,
    "org": 1000,
    "dev": 1400,
    "app": 1400,
    "io": 3500,
}

# Flat markup in cents for standard-priced domains
STANDARD_MARKUP: dict[str, int] = {
    "com": 209,
    "net": 209,
    "org": 209,
    "dev": 299,
    "app": 299,
    "io": 499,
}

# Percentage markup for premium domains (above the wholesale threshold)
PREMIUM_MARKUP_PCT: float = 0.25


def calculate_retail_cents(wholesale_cents: int, tld: str) -> int:
    """Calculate the retail price in cents for a domain.

    Standard domains (wholesale at or below the TLD threshold) get a flat
    markup. Premium domains (above the threshold) get a percentage markup.

    For unknown TLDs, falls back to .com thresholds and markup.
    """
    threshold = WHOLESALE_THRESHOLDS.get(tld, WHOLESALE_THRESHOLDS["com"])
    markup = STANDARD_MARKUP.get(tld, STANDARD_MARKUP["com"])

    if wholesale_cents <= threshold:
        return wholesale_cents + markup
    else:
        return int(wholesale_cents * (1 + PREMIUM_MARKUP_PCT))


def format_price(cents: int) -> str:
    """Format a price in cents as a dollar string like '$10.99'."""
    return f"${cents / 100:.2f}"
