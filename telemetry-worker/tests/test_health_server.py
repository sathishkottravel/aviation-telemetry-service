"""The worker's $PORT health endpoint (used when hosted as a Render web service)."""

import asyncio
import json

import pytest

from telemetry_worker.services.health_server import start_health_server

ORIGIN = "https://sathishkottravel.github.io"


@pytest.fixture
async def server():
    connected = {"value": False}
    srv = await start_health_server(0, lambda: connected["value"], [ORIGIN])
    yield srv.sockets[0].getsockname()[1], connected
    srv.close()
    await srv.wait_closed()


async def request(port: int, raw: str) -> tuple[str, dict[str, str], bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(raw.encode())
    await writer.drain()
    head, _, body = (await reader.read()).partition(b"\r\n\r\n")
    writer.close()
    status, *lines = head.decode().split("\r\n")
    return status, {k.lower(): v.strip() for k, _, v in (line.partition(":") for line in lines)}, body


async def test_health_answers_200_with_rabbitmq_state(server):
    port, connected = server
    status, headers, body = await request(port, "GET /health HTTP/1.1\r\nHost: worker\r\n\r\n")
    assert (status, json.loads(body)) == ("HTTP/1.1 200 OK", {"status": "ok", "rabbitmq": False})
    assert "access-control-allow-origin" not in headers
    connected["value"] = True
    assert json.loads((await request(port, "GET /health HTTP/1.1\r\n\r\n"))[2])["rabbitmq"] is True


async def test_cors_for_allowed_origin_only(server):
    port, _ = server
    _, headers, _ = await request(port, f"GET /health HTTP/1.1\r\nOrigin: {ORIGIN}\r\n\r\n")
    assert headers["access-control-allow-origin"] == ORIGIN
    _, headers, _ = await request(port, "GET /health HTTP/1.1\r\nOrigin: https://evil.example\r\n\r\n")
    assert "access-control-allow-origin" not in headers


async def test_preflight_gets_204(server):
    port, _ = server
    status, headers, _ = await request(port, (
        f"OPTIONS /health HTTP/1.1\r\nOrigin: {ORIGIN}\r\nAccess-Control-Request-Method: GET\r\n"
        "Access-Control-Request-Headers: content-type\r\n\r\n"))
    assert status == "HTTP/1.1 204 No Content"
    assert headers["access-control-allow-methods"] == "GET, OPTIONS"
    assert headers["access-control-allow-headers"] == "content-type"
