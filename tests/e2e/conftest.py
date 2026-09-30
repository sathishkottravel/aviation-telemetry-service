"""End-to-end fixtures: the running stack (docker compose up), reached over HTTP and WebSocket.

Settings (environment): E2E_API_URL (default http://localhost:8000), E2E_MONGODB_URI / E2E_MONGODB_DB for
seeding the stack's database (defaults: mongodb://localhost:27017, aviation), E2E_ADMIN_TOKEN to clean up
test telemetry through DELETE /api/telemetry (otherwise the TTL removes it), E2E_API_TOKEN when the API
requires API_TOKEN (sent as the Authorization header and in the subscription connection_init payload).
"""

import asyncio
import json
import os
import uuid
from contextlib import asynccontextmanager

import httpx
import pytest
import websockets

API_URL = os.environ.get("E2E_API_URL", "http://localhost:8000")
WS_URL = API_URL.replace("http", "ws", 1) + "/graphql"
ADMIN_TOKEN = os.environ.get("E2E_ADMIN_TOKEN", "")
API_TOKEN = os.environ.get("E2E_API_TOKEN", "")
AUTH_HEADERS = {"Authorization": f"Bearer {API_TOKEN}"} if API_TOKEN else {}


class GraphQLClient:
    def __init__(self, http: httpx.AsyncClient):
        self.http = http

    async def execute(self, query: str, variables: dict | None = None) -> dict:
        response = await self.http.post("/graphql", json={"query": query, "variables": variables or {}})
        response.raise_for_status()
        return response.json()

    async def data(self, query: str, variables: dict | None = None) -> dict:
        body = await self.execute(query, variables)
        assert "errors" not in body, body["errors"]
        return body["data"]

    async def error_code(self, query: str, variables: dict | None = None) -> str:
        body = await self.execute(query, variables)
        return body["errors"][0]["extensions"]["code"]

    @asynccontextmanager
    async def subscribe(self, query: str, variables: dict | None = None):
        """Open a graphql-transport-ws subscription; yields an async next() for the next payload's data."""
        async with websockets.connect(WS_URL, subprotocols=["graphql-transport-ws"]) as ws:
            init = {"type": "connection_init"}
            if API_TOKEN:
                init["payload"] = {"authorization": f"Bearer {API_TOKEN}"}
            await ws.send(json.dumps(init))
            assert json.loads(await ws.recv())["type"] == "connection_ack"
            await ws.send(json.dumps({"id": "1", "type": "subscribe",
                                      "payload": {"query": query, "variables": variables or {}}}))
            await asyncio.sleep(0.3)  # let the server register the subscriber

            async def next_data(timeout: float = 15):
                message = json.loads(await asyncio.wait_for(ws.recv(), timeout))
                assert message["type"] == "next", message
                return message["payload"]["data"]

            yield next_data


@pytest.fixture
async def api():
    async with httpx.AsyncClient(base_url=API_URL, timeout=20, headers=AUTH_HEADERS) as http:
        try:
            (await http.get("/health")).raise_for_status()
        except httpx.HTTPError:
            pytest.skip(f"stack not running at {API_URL} (docker compose up)")
        yield http


@pytest.fixture
def gql(api) -> GraphQLClient:
    return GraphQLClient(api)


@pytest.fixture
async def flight_id(api):
    """A unique flight ID per test; its telemetry is pruned afterwards when an admin token is available."""
    value = f"E2E-{uuid.uuid4().hex[:8].upper()}"
    yield value
    if ADMIN_TOKEN:
        await api.delete("/api/telemetry", params={"flight_id": value}, headers={"X-Admin-Token": ADMIN_TOKEN})


@pytest.fixture(scope="session")
def seeded():
    """Seed the stack's database (idempotent) so navigation queries have the ESSP -> ESSA demo data."""
    from telemetry_shared.config import get_settings
    from telemetry_shared.database import seed

    os.environ["MONGODB_URI"] = os.environ.get("E2E_MONGODB_URI", "mongodb://localhost:27017")
    os.environ["MONGODB_DB"] = os.environ.get("E2E_MONGODB_DB", "aviation")
    get_settings.cache_clear()
    try:
        asyncio.run(seed.seed())
    except Exception as exc:
        pytest.skip(f"cannot seed the stack's MongoDB: {exc}")
    finally:
        os.environ["MONGODB_DB"] = "aviation_test"
        get_settings.cache_clear()


def telemetry_payload(flight_id: str, **overrides) -> dict:
    return {"flight_id": flight_id, "callsign": "E2ETEST", "timestamp": "2026-09-29T12:00:00Z", "latitude": 59.35,
            "longitude": 17.94, "altitude": 3500, "ground_speed": 210, "track": 45, "vertical_rate": 1200,
            **overrides}
