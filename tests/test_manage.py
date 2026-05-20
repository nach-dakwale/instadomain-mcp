"""Integration tests for order status, renewal, and expiry admin routes."""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from instadomain import routes_manage as manage_module
from instadomain.api import create_app
from tests.conftest import MockConn, MockPool, noop_lifespan

_COMPLETE_ORDER = {
    "id": "ord_123",
    "domain": "example",
    "tld": "com",
    "status": "complete",
    "amount_cents": 1812,
    "created_at": datetime.now(timezone.utc),
    "completed_at": datetime.now(timezone.utc),
    "nameservers": ["ada.ns.cloudflare.com", "brad.ns.cloudflare.com"],
    "error_msg": None,
    "cloudflare_api_token": None,
    "email": "owner@example.com",
}


def _make_app(conn=None):
    app = create_app()
    app.router.lifespan_context = noop_lifespan
    conn = conn or MockConn()
    app.state.pool = MockPool(conn)
    app.state.encryption_key = "test-key"
    app.state.opensrs = MagicMock()
    return app, conn


# ---------------------------------------------------------------------------
# GET /status/{order_id}
# ---------------------------------------------------------------------------

def test_status_returns_order_details(monkeypatch):
    app, _ = _make_app()

    async def fake_get_order(_pool, _id):
        return _COMPLETE_ORDER

    monkeypatch.setattr(manage_module, "get_order", fake_get_order)

    with TestClient(app) as client:
        resp = client.get("/status/ord_123")

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "complete"
    assert data["domain"] == "example.com"
    assert data["nameservers"] == ["ada.ns.cloudflare.com", "brad.ns.cloudflare.com"]


def test_status_decrypts_cloudflare_token_for_complete_order(monkeypatch):
    app, _ = _make_app()
    order = {**_COMPLETE_ORDER, "cloudflare_api_token": "enc:secret"}

    async def fake_get_order(_pool, _id):
        return order

    monkeypatch.setattr(manage_module, "get_order", fake_get_order)
    monkeypatch.setattr(manage_module, "decrypt", lambda token, _key: f"plain:{token}")

    with TestClient(app) as client:
        resp = client.get("/status/ord_123")

    assert resp.status_code == 200
    assert resp.json()["cloudflare_token"] == "plain:enc:secret"


def test_status_returns_404_for_missing_order(monkeypatch):
    app, _ = _make_app()

    async def fake_get_order(_pool, _id):
        return None

    monkeypatch.setattr(manage_module, "get_order", fake_get_order)

    with TestClient(app) as client:
        resp = client.get("/status/ord_missing")

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /renew/{order_id}
# ---------------------------------------------------------------------------

def test_renew_domain_creates_checkout_session(monkeypatch):
    app, _ = _make_app()

    async def fake_get_order(_pool, _id):
        return _COMPLETE_ORDER

    async def fake_get_price(domain, opensrs):
        return 1450

    def fake_checkout(label, cents, order_id):
        return {"session_id": "cs_renew", "checkout_url": "https://stripe.com/pay/cs_renew"}

    monkeypatch.setattr(manage_module, "get_order", fake_get_order)
    monkeypatch.setattr(manage_module, "_get_price", fake_get_price)
    monkeypatch.setattr(manage_module, "create_checkout_session", fake_checkout)

    with TestClient(app) as client:
        resp = client.post("/renew/ord_123")

    assert resp.status_code == 200
    data = resp.json()
    assert data["checkout_url"] == "https://stripe.com/pay/cs_renew"
    assert data["domain"] == "example.com"
    assert data["price_cents"] > 1450


def test_renew_rejects_non_complete_order(monkeypatch):
    app, _ = _make_app()

    async def fake_get_order(_pool, _id):
        return {**_COMPLETE_ORDER, "status": "pending_payment"}

    monkeypatch.setattr(manage_module, "get_order", fake_get_order)

    with TestClient(app) as client:
        resp = client.post("/renew/ord_123")

    assert resp.status_code == 400
    assert "complete" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# GET /expiring (admin)
# ---------------------------------------------------------------------------

def test_expiring_requires_admin_key(monkeypatch):
    app, _ = _make_app()
    monkeypatch.setenv("INSTADOMAIN_ADMIN_KEY", "secret")

    with TestClient(app) as client:
        resp = client.get("/expiring")

    assert resp.status_code == 401


def test_expiring_returns_domains_with_days_remaining(monkeypatch):
    app, _ = _make_app()
    expires_at = datetime.now(timezone.utc) + timedelta(days=30)

    async def fake_get_expiring(_pool, _days):
        return [{
            **_COMPLETE_ORDER,
            "domain_expires_at": expires_at,
        }]

    monkeypatch.setattr(manage_module, "get_expiring_orders", fake_get_expiring)
    monkeypatch.setattr(
        manage_module, "Settings",
        lambda: type("S", (), {"backend_url": "https://instadomain.fly.dev"})(),
    )
    monkeypatch.setenv("INSTADOMAIN_ADMIN_KEY", "secret")

    with TestClient(app) as client:
        resp = client.get("/expiring", headers={"x-api-key": "secret"})

    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 1
    assert data["orders"][0]["domain"] == "example.com"
    assert 28 <= data["orders"][0]["days_remaining"] <= 31
