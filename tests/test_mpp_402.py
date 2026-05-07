"""Tests for the MPP paywalled endpoint.

Verifies the 402 challenge is returned when no Authorization header is
present, and the order-not-payable guard fires for non-pending orders.
"""
from contextlib import asynccontextmanager

from fastapi.testclient import TestClient

from instadomain import routes_mpp as mpp_module
from instadomain.api import create_app


@asynccontextmanager
async def _noop_lifespan(_app):
    yield


def _wire_app_for_mpp(monkeypatch, *, order: dict | None):
    app = create_app()
    app.router.lifespan_context = _noop_lifespan
    app.state.pool = object()
    app.state.encryption_key = "test-key"
    app.state.mpp_enabled = True
    app.state.mpp_network_id = "bn_test_123"
    app.state.mpp_realm = "test.example"
    app.state.mpp_payment_method_types = ["card", "link"]
    app.state.mpp_secret_key = "0" * 64
    app.state.stripe_secret_key = "sk_test_dummy"

    async def fake_get_order(_pool, _order_id):
        return order

    async def fake_get_pending(_pool, _order_id):
        if order is None:
            return None
        if order.get("status") == "pending_payment" and order.get("payment_method") == "mpp":
            return order
        return None

    monkeypatch.setattr(mpp_module, "get_order", fake_get_order)
    monkeypatch.setattr(mpp_module, "get_pending_mpp_order", fake_get_pending)
    return app


def test_pay_mpp_returns_402_challenge_when_no_authorization(monkeypatch):
    order = {
        "id": "ord_pending",
        "domain": "example",
        "tld": "com",
        "amount_cents": 1812,
        "payment_method": "mpp",
        "status": "pending_payment",
    }
    app = _wire_app_for_mpp(monkeypatch, order=order)

    with TestClient(app) as client:
        response = client.get("/pay/mpp/ord_pending")

    assert response.status_code == 402
    assert "WWW-Authenticate" in response.headers
    challenge = response.headers["WWW-Authenticate"]
    assert challenge.lower().startswith("payment ")
    body = response.json()
    assert body["order_id"] == "ord_pending"


def test_pay_mpp_rejects_non_pending_order(monkeypatch):
    order = {
        "id": "ord_paid",
        "domain": "example",
        "tld": "com",
        "amount_cents": 1812,
        "payment_method": "mpp",
        "status": "complete",
    }
    app = _wire_app_for_mpp(monkeypatch, order=order)

    with TestClient(app) as client:
        response = client.get("/pay/mpp/ord_paid")

    assert response.status_code == 409
    assert "cannot be paid again" in response.json()["detail"]


def test_pay_mpp_rejects_non_mpp_order(monkeypatch):
    order = {
        "id": "ord_x402",
        "domain": "example",
        "tld": "com",
        "amount_cents": 1812,
        "payment_method": "x402",
        "status": "pending_payment",
    }
    app = _wire_app_for_mpp(monkeypatch, order=order)

    with TestClient(app) as client:
        response = client.get("/pay/mpp/ord_x402")

    assert response.status_code == 400
    assert "Not an MPP order" in response.json()["detail"]


def test_pay_mpp_returns_404_for_missing_order(monkeypatch):
    app = _wire_app_for_mpp(monkeypatch, order=None)

    with TestClient(app) as client:
        response = client.get("/pay/mpp/ord_missing")

    assert response.status_code == 404
