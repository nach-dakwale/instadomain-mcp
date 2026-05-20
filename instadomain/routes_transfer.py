"""Transfer authorization routes with email-based verification."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

from instadomain.emails import send_transfer_verification_email
from instadomain.orders import get_order

logger = logging.getLogger(__name__)

router = APIRouter()

CODE_TTL_MINUTES = 15
TOKEN_TTL_MINUTES = 15


def _hash_code(code: str, secret_key: str) -> str:
    return hmac.new(secret_key.encode(), code.encode(), hashlib.sha256).hexdigest()


def _mask_email(email: str) -> str:
    if "@" not in email:
        return "***"
    user, domain = email.split("@", 1)
    if len(user) <= 2:
        return f"{'*' * len(user)}@{domain}"
    return f"{user[0]}{'*' * (len(user) - 2)}{user[-1]}@{domain}"


async def _require_transfer_token(order_id: str, token: str | None, pool) -> None:
    if not token:
        raise HTTPException(
            status_code=401,
            detail=(
                "Transfer token required. Call POST /transfer/request-code/{order_id} "
                "to receive a verification code by email, then POST "
                "/transfer/verify-code/{order_id} to get a token."
            ),
        )
    now = datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM transfer_verifications "
            "WHERE order_id = $1 AND verified_token = $2 AND token_expires_at > $3",
            order_id, token, now,
        )
    if row is None:
        raise HTTPException(status_code=401, detail="Invalid or expired transfer token")


class VerifyCodeRequest(BaseModel):
    code: str


@router.post("/transfer/request-code/{order_id}")
async def request_transfer_code(order_id: str, request: Request):
    """Send a 6-digit verification code to the registrant's email."""
    pool = request.app.state.pool
    encryption_key = request.app.state.encryption_key

    order = await get_order(pool, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    if order["status"] != "complete":
        raise HTTPException(status_code=400, detail="Order must be complete to request transfer")

    email = order.get("email")
    if not email:
        raise HTTPException(status_code=400, detail="No email on file for this order")

    domain = f"{order['domain']}.{order['tld']}"
    code = f"{secrets.randbelow(1_000_000):06d}"
    code_hash = _hash_code(code, encryption_key)
    verification_id = f"tv_{uuid.uuid4().hex}"
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=CODE_TTL_MINUTES)

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE transfer_verifications SET code_expires_at = now() "
            "WHERE order_id = $1 AND verified_token IS NULL",
            order_id,
        )
        await conn.execute(
            "INSERT INTO transfer_verifications "
            "(id, order_id, code_hash, code_expires_at) VALUES ($1, $2, $3, $4)",
            verification_id, order_id, code_hash, expires_at,
        )

    await send_transfer_verification_email(to_email=email, domain=domain, code=code)

    return {
        "message": f"Verification code sent to {_mask_email(email)}",
        "expires_in_minutes": CODE_TTL_MINUTES,
    }


@router.post("/transfer/verify-code/{order_id}")
async def verify_transfer_code(
    order_id: str,
    body: VerifyCodeRequest,
    request: Request,
):
    """Verify the emailed code and return a short-lived transfer token."""
    pool = request.app.state.pool
    encryption_key = request.app.state.encryption_key
    code_hash = _hash_code(body.code.strip(), encryption_key)
    now = datetime.now(timezone.utc)

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM transfer_verifications "
            "WHERE order_id = $1 AND code_hash = $2 "
            "AND code_expires_at > $3 AND verified_token IS NULL "
            "ORDER BY created_at DESC LIMIT 1",
            order_id, code_hash, now,
        )
        if row is None:
            raise HTTPException(status_code=400, detail="Invalid or expired code")

        transfer_token = uuid.uuid4().hex
        token_expires_at = now + timedelta(minutes=TOKEN_TTL_MINUTES)
        await conn.execute(
            "UPDATE transfer_verifications "
            "SET verified_token = $1, token_expires_at = $2 WHERE id = $3",
            transfer_token, token_expires_at, row["id"],
        )

    return {
        "transfer_token": transfer_token,
        "expires_in_minutes": TOKEN_TTL_MINUTES,
    }


@router.get("/transfer-code/{order_id}")
async def get_transfer_code(
    order_id: str,
    request: Request,
    x_transfer_token: str | None = Header(default=None),
):
    """Get the EPP auth code. Requires a verified transfer token."""
    pool = request.app.state.pool
    opensrs = request.app.state.opensrs

    await _require_transfer_token(order_id, x_transfer_token, pool)

    order = await get_order(pool, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    if order["status"] != "complete":
        raise HTTPException(status_code=400, detail="Order must be complete")

    domain = f"{order['domain']}.{order['tld']}"
    try:
        auth_code = await asyncio.to_thread(opensrs.get_transfer_auth_code, domain)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to get transfer code: {exc}")

    return {"order_id": order_id, "domain": domain, "auth_code": auth_code}


@router.post("/unlock/{order_id}")
async def unlock_domain(
    order_id: str,
    request: Request,
    x_transfer_token: str | None = Header(default=None),
):
    """Unlock domain for transfer. Requires a verified transfer token."""
    pool = request.app.state.pool
    opensrs = request.app.state.opensrs

    await _require_transfer_token(order_id, x_transfer_token, pool)

    order = await get_order(pool, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    if order["status"] != "complete":
        raise HTTPException(status_code=400, detail="Order must be complete")

    domain = f"{order['domain']}.{order['tld']}"
    try:
        await asyncio.to_thread(opensrs.unlock_domain, domain)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to unlock domain: {exc}")

    return {"order_id": order_id, "domain": domain, "unlocked": True}
