from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from instadomain import api as api_module
from instadomain.api import create_app


@asynccontextmanager
async def _noop_lifespan(_app):
    yield


def test_status_allows_retrieving_dns_token_multiple_times(monkeypatch):
    app = create_app()
    app.router.lifespan_context = _noop_lifespan
    app.state.pool = object()
    app.state.encryption_key = "test-key"

    order = {
        "id": "ord_123",
        "domain": "example",
        "tld": "com",
        "status": "complete",
        "amount_cents": 1999,
        "created_at": datetime.now(timezone.utc),
        "completed_at": datetime.now(timezone.utc),
        "nameservers": ["a.ns.cloudflare.com", "b.ns.cloudflare.com"],
        "error_msg": None,
        "cloudflare_api_token": "encrypted-token",
    }

    async def fake_get_order(_pool, _order_id):
        return order

    monkeypatch.setattr(api_module, "get_order", fake_get_order)
    monkeypatch.setattr(api_module, "decrypt", lambda token, _key: f"plain:{token}")

    with TestClient(app) as client:
        first = client.get("/status/ord_123")
        second = client.get("/status/ord_123")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["cloudflare_token"] == "plain:encrypted-token"
    assert second.json()["cloudflare_token"] == "plain:encrypted-token"
