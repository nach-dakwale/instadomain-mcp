from contextlib import asynccontextmanager

from fastapi.testclient import TestClient

from instadomain.api import create_app


@asynccontextmanager
async def _noop_lifespan(_app):
    yield


def _registrant() -> dict:
    return {
        "first_name": "Test",
        "last_name": "User",
        "email": "test@example.com",
        "address1": "123 Main St",
        "city": "San Francisco",
        "state": "CA",
        "postal_code": "94102",
        "country": "US",
        "phone": "+1.5551234567",
    }


def test_buy_mpp_returns_disabled_message():
    app = create_app()
    app.router.lifespan_context = _noop_lifespan
    app.state.pool = object()
    app.state.encryption_key = "test-key"
    app.state.mpp_enabled = False

    with TestClient(app) as client:
        response = client.post(
            "/buy/mpp",
            json={"domain": "example.com", "registrant": _registrant()},
        )

    assert response.status_code == 503
    assert "MPP payments not configured" in response.json()["detail"]


def test_pay_mpp_returns_disabled_message():
    app = create_app()
    app.router.lifespan_context = _noop_lifespan
    app.state.pool = object()
    app.state.encryption_key = "test-key"
    app.state.mpp_enabled = False

    with TestClient(app) as client:
        response = client.get("/pay/mpp/ord_anything")

    assert response.status_code == 503
    assert "MPP payments not configured" in response.json()["detail"]
