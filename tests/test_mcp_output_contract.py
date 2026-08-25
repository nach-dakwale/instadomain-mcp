"""Offline output-contract tests for the three read-only MCP tools.

Covers (no network beyond 127.0.0.1, no paid calls):
- tools/list emits structured output schemas for check_domain,
  check_domains_bulk, suggest_domains
- success arms and the bulk over-limit error arm execute and their real
  structured content validates against the advertised schema
- the 2024-11-05 client negotiation path still works (raw stdio + HTTP)
- bulk vs suggest summaries are distinct (bulk has summary.total, suggest does not)
- text-content compatibility is preserved
- non-target tools retain their existing untyped output shape
- stdio and streamable-HTTP projections

Run: PYTHONPATH=. .venv/bin/python -m pytest tests/test_mcp_output_contract.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import httpx
import jsonschema
import pytest
import uvicorn
from fastapi import FastAPI
from fastmcp import Client

import instadomain.mcp_server as mcp_server

REPO_ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------
# Stub backend mirroring instadomain/routes_check.py shapes exactly
# ---------------------------------------------------------------------------


def _build_stub_app() -> FastAPI:
    app = FastAPI()

    @app.get("/check/{domain}")
    async def check_domain(domain: str):
        if domain.startswith("taken"):
            return {"domain": domain.lower(), "available": False}
        if domain.startswith("pricefail"):
            return {
                "domain": domain.lower(),
                "available": True,
                "price_cents": None,
                "price_display": None,
            }
        if domain.startswith("extrameta"):
            # hostile: unvalidated extra upstream metadata must pass through
            return {
                "domain": domain.lower(),
                "available": True,
                "price_cents": 1855,
                "price_display": "$18.55",
                "wholesale_cents": 1000,
                "upstream_note": {"unvalidated": ["metadata"]},
            }
        if domain.startswith("malformed"):
            # hostile: backend contract violation -> must raise, not lie
            return {"available": "yes"}
        if domain.startswith("wrongtype"):
            # hostile: default Pydantic coercion would accept these strings.
            return {
                "domain": domain.lower(),
                "available": "true",
                "price_cents": "1855",
            }
        return {
            "domain": domain.lower(),
            "available": True,
            "price_cents": 1855,
            "price_display": "$18.55",
            "wholesale_cents": 1000,
        }

    @app.post("/check")
    async def check_bulk(body: dict):
        domains = body["domains"]
        if any(domain.startswith("wrongtype") for domain in domains):
            return {
                "summary": {"total": "1", "available": 1, "taken": 0},
                "available": [],
                "taken": [],
            }
        enriched = []
        for i, d in enumerate(domains):
            item = {"domain": d.lower(), "available": i % 2 == 0}
            if item["available"] and (d.startswith("a") or len(domains) == 1):
                item["register_urls"] = {
                    "dynadot": f"https://www.dynadot.com/domain/search?domain={d}"
                }
            enriched.append(item)
        available = [r for r in enriched if r["available"]]
        taken = [r for r in enriched if not r["available"]]
        return {
            "summary": {
                "total": len(enriched),
                "available": len(available),
                "taken": len(taken),
            },
            "available": available,
            "taken": taken,
        }

    @app.get("/suggest")
    async def suggest(keyword: str = ""):
        if keyword == "wrongtype":
            return {
                "keyword": keyword,
                "candidates_checked": "1",
                "summary": {"available": "1", "taken": 0},
                "available": [],
                "taken": [],
            }
        candidates = [
            f"{keyword}.com",
            f"get{keyword}.com",
            f"{keyword}.io",
            f"{keyword}.ai",
        ]
        enriched = []
        for i, c in enumerate(candidates):
            item = {"domain": c, "available": i % 2 == 0}
            if item["available"]:
                item["register_urls"] = {
                    "dynadot": f"https://www.dynadot.com/domain/search?domain={c}"
                }
            enriched.append(item)
        available = [r for r in enriched if r["available"]]
        taken = [r for r in enriched if not r["available"]]
        return {
            "keyword": keyword,
            "candidates_checked": len(enriched),
            "summary": {"available": len(available), "taken": len(taken)},
            "available": available,
            "taken": taken,
        }

    return app


class StubBackend:
    """In-process uvicorn server bound to an ephemeral loopback port."""

    def __init__(self):
        config = uvicorn.Config(
            _build_stub_app(), host="127.0.0.1", port=0, log_level="error"
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    @property
    def url(self) -> str:
        port = self.server.servers[0].sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}"

    def start(self) -> None:
        self.thread.start()
        while not self.server.started:
            if not self.thread.is_alive():
                raise RuntimeError("stub backend failed to start")

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


@pytest.fixture()
def stub_backend(monkeypatch):
    backend = StubBackend()
    backend.start()
    monkeypatch.setattr(mcp_server, "BACKEND_URL", backend.url)
    yield backend
    backend.stop()


def _validate_against_advertised_schema(structured, schema):
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(instance=structured, schema=schema)


def _resolve_local_ref(schema: dict, node: dict) -> dict:
    ref = node.get("$ref")
    if ref is None:
        return node
    assert ref.startswith("#/$defs/")
    return schema["$defs"][ref.removeprefix("#/$defs/")]


async def _tool_map(client: Client) -> dict:
    return {t.name: t for t in await client.list_tools()}


# ---------------------------------------------------------------------------
# tools/list structured output schemas
# ---------------------------------------------------------------------------


async def test_tools_list_emits_structured_output_schemas():
    async with Client(mcp_server.mcp) as client:
        tools = await _tool_map(client)

    cd = tools["check_domain"].outputSchema
    assert cd["type"] == "object"
    assert set(cd["required"]) >= {"domain", "available"}
    assert cd["properties"]["domain"]["type"] == "string"
    assert cd["properties"]["available"]["type"] == "boolean"
    for price in ("price_cents", "price_display", "wholesale_cents"):
        prop = cd["properties"][price]
        assert any(s.get("type") == "null" for s in prop.get("anyOf", [])), price
        # nullable and optional: never required, so null can appear on real paths
        assert price not in set(cd["required"])

    bulk = tools["check_domains_bulk"].outputSchema
    assert bulk["type"] == "object"
    assert "x-fastmcp-wrap-result" not in bulk
    arms = bulk["anyOf"]
    success_arm = next(a for a in arms if "summary" in a.get("properties", {}))
    error_arm = next(a for a in arms if "error" in a.get("properties", {}))
    assert set(success_arm["required"]) == {"summary", "available", "taken"}
    summary = _resolve_local_ref(bulk, success_arm["properties"]["summary"])
    assert set(summary["required"]) == {"total", "available", "taken"}
    for key in ("total", "available", "taken"):
        assert summary["properties"][key]["type"] == "integer"
    assert set(error_arm["required"]) == {"error", "limit"}
    assert error_arm["properties"]["error"]["type"] == "string"
    assert error_arm["properties"]["limit"]["type"] == "integer"

    sug = tools["suggest_domains"].outputSchema
    assert sug["type"] == "object"
    assert set(sug["required"]) >= {
        "keyword",
        "candidates_checked",
        "summary",
        "available",
        "taken",
    }
    s_summary = _resolve_local_ref(sug, sug["properties"]["summary"])
    assert set(s_summary["required"]) == {"available", "taken"}
    assert "total" not in s_summary["properties"], "suggest must not promise total"


# ---------------------------------------------------------------------------
# Success arms validated against advertised schemas
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("domain", "expect_available", "expect_prices"),
    [
        ("coolsite.com", True, True),
        ("taken-example.dev", False, False),
        ("pricefail.io", True, False),
        ("extrameta.ai", True, True),
    ],
)
async def test_check_domain_arms_validate_against_schema(
    stub_backend, domain, expect_available, expect_prices
):
    async with Client(mcp_server.mcp) as client:
        tools = await _tool_map(client)
        result = await client.call_tool("check_domain", {"domain": domain})

    _validate_against_advertised_schema(
        result.structured_content, tools["check_domain"].outputSchema
    )
    data = result.structured_content
    assert data["domain"] == domain
    assert data["available"] is expect_available
    if expect_prices:
        assert data["price_cents"] == 1855
        assert data["price_display"] == "$18.55"
        assert data["wholesale_cents"] == 1000
    else:
        assert not data.get("price_cents")
        assert not data.get("price_display")


async def test_check_domain_passes_unvalidated_extra_metadata(stub_backend):
    async with Client(mcp_server.mcp) as client:
        result = await client.call_tool("check_domain", {"domain": "extrameta.ai"})
    assert result.structured_content["upstream_note"] == {"unvalidated": ["metadata"]}


async def test_bulk_success_arm_validates_against_schema(stub_backend):
    domains = ["alpha.com", "beta.dev", "gamma.io"]
    async with Client(mcp_server.mcp) as client:
        tools = await _tool_map(client)
        result = await client.call_tool("check_domains_bulk", {"domains": domains})

    _validate_against_advertised_schema(
        result.structured_content, tools["check_domains_bulk"].outputSchema
    )
    payload = result.structured_content
    assert payload["summary"] == {"total": 3, "available": 2, "taken": 1}
    assert [i["domain"] for i in payload["available"]] == ["alpha.com", "gamma.io"]
    assert [i["domain"] for i in payload["taken"]] == ["beta.dev"]
    # per-domain items guarantee only proven fields; register_urls stays optional
    for item in payload["available"] + payload["taken"]:
        assert set(item) <= {"domain", "available", "register_urls"}
    # optional affiliate field: absent or null when not applicable
    assert all(not i.get("register_urls") for i in payload["taken"])
    assert all("register_urls" not in i or i["register_urls"] is None for i in payload["taken"])
    assert payload["available"][0]["register_urls"]["dynadot"].startswith(
        "https://www.dynadot.com/"
    )


async def test_bulk_boundary_exactly_50_is_success(stub_backend):
    domains = [f"z{i}.example{i % 10}.com" for i in range(50)]
    async with Client(mcp_server.mcp) as client:
        result = await client.call_tool("check_domains_bulk", {"domains": domains})
    payload = result.structured_content
    assert payload["summary"]["total"] == 50
    assert "error" not in payload


async def test_bulk_over_limit_error_arm(stub_backend):
    domains = [f"d{i}.example.com" for i in range(51)]
    async with Client(mcp_server.mcp) as client:
        tools = await _tool_map(client)
        result = await client.call_tool("check_domains_bulk", {"domains": domains})

    _validate_against_advertised_schema(
        result.structured_content, tools["check_domains_bulk"].outputSchema
    )
    payload = result.structured_content
    assert payload["limit"] == 50
    assert isinstance(payload["error"], str) and "51" in payload["error"]


async def test_suggest_arm_and_distinct_summary(stub_backend):
    async with Client(mcp_server.mcp) as client:
        tools = await _tool_map(client)
        result = await client.call_tool("suggest_domains", {"keyword": "taskflow"})

    _validate_against_advertised_schema(
        result.structured_content, tools["suggest_domains"].outputSchema
    )
    payload = result.structured_content
    assert payload["keyword"] == "taskflow"
    assert payload["candidates_checked"] > 0
    assert set(payload["summary"]) == {"available", "taken"}
    assert "total" not in payload["summary"], "suggest summary is distinct from bulk"

    # schema-level distinction too
    bulk = tools["check_domains_bulk"].outputSchema
    bulk_success = next(
        arm for arm in bulk["anyOf"] if "summary" in arm.get("properties", {})
    )
    bulk_summary = _resolve_local_ref(
        bulk, bulk_success["properties"]["summary"]
    )
    assert "total" in bulk_summary["required"]
    sug_schema = tools["suggest_domains"].outputSchema
    sug_summary = _resolve_local_ref(sug_schema, sug_schema["properties"]["summary"])
    assert "total" not in sug_summary["required"]
    assert "total" not in sug_summary["properties"]


async def test_hostile_malformed_backend_response_raises_not_lies(stub_backend):
    from fastmcp.exceptions import ToolError

    async with Client(mcp_server.mcp) as client:
        with pytest.raises(ToolError):
            await client.call_tool("check_domain", {"domain": "malformed.com"})


@pytest.mark.parametrize(
    ("tool_name", "args"),
    [
        ("check_domain", {"domain": "wrongtype.com"}),
        ("check_domains_bulk", {"domains": ["wrongtype.com"]}),
        ("suggest_domains", {"keyword": "wrongtype"}),
    ],
)
async def test_hostile_wrong_types_are_rejected_without_coercion(
    stub_backend, tool_name, args
):
    from fastmcp.exceptions import ToolError

    async with Client(mcp_server.mcp) as client:
        with pytest.raises(ToolError):
            await client.call_tool(tool_name, args)


async def test_absent_and_explicit_null_fields_remain_distinct(stub_backend):
    async with Client(mcp_server.mcp) as client:
        taken = await client.call_tool("check_domain", {"domain": "taken-example.dev"})
        failed = await client.call_tool("check_domain", {"domain": "pricefail.io"})

    assert "price_cents" not in taken.structured_content
    assert "price_display" not in taken.structured_content
    assert failed.structured_content["price_cents"] is None
    assert failed.structured_content["price_display"] is None


# ---------------------------------------------------------------------------
# Text-content compatibility
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool_name", "args"),
    [
        ("check_domain", {"domain": "coolsite.com"}),
        ("check_domains_bulk", {"domains": ["alpha.com"]}),
        ("check_domains_bulk", {"domains": [f"d{i}.example.com" for i in range(51)]}),
        ("suggest_domains", {"keyword": "taskflow"}),
    ],
)
async def test_text_content_remains_plain_json_payload(stub_backend, tool_name, args):
    async with Client(mcp_server.mcp) as client:
        result = await client.call_tool(tool_name, args)
    assert len(result.content) == 1
    text_payload = json.loads(result.content[0].text)
    structured = result.structured_content
    assert text_payload == structured


async def test_non_target_tools_remain_untyped():
    """Only the three check and suggestion tools gain output contracts."""
    async with Client(mcp_server.mcp) as client:
        tools = await _tool_map(client)
    for name in (
        "buy_domain",
        "buy_domain_crypto",
        "buy_domain_mpp",
        "get_domain_status",
        "request_transfer_code",
        "verify_transfer_code",
        "get_transfer_code",
        "unlock_domain",
        "renew_domain",
    ):
        schema = tools[name].outputSchema
        assert schema == {"type": "object", "additionalProperties": True}, name


# ---------------------------------------------------------------------------
# stdio projection + 2024-11-05 negotiation (raw JSON-RPC, no SDK magic)
# ---------------------------------------------------------------------------


def _raw_stdio_roundtrip(proc, msg: dict) -> dict:
    proc.stdin.write((json.dumps(msg) + "\n").encode())
    proc.stdin.flush()
    while True:
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError("stdio server closed unexpectedly")
        parsed = json.loads(line)
        if parsed.get("id") == msg.get("id"):
            return parsed


def _spawn_stdio(entry_args, env_extra):
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT), **env_extra}
    return subprocess.Popen(
        entry_args,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
        cwd=str(REPO_ROOT),
    )


def _stdio_tools_list(proc) -> dict:
    listing = _raw_stdio_roundtrip(
        proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
    )
    return {t["name"]: t for t in listing["result"]["tools"]}


def _initialize_2024_11_05(proc) -> None:
    init = _raw_stdio_roundtrip(
        proc,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "raw-contract-test", "version": "0.0.0"},
            },
        },
    )
    assert init["result"]["protocolVersion"] == "2024-11-05"
    proc.stdin.write(
        (
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
            + "\n"
        ).encode()
    )
    proc.stdin.flush()


def test_stdio_module_entry_negotiates_and_lists_schemas(stub_backend):
    """The repo's own `python -m instadomain.mcp_server` stdio entry point.

    Pre-existing quirk (verified identical at base commit 9a5ad8a): running via
    -m re-imports instadomain.mcp_server under its package name, so the buy
    tools register on a second, unserved FastMCP instance and only the nine
    non-buy tools are listed. This test locks that behavior while proving our
    three read-only tools advertise output schemas over raw stdio.
    """
    proc = _spawn_stdio(
        [sys.executable, "-m", "instadomain.mcp_server"],
        {"INSTADOMAIN_BACKEND_URL": stub_backend.url},
    )
    try:
        _initialize_2024_11_05(proc)
        by_name = _stdio_tools_list(proc)
        assert set(by_name) == {
            "check_domain",
            "check_domains_bulk",
            "suggest_domains",
            "get_domain_status",
            "request_transfer_code",
            "verify_transfer_code",
            "get_transfer_code",
            "unlock_domain",
            "renew_domain",
        }
        for name in ("check_domain", "check_domains_bulk", "suggest_domains"):
            assert by_name[name].get("outputSchema"), f"{name} missing outputSchema over stdio"
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_stdio_import_entry_lists_all_tools_without_retyping_non_targets(stub_backend):
    """Import-style stdio projection serves all 12 tools on one instance."""
    code = "from instadomain.mcp_server import mcp; mcp.run(transport='stdio')"
    proc = _spawn_stdio(
        [sys.executable, "-c", code],
        {"INSTADOMAIN_BACKEND_URL": stub_backend.url},
    )
    try:
        _initialize_2024_11_05(proc)
        by_name = _stdio_tools_list(proc)
        assert len(by_name) == 12
        for name in ("buy_domain", "buy_domain_crypto", "buy_domain_mpp"):
            assert by_name[name]["outputSchema"] == {
                "type": "object",
                "additionalProperties": True,
            }, name
    finally:
        proc.kill()
        proc.wait(timeout=10)


# ---------------------------------------------------------------------------
# streamable-HTTP projection
# ---------------------------------------------------------------------------


class _HttpServer:
    def __init__(self, app):
        config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    @property
    def url(self):
        port = self.server.servers[0].sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}"

    def start(self):
        self.thread.start()
        while not self.server.started:
            if not self.thread.is_alive():
                raise RuntimeError("http mcp server failed to start")

    def stop(self):
        self.server.should_exit = True
        self.thread.join(timeout=10)


def _parse_rpc_response(resp: httpx.Response) -> dict:
    ctype = resp.headers.get("content-type", "")
    body = resp.text
    if "text/event-stream" in ctype:
        for line in body.splitlines():
            if line.startswith("data:"):
                return json.loads(line[len("data:") :].strip())
        raise AssertionError(f"no SSE data frame: {body!r}")
    return json.loads(body)


ACCEPT_SSE = "application/json, text/event-stream"


def test_streamable_http_projection_full_contract(stub_backend):
    app = mcp_server.mcp.http_app(path="/mcp")
    srv = _HttpServer(app)
    srv.start()
    try:
        with httpx.Client(base_url=srv.url, timeout=30) as http:
            init = http.post(
                "/mcp",
                headers={"Accept": ACCEPT_SSE},
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "raw-http-test", "version": "0.0.0"},
                    },
                },
            )
            assert init.status_code == 200, init.text
            init_resp = _parse_rpc_response(init)
            assert init_resp["result"]["protocolVersion"] == "2024-11-05"
            session = init.headers.get("mcp-session-id")
            assert session

            notified = http.post(
                "/mcp",
                headers={"Accept": ACCEPT_SSE, "Mcp-Session-Id": session},
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )
            assert notified.status_code in (200, 202, 204), notified.text

            def rpc(id_, name, params):
                return _parse_rpc_response(
                    http.post(
                        "/mcp",
                        headers={"Accept": ACCEPT_SSE, "Mcp-Session-Id": session},
                        json={"jsonrpc": "2.0", "id": id_, "method": name, "params": params},
                    )
                )

            listing = rpc(2, "tools/list", {})
            by_name = {t["name"]: t for t in listing["result"]["tools"]}
            for name in ("check_domain", "check_domains_bulk", "suggest_domains"):
                assert by_name[name].get("outputSchema"), f"{name} missing outputSchema over HTTP"
            for name in ("buy_domain", "buy_domain_crypto", "buy_domain_mpp"):
                assert by_name[name]["outputSchema"] == {
                    "type": "object",
                    "additionalProperties": True,
                }, name

            # end-to-end structured content over HTTP: bulk-limit arm
            call = rpc(
                3,
                "tools/call",
                {
                    "name": "check_domains_bulk",
                    "arguments": {"domains": [f"d{i}.example.com" for i in range(51)]},
                },
            )
            structured = call["result"]["structuredContent"]
            jsonschema.validate(
                instance=structured, schema=by_name["check_domains_bulk"]["outputSchema"]
            )
            assert structured["limit"] == 50
            assert json.loads(call["result"]["content"][0]["text"]) == structured

            # end-to-end success arm over HTTP
            call = rpc(4, "tools/call", {"name": "suggest_domains", "arguments": {"keyword": "taskflow"}})
            structured = call["result"]["structuredContent"]
            jsonschema.validate(
                instance=structured, schema=by_name["suggest_domains"]["outputSchema"]
            )
            assert structured["keyword"] == "taskflow"
            assert "total" not in structured["summary"]
    finally:
        srv.stop()
