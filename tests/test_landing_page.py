from contextlib import asynccontextmanager

from fastapi.testclient import TestClient

from instadomain.api import create_app


@asynccontextmanager
async def _noop_lifespan(_app):
    yield


def test_root_serves_landing_page():
    app = create_app()
    app.router.lifespan_context = _noop_lifespan

    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "buys the domain." in response.text
    assert "You keep building." in response.text
    assert "https://instadomain.fly.dev/mcp/" in response.text
    assert "GET /check/{domain}" in response.text
    assert 'href="/refunds"' in response.text


def test_landing_page_includes_faq_section():
    app = create_app()
    app.router.lifespan_context = _noop_lifespan

    with TestClient(app) as client:
        response = client.get("/")

    assert "Do I actually own the domain?" in response.text
    assert "How does DNS get set up" in response.text
    assert "Can I transfer my domain" in response.text
    assert "DNS somewhere other than Cloudflare" in response.text


def test_refunds_page_renders():
    app = create_app()
    app.router.lifespan_context = _noop_lifespan

    with TestClient(app) as client:
        response = client.get("/refunds")

    assert response.status_code == 200
    assert "Refund and Return Policy" in response.text
    assert "Stripe MPP" in response.text
    assert "x402" in response.text
