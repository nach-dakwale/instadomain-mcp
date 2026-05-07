from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import asyncpg

# State machine: maps current status -> set of valid next statuses
TRANSITIONS: dict[str, set[str]] = {
    "pending_payment": {"registering", "expired", "failed"},
    "registering": {"setting_dns", "failed"},
    "setting_dns": {"setting_dns", "complete", "failed", "dns_pending"},
    "dns_pending": {"setting_dns", "complete", "failed"},
    "complete": set(),
    "expired": set(),
    "failed": set(),
}


def _row_to_dict(row: asyncpg.Record | None) -> dict | None:
    if row is None:
        return None
    return dict(row)


async def create_order(
    pool: asyncpg.Pool,
    *,
    domain: str,
    tld: str,
    amount_cents: int,
    stripe_session_id: str | None = None,
    wholesale_cents: int | None = None,
    payment_method: str = "stripe",
    registrant_contact: dict | None = None,
) -> dict:
    """Insert a new order and return it as a dict."""
    order_id = f"ord_{uuid.uuid4().hex}"
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO orders (id, domain, tld, amount_cents, wholesale_cents,
                                stripe_session_id, payment_method, registrant_contact)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            RETURNING *
            """,
            order_id,
            domain,
            tld,
            amount_cents,
            wholesale_cents,
            stripe_session_id,
            payment_method,
            json.dumps(registrant_contact) if isinstance(registrant_contact, dict) else registrant_contact,
        )
    return _row_to_dict(row)


async def get_order(pool: asyncpg.Pool, order_id: str) -> dict | None:
    """Fetch an order by its ID."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM orders WHERE id = $1", order_id)
    return _row_to_dict(row)


async def get_order_by_stripe_session(
    pool: asyncpg.Pool, stripe_session_id: str
) -> dict | None:
    """Fetch an order by its Stripe session ID or renewal session ID."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM orders "
            "WHERE stripe_session_id = $1 OR renewal_stripe_session_id = $1",
            stripe_session_id,
        )
    return _row_to_dict(row)


def _valid_source_statuses(new_status: str) -> list[str]:
    """Return the list of statuses that are allowed to transition to new_status."""
    return [src for src, targets in TRANSITIONS.items() if new_status in targets]


async def update_order_status(
    pool: asyncpg.Pool, order_id: str, new_status: str, **fields
) -> dict:
    """Atomically transition an order to a new status, with optional field updates.

    Uses a single UPDATE with a WHERE clause that checks both the order ID and
    that the current status is a valid source for the requested transition.
    This prevents race conditions where two concurrent callers both pass the
    state check and proceed with duplicate work.

    Raises:
        ValueError: If the order does not exist or the transition is not valid
                    from the current status.
    """
    valid_sources = _valid_source_statuses(new_status)
    if not valid_sources:
        raise ValueError(
            f"No status can transition to '{new_status}'"
        )

    now = datetime.now(timezone.utc)
    fields["status"] = new_status
    fields["updated_at"] = now
    if new_status == "complete":
        fields["completed_at"] = now

    # Build dynamic SET clause
    set_parts = []
    values = []
    for i, (col, val) in enumerate(fields.items(), start=1):
        set_parts.append(f"{col} = ${i}")
        values.append(val)

    # $N+1 = order_id, $N+2 = valid source statuses array
    values.append(order_id)
    id_param = f"${len(values)}"
    values.append(valid_sources)
    sources_param = f"${len(values)}"

    query = (
        f"UPDATE orders SET {', '.join(set_parts)} "
        f"WHERE id = {id_param} AND status = ANY({sources_param}::text[]) "
        f"RETURNING *"
    )
    async with pool.acquire() as conn:
        row = await conn.fetchrow(query, *values)

    if row is None:
        raise ValueError(
            f"Transition to '{new_status}' failed for order {order_id}: "
            f"order not found or current status not in {valid_sources}"
        )

    return _row_to_dict(row)


async def get_stuck_dns_orders(pool: asyncpg.Pool) -> list[dict]:
    """Fetch all orders stuck in dns_pending or setting_dns status.

    Used at startup to recover orders whose in-memory retry tasks were lost.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM orders WHERE status IN ('dns_pending', 'setting_dns') "
            "ORDER BY updated_at ASC"
        )
    return [dict(row) for row in rows]


async def get_pending_x402_order(pool: asyncpg.Pool, order_id: str) -> dict | None:
    """Fetch an x402 order that is still awaiting payment."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM orders WHERE id = $1 AND payment_method = 'x402' "
            "AND status = 'pending_payment'",
            order_id,
        )
    return _row_to_dict(row)


async def get_pending_mpp_order(pool: asyncpg.Pool, order_id: str) -> dict | None:
    """Fetch an MPP order that is still awaiting payment."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM orders WHERE id = $1 AND payment_method = 'mpp' "
            "AND status = 'pending_payment'",
            order_id,
        )
    return _row_to_dict(row)


async def get_expiring_orders(pool: asyncpg.Pool, days: int = 90) -> list[dict]:
    """Fetch all completed orders with domains expiring within the given days."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM orders WHERE status = 'complete' "
            "AND domain_expires_at IS NOT NULL "
            "AND domain_expires_at > now() "
            "AND domain_expires_at <= now() + make_interval(days => $1) "
            "ORDER BY domain_expires_at ASC",
            days,
        )
    return [dict(row) for row in rows]


async def update_domain_expiry(
    pool: asyncpg.Pool, order_id: str, expires_at: datetime
) -> dict:
    """Update the domain_expires_at field for an order."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE orders SET domain_expires_at = $1, updated_at = now() "
            "WHERE id = $2 RETURNING *",
            expires_at,
            order_id,
        )
    if row is None:
        raise ValueError(f"Order {order_id} not found")
    return dict(row)


async def update_x402_settlement(
    pool: asyncpg.Pool,
    order_id: str,
    tx_hash: str,
    payer_address: str | None = None,
) -> dict:
    """Record the on-chain transaction hash and payer address for an x402 payment."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE orders SET x402_tx_hash = $1, x402_payer_address = $2, updated_at = now() "
            "WHERE id = $3 RETURNING *",
            tx_hash,
            payer_address,
            order_id,
        )
    if row is None:
        raise ValueError(f"Order {order_id} not found")
    return _row_to_dict(row)
