"""The worker's $PORT health endpoint (used when hosted as a Render web service)."""

import asyncio
import json

from telemetry_worker.services.health_server import start_health_server


async def test_health_answers_200_with_rabbitmq_state():
    connected = {"value": False}
    server = await start_health_server(0, lambda: connected["value"])
    port = server.sockets[0].getsockname()[1]
    try:
        async def get() -> tuple[bytes, dict]:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"GET /health HTTP/1.1\r\nHost: worker\r\n\r\n")
            await writer.drain()
            head, _, body = (await reader.read()).partition(b"\r\n\r\n")
            writer.close()
            return head.split(b"\r\n")[0], json.loads(body)

        assert await get() == (b"HTTP/1.1 200 OK", {"status": "ok", "rabbitmq": False})
        connected["value"] = True
        assert (await get())[1]["rabbitmq"] is True
    finally:
        server.close()
        await server.wait_closed()
