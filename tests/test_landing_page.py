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
    assert "Tell your AI to buy the domain and keep shipping." in response.text
    assert "https://instadomain.fly.dev/mcp/" in response.text
    assert "GET /check/{domain}" in response.text
