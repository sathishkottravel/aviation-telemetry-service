"""Minimal HTTP health endpoint for hosts that require the worker to listen on $PORT (Render web services).

Answers every GET with 200 and {"status": "ok", "rabbitmq": <connected>}. Not started when PORT is unset.
Browsers from an allowed origin (CORS_ORIGINS) get CORS headers; OPTIONS preflights get 204.
"""

import asyncio
import json
import logging
from collections.abc import Callable, Collection

logger = logging.getLogger(__name__)


async def start_health_server(
    port: int, rabbitmq_connected: Callable[[], bool], allowed_origins: Collection[str] = ()
) -> asyncio.Server:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            method = (await reader.readline()).decode("latin-1").split(" ", 1)[0].upper()
            headers = {}
            while line := (await reader.readline()).strip():
                name, _, value = line.decode("latin-1").partition(":")
                headers[name.strip().lower()] = value.strip()

            cors = ""
            if (origin := headers.get("origin")) in allowed_origins:
                cors = f"Access-Control-Allow-Origin: {origin}\r\nVary: Origin\r\n"

            if method == "OPTIONS":
                requested = headers.get("access-control-request-headers", "")
                head = "HTTP/1.1 204 No Content\r\n" + cors
                if cors:
                    head += "Access-Control-Allow-Methods: GET, OPTIONS\r\nAccess-Control-Max-Age: 600\r\n"
                    if requested:
                        head += f"Access-Control-Allow-Headers: {requested}\r\n"
                writer.write((head + "Content-Length: 0\r\nConnection: close\r\n\r\n").encode())
            else:
                body = json.dumps({"status": "ok", "rabbitmq": rabbitmq_connected()}).encode()
                writer.write(
                    (
                        "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                        + cors
                        + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
                    ).encode()
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
