import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pymongo.errors import PyMongoError

from api_service.services import health_service, telemetry_service
from api_service.services.telemetry_service import IngestUnavailableError
from telemetry_shared.config import get_settings
from telemetry_shared.models import Telemetry

router = APIRouter()


def require_admin_token(x_admin_token: Annotated[str | None, Header()] = None) -> None:
    expected = get_settings().admin_token
    if not expected:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin endpoints are disabled; set ADMIN_TOKEN to enable them")
    if x_admin_token is None or not secrets.compare_digest(x_admin_token, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing or invalid X-Admin-Token")


@router.get("/health")
async def health(request: Request) -> dict[str, str | bool]:
    return {"status": "ok", "rabbitmq": request.app.state.rabbitmq.is_connected}


@router.get("/health/all")
async def health_all(request: Request) -> dict:
    """api, producer and worker in one call (status is "ok" or "degraded"; always HTTP 200)."""
    return await health_service.check_all(request.app.state.rabbitmq.is_connected)


@router.post("/api/telemetry", status_code=status.HTTP_202_ACCEPTED)
async def ingest_telemetry(telemetry: Telemetry, request: Request) -> dict[str, str]:
    try:
        await telemetry_service.submit(telemetry, request.app.state.rabbitmq)
    except IngestUnavailableError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Telemetry ingest is unavailable") from exc
    return {"status": "accepted"}


@router.delete("/api/telemetry", dependencies=[Depends(require_admin_token)])
async def prune_telemetry(
    flight_id: str | None = None,
    before: Annotated[datetime | None, Query(description="Delete telemetry with a timestamp before this time")] = None,
    older_than_hours: Annotated[float | None, Query(gt=0, description="Delete telemetry older than this")] = None,
) -> dict[str, int | str | None]:
    """Prune telemetry on demand. At least one filter is required, so a bare call can't wipe the collection."""
    if before is not None and older_than_hours is not None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Use either before or older_than_hours, not both")
    if older_than_hours is not None:
        before = datetime.now(UTC) - timedelta(hours=older_than_hours)
    if flight_id is None and before is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "Give at least one of flight_id, before or older_than_hours"
        )
    try:
        deleted = await telemetry_service.prune(flight_id, before)
    except PyMongoError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "MongoDB is unavailable") from exc
    return {"deleted": deleted, "flight_id": flight_id, "before": before.isoformat() if before else None}
