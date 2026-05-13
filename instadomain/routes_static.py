"""Static page routes: health, index, success, cancel, terms, privacy, refunds, llms.txt, mcp.json."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse

router = APIRouter()

_STATIC_DIR = Path(__file__).parent / "static"


@router.get("/health")
async def health():
    return {"status": "ok"}


@router.get("/success", response_class=HTMLResponse)
async def success():
    return (
        "<html><body><h1>Domain purchase successful!</h1>"
        "<p>You can close this tab. Your AI assistant is tracking the order.</p>"
        "</body></html>"
    )


@router.get("/cancel", response_class=HTMLResponse)
async def cancel():
    return (
        "<html><body><h1>Purchase cancelled</h1>"
        "<p>No charge was made. You can close this tab.</p>"
        "</body></html>"
    )


@router.get("/", response_class=HTMLResponse)
async def index():
    return (_STATIC_DIR / "index.html").read_text()


@router.get("/llms.txt")
async def llms_txt():
    return PlainTextResponse((_STATIC_DIR / "llms.txt").read_text())


@router.get("/.well-known/mcp.json")
async def mcp_json():
    import json

    return JSONResponse(json.loads((_STATIC_DIR / "mcp.json").read_text()))


@router.get("/terms", response_class=HTMLResponse)
async def terms():
    return (_STATIC_DIR / "terms.html").read_text()


@router.get("/privacy", response_class=HTMLResponse)
async def privacy():
    return (_STATIC_DIR / "privacy.html").read_text()


@router.get("/refunds", response_class=HTMLResponse)
async def refunds():
    return (_STATIC_DIR / "refunds.html").read_text()


@router.get("/og.png")
async def og_image():
    return FileResponse(_STATIC_DIR / "og.png", media_type="image/png")


@router.get("/tiktokClCiYUj0VJTwnwirfKCsSIRSQGDfUcYE.txt")
async def tiktok_verification():
    return PlainTextResponse((_STATIC_DIR / "tiktokClCiYUj0VJTwnwirfKCsSIRSQGDfUcYE.txt").read_text())
