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
    app.state.x402_enabled = True

    with TestClient(app) as client:
        response = client.post("/buy/crypto", json={"domain": "example.com"})

    assert response.status_code == 503
    assert response.json()["message"].startswith("Crypto purchases are temporarily disabled")
