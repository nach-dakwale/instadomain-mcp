from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from instadomain import api as api_module
from instadomain.api import create_app


@asynccontextmanager
async def _noop_lifespan(_app):
    yield


def test_send_expiration_reminders_sends_and_reports_errors(monkeypatch):
    app = create_app()
    app.router.lifespan_context = _noop_lifespan
    app.state.pool = object()
    app.state.encryption_key = "test-key"

    expires_at = datetime.now(timezone.utc) + timedelta(days=14)
    orders = [
        {
            "id": "ord_ok",
            "domain": "example",
            "tld": "com",
            "email": "ok@example.com",
            "domain_expires_at": expires_at,
        },
        {
            "id": "ord_fail",
            "domain": "broken",
            "tld": "net",
            "email": "fail@example.com",
            "domain_expires_at": expires_at,
        },
    ]
    sent_calls = []

    async def fake_get_expiring_orders(_pool, _days):
        return orders

    async def fake_send_expiration_reminder_email(**kwargs):
        sent_calls.append(kwargs)
        if kwargs["domain"] == "broken.net":
            raise RuntimeError("smtp down")

    monkeypatch.setattr(api_module, "get_expiring_orders", fake_get_expiring_orders)
    monkeypatch.setattr(
        api_module,
        "send_expiration_reminder_email",
        fake_send_expiration_reminder_email,
    )
    monkeypatch.setattr(api_module, "Settings", lambda: type("S", (), {"backend_url": "https://backend.test"})())
    monkeypatch.setenv("INSTADOMAIN_ADMIN_KEY", "secret")

    with TestClient(app) as client:
        response = client.post(
            "/admin/send-expiration-reminders?days=30",
            headers={"x-api-key": "secret"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "count": 1,
        "errors": [
            {
                "order_id": "ord_fail",
                "domain": "broken.net",
                "error": "smtp down",
            }
        ],
    }
    assert len(sent_calls) == 2
    assert sent_calls[0]["renewal_url"] == "https://backend.test/renew/ord_ok"
    assert sent_calls[0]["expires_at"] == expires_at.isoformat()


def test_send_expiration_reminders_requires_api_key(monkeypatch):
    app = create_app()
    app.router.lifespan_context = _noop_lifespan
    app.state.pool = object()
    app.state.encryption_key = "test-key"
    monkeypatch.setenv("INSTADOMAIN_ADMIN_KEY", "secret")

    with TestClient(app) as client:
        response = client.post("/admin/send-expiration-reminders")

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid or missing API key"
