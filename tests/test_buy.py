"""Integration tests for the buy and Stripe webhook flow."""
import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from instadomain import routes_buy as buy_module
from instadomain.api import create_app
from tests.conftest import MockConn, MockPool, noop_lifespan

_REGISTRANT = {
    "first_name": "Jane",
    "last_name": "Doe",
    "email": "jane@example.com",
    "address1": "123 Main St",
    "city": "Dover",
    "state": "DE",
    "postal_code": "19901",
    "country": "US",
    "phone": "+1.5551234567",
}

_PENDING_ORDER = {
    "id": "ord_new",
    "domain": "example",
    "tld": "com",
    "status": "pending_payment",
    "amount_cents": 1812,
    "created_at": datetime.now(timezone.utc),
    "completed_at": None,
    "nameservers": None,
    "error_msg": None,
    "registrant_contact": None,
    "email": None,
    "cloudflare_api_token": None,
    "renewal_stripe_session_id": None,
    "domain_expires_at": None,
}


def _make_app(conn=None):
    app = create_app()
    app.router.lifespan_context = noop_lifespan
    conn = conn or MockConn()
    app.state.pool = MockPool(conn)
    app.state.encryption_key = "test-key"
    app.state.opensrs = MagicMock()
    app.state.cloudflare = MagicMock()
    return app, conn


# ---------------------------------------------------------------------------
# POST /buy
# ---------------------------------------------------------------------------

def test_buy_domain_creates_order_and_returns_checkout_url(monkeypatch):
    app, _ = _make_app()

    async def fake_check(domain):
        return {"domain": domain, "available": True}

    async def fake_get_price(domain, opensrs):
        return 1450

    async def fake_create_order(pool, **kw):
        return {**_PENDING_ORDER, "id": "ord_new"}

    def fake_checkout(label, cents, order_id):
        return {"session_id": "cs_test", "checkout_url": "https://stripe.com/pay/cs_test"}

    monkeypatch.setattr(buy_module, "_check_availability", fake_check)
    monkeypatch.setattr(buy_module, "_get_price", fake_get_price)
    monkeypatch.setattr(buy_module, "create_order", fake_create_order)
    monkeypatch.setattr(buy_module, "create_checkout_session", fake_checkout)

    with TestClient(app) as client:
        resp = client.post("/buy", json={"domain": "example.com", "registrant": _REGISTRANT})

    assert resp.status_code == 200
    data = resp.json()
    assert data["order_id"] == "ord_new"
    assert data["checkout_url"] == "https://stripe.com/pay/cs_test"
    assert data["price_cents"] > 1450
    assert data["price_display"].startswith("$")


def test_buy_domain_rejects_unavailable_domain(monkeypatch):
    app, _ = _make_app()

    async def fake_check(domain):
        return {"domain": domain, "available": False}

    monkeypatch.setattr(buy_module, "_check_availability", fake_check)

    with TestClient(app) as client:
        resp = client.post("/buy", json={"domain": "taken.com", "registrant": _REGISTRANT})

    assert resp.status_code == 400
    assert "not available" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# POST /webhooks/stripe
# ---------------------------------------------------------------------------

def test_stripe_webhook_triggers_fulfillment_for_pending_order(monkeypatch):
    app, _ = _make_app()
    fulfilled = []

    def fake_verify(payload, sig):
        return {"type": "checkout.session.completed", "created": 9999999999}

    def fake_process(event):
        return {"session_id": "cs_test", "payment_intent": "pi_123", "email": "jane@test.com"}

    async def fake_get_by_session(pool, session_id):
        return _PENDING_ORDER

    async def fake_update_status(pool, order_id, new_status, **fields):
        return {**_PENDING_ORDER, "status": new_status}

    async def fake_fulfill(**kw):
        fulfilled.append(kw["order_id"])

    monkeypatch.setattr(buy_module, "verify_webhook", fake_verify)
    monkeypatch.setattr(buy_module, "process_webhook_event", fake_process)
    monkeypatch.setattr(buy_module, "get_order_by_stripe_session", fake_get_by_session)
    monkeypatch.setattr(buy_module, "update_order_status", fake_update_status)
    monkeypatch.setattr(buy_module, "fulfill_order", fake_fulfill)

    with TestClient(app) as client:
        resp = client.post(
            "/webhooks/stripe",
            content=b"payload",
            headers={"stripe-signature": "sig"},
        )

    assert resp.status_code == 200
    assert resp.json()["order_id"] == "ord_new"


def test_stripe_webhook_skips_already_processed_order(monkeypatch):
    app, _ = _make_app()

    def fake_verify(payload, sig):
        return {"type": "checkout.session.completed", "created": 9999999999}

    def fake_process(event):
        return {"session_id": "cs_test", "payment_intent": "pi_123", "email": None}

    async def fake_get_by_session(pool, session_id):
        return {**_PENDING_ORDER, "status": "registering"}

    monkeypatch.setattr(buy_module, "verify_webhook", fake_verify)
    monkeypatch.setattr(buy_module, "process_webhook_event", fake_process)
    monkeypatch.setattr(buy_module, "get_order_by_stripe_session", fake_get_by_session)

    with TestClient(app) as client:
        resp = client.post(
            "/webhooks/stripe",
            content=b"payload",
            headers={"stripe-signature": "sig"},
        )

    assert resp.status_code == 200
    assert resp.json() == {"received": True}


def test_stripe_webhook_rejects_bad_signature(monkeypatch):
    app, _ = _make_app()

    def fake_verify(payload, sig):
        raise ValueError("bad sig")

    monkeypatch.setattr(buy_module, "verify_webhook", fake_verify)

    with TestClient(app) as client:
        resp = client.post(
            "/webhooks/stripe",
            content=b"payload",
            headers={"stripe-signature": "bad"},
        )

    assert resp.status_code == 400
