from contextlib import asynccontextmanager

from fastapi.testclient import TestClient

from instadomain.api import create_app


@asynccontextmanager
async def _noop_lifespan(_app):
    yield


def test_buy_crypto_returns_disabled_message():
    app = create_app()
    app.router.lifespan_context = _noop_lifespan
    app.state.pool = object()
    app.state.encryption_key = "test-key"
    app.state.x402_enabled = False

    with TestClient(app) as client:
        response = client.post("/buy/crypto", json={
            "domain": "example.com",
            "registrant": {
                "first_name": "Test",
                "last_name": "User",
                "email": "test@example.com",
                "address1": "123 Main St",
                "city": "San Francisco",
                "state": "CA",
                "postal_code": "94102",
                "country": "US",
                "phone": "+1.5551234567",
            },
        })

    assert response.status_code == 503
    assert "Crypto payments not configured" in response.json()["detail"]
