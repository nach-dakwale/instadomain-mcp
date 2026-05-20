"""Integration tests for domain check and suggest endpoints."""
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from instadomain import routes_check as check_module
from instadomain.api import create_app
from tests.conftest import noop_lifespan


def _make_app():
    app = create_app()
    app.router.lifespan_context = noop_lifespan
    app.state.pool = object()
    app.state.encryption_key = "test-key"
    app.state.opensrs = MagicMock()
    return app


# ---------------------------------------------------------------------------
# GET /check/{domain}
# ---------------------------------------------------------------------------

def test_check_available_domain_returns_price(monkeypatch):
    app = _make_app()

    async def fake_check(domain):
        return {"domain": domain, "available": True}

    async def fake_get_price(domain, opensrs):
        return 1450

    monkeypatch.setattr(check_module, "_check_availability", fake_check)
    monkeypatch.setattr(check_module, "_get_price", fake_get_price)

    with TestClient(app) as client:
        resp = client.get("/check/example.com")

    assert resp.status_code == 200
    data = resp.json()
    assert data["available"] is True
    assert data["price_cents"] > 1450  # retail markup applied
    assert data["price_display"].startswith("$")
    assert data["wholesale_cents"] == 1450


def test_check_unavailable_domain_omits_price(monkeypatch):
    app = _make_app()

    async def fake_check(domain):
        return {"domain": domain, "available": False}

    monkeypatch.setattr(check_module, "_check_availability", fake_check)

    with TestClient(app) as client:
        resp = client.get("/check/taken.com")

    assert resp.status_code == 200
    data = resp.json()
    assert data["available"] is False
    assert "price_cents" not in data


def test_check_domain_price_failure_returns_null_price(monkeypatch):
    app = _make_app()

    async def fake_check(domain):
        return {"domain": domain, "available": True}

    async def fake_get_price(domain, opensrs):
        raise RuntimeError("OpenSRS unavailable")

    monkeypatch.setattr(check_module, "_check_availability", fake_check)
    monkeypatch.setattr(check_module, "_get_price", fake_get_price)

    with TestClient(app) as client:
        resp = client.get("/check/example.com")

    assert resp.status_code == 200
    assert resp.json()["available"] is True
    assert resp.json()["price_cents"] is None
    assert resp.json()["price_display"] is None


# ---------------------------------------------------------------------------
# POST /check (bulk)
# ---------------------------------------------------------------------------

def test_check_bulk_categorizes_available_and_taken(monkeypatch):
    app = _make_app()

    async def fake_rdap(domains):
        return [
            {"domain": "free.com", "available": True},
            {"domain": "taken.com", "available": False},
            {"domain": "also-free.io", "available": True},
        ]

    monkeypatch.setattr(check_module, "_check_domains_rdap", fake_rdap)

    with TestClient(app) as client:
        resp = client.post("/check", json={"domains": ["free.com", "taken.com", "also-free.io"]})

    assert resp.status_code == 200
    data = resp.json()
    assert data["summary"]["total"] == 3
    assert data["summary"]["available"] == 2
    assert data["summary"]["taken"] == 1
    assert data["available"][0]["domain"] == "free.com"


def test_check_bulk_rejects_over_50_domains():
    app = _make_app()

    with TestClient(app) as client:
        resp = client.post("/check", json={"domains": [f"d{i}.com" for i in range(51)]})

    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /suggest
# ---------------------------------------------------------------------------

def test_suggest_returns_available_and_taken_candidates(monkeypatch):
    app = _make_app()

    def fake_generate(keyword):
        return ["taskflow.com", "taskflow.io", "gettaskflow.com"]

    async def fake_rdap(domains):
        return [
            {"domain": "taskflow.com", "available": True},
            {"domain": "taskflow.io", "available": False},
            {"domain": "gettaskflow.com", "available": True},
        ]

    monkeypatch.setattr(check_module, "_generate_candidates", fake_generate)
    monkeypatch.setattr(check_module, "_check_domains_rdap", fake_rdap)

    with TestClient(app) as client:
        resp = client.get("/suggest?keyword=taskflow")

    assert resp.status_code == 200
    data = resp.json()
    assert data["keyword"] == "taskflow"
    assert data["candidates_checked"] == 3
    assert data["summary"]["available"] == 2
    assert data["summary"]["taken"] == 1


def test_suggest_requires_keyword():
    app = _make_app()

    with TestClient(app) as client:
        resp = client.get("/suggest")

    assert resp.status_code == 422
