from contextlib import asynccontextmanager

from fastapi.testclient import TestClient

from instadomain import routes_x402 as x402_module
from instadomain.api import create_app


@asynccontextmanager
async def _noop_lifespan(_app):
    yield


def test_pay_rejects_non_pending_x402_order(monkeypatch):
    app = create_app()
    app.router.lifespan_context = _noop_lifespan
    app.state.pool = object()
    app.state.encryption_key = "test-key"
    app.state.x402_enabled = True
    app.state.x402_resource_server = object()
    app.state.x402_wallet = "0xwallet"
    app.state.x402_network = "eip155:8453"

    async def fake_get_order(_pool, _order_id):
        return {
            "id": "ord_paid",
            "payment_method": "x402",
            "status": "complete",
        }

    async def fake_get_pending_x402_order(_pool, _order_id):
        raise AssertionError("pending x402 lookup should not run for non-pending orders")

    monkeypatch.setattr(x402_module, "get_order", fake_get_order)
    monkeypatch.setattr(x402_module, "get_pending_x402_order", fake_get_pending_x402_order)

    with TestClient(app) as client:
        response = client.get("/pay/ord_paid")

    assert response.status_code == 409
    assert "cannot be paid again" in response.json()["detail"]
