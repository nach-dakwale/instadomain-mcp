"""MPP (Machine Payments Protocol) handler.

Wraps pympp's Mpp server with a Stripe Shared Payment Token charge intent.
A single Mpp instance is built per-request because StripeClient is cheap to
construct and the configuration is fully derived from app state.
"""
from __future__ import annotations

from typing import Any

import stripe as stripe_sdk
from mpp.methods.stripe import ChargeIntent, stripe as stripe_method
from mpp.server import Mpp


STRIPE_API_VERSION = "2026-03-04.preview"


def build_mpp_server(
    *,
    stripe_secret_key: str,
    network_id: str,
    secret_key: str,
    realm: str,
    payment_method_types: list[str],
    recipient: str = "instadomain",
) -> Mpp:
    """Return a stateless `Mpp` server that charges SPTs via Stripe."""
    client: Any = stripe_sdk.StripeClient(
        stripe_secret_key,
        stripe_version=STRIPE_API_VERSION,
    )
    return Mpp.create(
        method=stripe_method(
            network_id=network_id,
            payment_method_types=payment_method_types,
            currency="usd",
            decimals=2,
            recipient=recipient,
            intents={"charge": ChargeIntent(client=client)},
        ),
        realm=realm,
        secret_key=secret_key,
    )
