from __future__ import annotations

import asyncpg

_pool: asyncpg.Pool | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id                      TEXT PRIMARY KEY,
    email                   TEXT,
    domain                  TEXT NOT NULL,
    tld                     TEXT NOT NULL,
    registration_years      INTEGER NOT NULL DEFAULT 1,
    amount_cents            INTEGER NOT NULL,
    wholesale_cents         INTEGER,
    currency                TEXT NOT NULL DEFAULT 'usd',
    stripe_session_id       TEXT UNIQUE,
    stripe_payment_intent   TEXT,
    opensrs_order_id        TEXT,
    cloudflare_zone_id      TEXT,
    cloudflare_api_token    TEXT,
    cloudflare_token_retrieved BOOLEAN NOT NULL DEFAULT FALSE,
    nameservers             TEXT[],
    payment_method          TEXT NOT NULL DEFAULT 'stripe',
    x402_tx_hash            TEXT,
    x402_payer_address      TEXT,
    registrant_contact      JSONB,
    status                  TEXT NOT NULL DEFAULT 'pending_payment',
    error_msg               TEXT,
    retry_count             INTEGER NOT NULL DEFAULT 0,
    domain_expires_at       TIMESTAMPTZ,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at            TIMESTAMPTZ,
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_orders_stripe_session_id ON orders (stripe_session_id);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders (status);
CREATE INDEX IF NOT EXISTS idx_orders_domain ON orders (domain);
"""

MIGRATION_X402 = """
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'orders' AND column_name = 'payment_method'
    ) THEN
        ALTER TABLE orders ADD COLUMN payment_method TEXT NOT NULL DEFAULT 'stripe';
        ALTER TABLE orders ADD COLUMN x402_tx_hash TEXT;
    END IF;
END $$;
"""

MIGRATION_X402_PAYER = """
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'orders' AND column_name = 'x402_payer_address'
    ) THEN
        ALTER TABLE orders ADD COLUMN x402_payer_address TEXT;
    END IF;
END $$;
"""

MIGRATION_REGISTRANT_CONTACT = """
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'orders' AND column_name = 'registrant_contact'
    ) THEN
        ALTER TABLE orders ADD COLUMN registrant_contact JSONB;
    END IF;
END $$;
"""


async def init_pool(database_url: str) -> asyncpg.Pool:
    """Create the connection pool and run the schema migration."""
    global _pool
    _pool = await asyncpg.create_pool(database_url)
    async with _pool.acquire() as conn:
        await conn.execute(SCHEMA)
        await conn.execute(MIGRATION_X402)
        await conn.execute(MIGRATION_X402_PAYER)
        await conn.execute(MIGRATION_REGISTRANT_CONTACT)
    return _pool


def get_pool() -> asyncpg.Pool:
    """Return the existing pool or raise if not initialized."""
    if _pool is None:
        raise RuntimeError("Database pool not initialized. Call init_pool() first.")
    return _pool


async def close_pool() -> None:
    """Close the connection pool."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
