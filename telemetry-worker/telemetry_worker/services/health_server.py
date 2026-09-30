"""Minimal HTTP health endpoint for hosts that require the worker to listen on $PORT (Render web services).

Answers every request with 200 and {"status": "ok", "rabbitmq": <connected>}. Not started when PORT is unset.
"""

import asyncio
import json
import logging
from collections.abc import Callable

logger = logging.getLogger(__name__)


async def start_health_server(port: int, rabbitmq_connected: Callable[[], bool]) -> asyncio.Server:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            # Read the request line and headers; the answer does not depend on them.
            while (await reader.readline()).strip():
                pass
            body = json.dumps({"status": "ok", "rabbitmq": rabbitmq_connected()}).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                + body
            )
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "0.0.0.0", port)
    logger.info("Health endpoint listening on port %d", port)
    return server
