from __future__ import annotations

import uuid
from datetime import datetime, timezone

import asyncpg

# State machine: maps current status -> set of valid next statuses
TRANSITIONS: dict[str, set[str]] = {
    "pending_payment": {"registering", "expired", "failed"},
    "registering": {"setting_dns", "failed"},
    "setting_dns": {"complete", "failed"},
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
) -> dict:
    """Insert a new order and return it as a dict."""
    order_id = f"ord_{uuid.uuid4().hex}"
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO orders (id, domain, tld, amount_cents, wholesale_cents,
                                stripe_session_id, payment_method)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            RETURNING *
            """,
            order_id,
            domain,
            tld,
            amount_cents,
            wholesale_cents,
            stripe_session_id,
            payment_method,
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
    """Fetch an order by its Stripe session ID."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM orders WHERE stripe_session_id = $1", stripe_session_id
        )
    return _row_to_dict(row)


async def update_order_status(
    pool: asyncpg.Pool, order_id: str, new_status: str, **fields
) -> dict:
    """Transition an order to a new status, with optional field updates.

    Validates the transition against the state machine. Sets completed_at
    when transitioning to 'complete'. Always updates updated_at.

    Raises:
        ValueError: If the transition is not allowed.
        ValueError: If the order does not exist.
    """
    async with pool.acquire() as conn:
        current = await conn.fetchval(
            "SELECT status FROM orders WHERE id = $1", order_id
        )
        if current is None:
            raise ValueError(f"Order {order_id} not found")

        allowed = TRANSITIONS.get(current, set())
        if new_status not in allowed:
            raise ValueError(
                f"Invalid transition: {current} -> {new_status}. "
                f"Allowed: {allowed or 'none (terminal state)'}"
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

        values.append(order_id)
        id_param = f"${len(values)}"

        query = f"UPDATE orders SET {', '.join(set_parts)} WHERE id = {id_param} RETURNING *"
        row = await conn.fetchrow(query, *values)

    return _row_to_dict(row)


async def get_pending_x402_order(pool: asyncpg.Pool, order_id: str) -> dict | None:
    """Fetch an x402 order that is still awaiting payment."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM orders WHERE id = $1 AND payment_method = 'x402' "
            "AND status = 'pending_payment'",
            order_id,
        )
    return _row_to_dict(row)


async def update_x402_settlement(
    pool: asyncpg.Pool, order_id: str, tx_hash: str
) -> dict:
    """Record the on-chain transaction hash for an x402 payment."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE orders SET x402_tx_hash = $1, updated_at = now() "
            "WHERE id = $2 RETURNING *",
            tx_hash,
            order_id,
        )
    if row is None:
        raise ValueError(f"Order {order_id} not found")
    return _row_to_dict(row)
