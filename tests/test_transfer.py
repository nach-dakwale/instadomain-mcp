"""Integration tests for the email-verified transfer flow."""
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from instadomain import routes_transfer as transfer_module
from instadomain.api import create_app
from tests.conftest import MockConn, MockPool, noop_lifespan

_COMPLETE_ORDER = {
    "id": "ord_123",
    "domain": "example",
    "tld": "com",
    "status": "complete",
    "email": "owner@example.com",
    "registrant_contact": None,
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
# request_transfer_code
# ---------------------------------------------------------------------------

def test_request_code_sends_email_to_registrant(monkeypatch):
    app, conn = _make_app()
    sent = []

    async def fake_get_order(_p, _id):
        return _COMPLETE_ORDER

    async def fake_send(**kw):
        sent.append(kw)

    monkeypatch.setattr(transfer_module, "get_order", fake_get_order)
    monkeypatch.setattr(transfer_module, "send_transfer_verification_email", fake_send)

    with TestClient(app) as client:
        resp = client.post("/transfer/request-code/ord_123")

    assert resp.status_code == 200
    assert "example.com" in resp.json()["message"] or "@" in resp.json()["message"]
    assert len(sent) == 1
    assert sent[0]["to_email"] == "owner@example.com"
    assert sent[0]["domain"] == "example.com"
    assert len(sent[0]["code"]) == 6
    assert conn.execute.call_count == 2  # invalidate old codes + insert new


def test_request_code_falls_back_to_registrant_contact_email(monkeypatch):
    order = {**_COMPLETE_ORDER, "email": None, "registrant_contact": {"email": "crypto@test.com"}}
    app, _ = _make_app()
    sent = []

    async def fake_get_order(_p, _id):
        return order

    async def fake_send(**kw):
        sent.append(kw)

    monkeypatch.setattr(transfer_module, "get_order", fake_get_order)
    monkeypatch.setattr(transfer_module, "send_transfer_verification_email", fake_send)

    with TestClient(app) as client:
        resp = client.post("/transfer/request-code/ord_123")

    assert resp.status_code == 200
    assert sent[0]["to_email"] == "crypto@test.com"


def test_request_code_returns_404_for_missing_order(monkeypatch):
    app, _ = _make_app()

    async def fake_get_order(_p, _id):
        return None

    monkeypatch.setattr(transfer_module, "get_order", fake_get_order)

    with TestClient(app) as client:
        resp = client.post("/transfer/request-code/ord_missing")

    assert resp.status_code == 404


def test_request_code_returns_400_for_non_complete_order(monkeypatch):
    app, _ = _make_app()

    async def fake_get_order(_p, _id):
        return {**_COMPLETE_ORDER, "status": "pending_payment"}

    monkeypatch.setattr(transfer_module, "get_order", fake_get_order)

    with TestClient(app) as client:
        resp = client.post("/transfer/request-code/ord_123")

    assert resp.status_code == 400
    assert "complete" in resp.json()["detail"]


def test_request_code_returns_400_when_no_email(monkeypatch):
    app, _ = _make_app()

    async def fake_get_order(_p, _id):
        return {**_COMPLETE_ORDER, "email": None, "registrant_contact": None}

    monkeypatch.setattr(transfer_module, "get_order", fake_get_order)

    with TestClient(app) as client:
        resp = client.post("/transfer/request-code/ord_123")

    assert resp.status_code == 400
    assert "email" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# verify_transfer_code
# ---------------------------------------------------------------------------

def test_verify_code_returns_transfer_token():
    conn = MockConn(fetchrow_return={"id": "tv_abc"})
    app, _ = _make_app(conn)

    with TestClient(app) as client:
        resp = client.post("/transfer/verify-code/ord_123", json={"code": "123456"})

    assert resp.status_code == 200
    data = resp.json()
    assert "transfer_token" in data
    assert len(data["transfer_token"]) == 32
    assert data["expires_in_minutes"] == 15
    conn.execute.assert_called_once()


def test_verify_code_returns_400_for_invalid_code():
    conn = MockConn(fetchrow_return=None)
    app, _ = _make_app(conn)

    with TestClient(app) as client:
        resp = client.post("/transfer/verify-code/ord_123", json={"code": "000000"})

    assert resp.status_code == 400
    assert "Invalid or expired" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# get_transfer_code (token-gated)
# ---------------------------------------------------------------------------

def test_get_transfer_code_blocked_without_token(monkeypatch):
    conn = MockConn(fetchrow_return=None)
    app, _ = _make_app(conn)

    monkeypatch.setattr(transfer_module, "get_order", lambda *_: (_ for _ in ()).throw(AssertionError("should not reach")))

    with TestClient(app) as client:
        resp = client.get("/transfer-code/ord_123")

    assert resp.status_code == 401
    assert "Transfer token required" in resp.json()["detail"]


def test_get_transfer_code_returns_401_for_expired_token(monkeypatch):
    conn = MockConn(fetchrow_return=None)
    app, _ = _make_app(conn)

    async def fake_get_order(_p, _id):
        return _COMPLETE_ORDER

    monkeypatch.setattr(transfer_module, "get_order", fake_get_order)

    with TestClient(app) as client:
        resp = client.get("/transfer-code/ord_123", headers={"x-transfer-token": "expired"})

    assert resp.status_code == 401
    assert "Invalid or expired" in resp.json()["detail"]


def test_get_transfer_code_returns_epp_code(monkeypatch):
    conn = MockConn(fetchrow_return={"id": "tv_abc"})
    app, _ = _make_app(conn)
    app.state.opensrs.get_transfer_auth_code = MagicMock(return_value="EPP-ABC-123")

    async def fake_get_order(_p, _id):
        return _COMPLETE_ORDER

    monkeypatch.setattr(transfer_module, "get_order", fake_get_order)

    with TestClient(app) as client:
        resp = client.get("/transfer-code/ord_123", headers={"x-transfer-token": "valid"})

    assert resp.status_code == 200
    assert resp.json()["auth_code"] == "EPP-ABC-123"
    assert resp.json()["domain"] == "example.com"


# ---------------------------------------------------------------------------
# unlock_domain (token-gated)
# ---------------------------------------------------------------------------

def test_unlock_domain_blocked_without_token(monkeypatch):
    conn = MockConn(fetchrow_return=None)
    app, _ = _make_app(conn)

    with TestClient(app) as client:
        resp = client.post("/unlock/ord_123")

    assert resp.status_code == 401


def test_unlock_domain_succeeds_with_valid_token(monkeypatch):
    conn = MockConn(fetchrow_return={"id": "tv_abc"})
    app, _ = _make_app(conn)
    app.state.opensrs.unlock_domain = MagicMock(return_value=None)

    async def fake_get_order(_p, _id):
        return _COMPLETE_ORDER

    monkeypatch.setattr(transfer_module, "get_order", fake_get_order)

    with TestClient(app) as client:
        resp = client.post("/unlock/ord_123", headers={"x-transfer-token": "valid"})

    assert resp.status_code == 200
    assert resp.json()["unlocked"] is True
    assert resp.json()["domain"] == "example.com"
