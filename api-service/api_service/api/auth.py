"""API token authentication (API_TOKEN). Empty token = authentication off (local development, tests).

- HTTP: GraphQL operations on /graphql and POST /api/telemetry need `Authorization: Bearer <API_TOKEN>`.
  The GraphiQL page (GET /graphql asking for HTML), /health and /docs stay open; DELETE /api/telemetry
  keeps its own X-Admin-Token check.
- WebSocket (GraphQL subscriptions): browsers cannot set headers on a WebSocket, so the token travels in the
  connection_init payload as {"authorization": "Bearer <API_TOKEN>"} (Apollo's connectionParams).
"""

import json
import secrets
from typing import Any

from starlette.types import ASGIApp, Receive, Scope, Send
from strawberry.exceptions import ConnectionRejectionError
from strawberry.fastapi import GraphQLRouter

from telemetry_shared.config import get_settings


def token_matches(authorization: str | None) -> bool:
    """True if authentication is off or the value is 'Bearer <API_TOKEN>'."""
    expected = get_settings().api_token
    if not expected:
        return True
    scheme, _, token = (authorization or "").partition(" ")
    return scheme.lower() == "bearer" and secrets.compare_digest(token.strip(), expected)


def _requires_token(method: str, path: str, accept: str) -> bool:
    if path.rstrip("/") == "/graphql":
        # The GraphiQL page itself is just HTML; the operations it sends are checked.
        return not (method == "GET" and "text/html" in accept)
    return path.rstrip("/") == "/api/telemetry" and method == "POST"


class ApiTokenMiddleware:
    """Pure ASGI middleware (WebSocket scopes pass through; see AuthenticatedGraphQLRouter)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
            if _requires_token(scope["method"], scope["path"], headers.get("accept", "")) and not token_matches(
                headers.get("authorization")
            ):
                await _unauthorized(send)
                return
        await self.app(scope, receive, send)


async def _unauthorized(send: Send) -> None:
    body = json.dumps({"detail": "Missing or invalid API token"}).encode()
    await send({
        "type": "http.response.start",
        "status": 401,
        "headers": [
            (b"content-type", b"application/json"),
            (b"www-authenticate", b"Bearer"),
            (b"content-length", str(len(body)).encode()),
        ],
    })
    await send({"type": "http.response.body", "body": body})


class AuthenticatedGraphQLRouter(GraphQLRouter):
    """Rejects subscription connections whose connection_init payload lacks the API token."""

    async def on_ws_connect(self, context: dict[str, Any]) -> None:
        params = context.get("connection_params") or {}
        authorization = params.get("authorization") or params.get("Authorization") if isinstance(params, dict) else None
        if not token_matches(authorization):
            raise ConnectionRejectionError({"reason": "Missing or invalid API token"})
        return None
