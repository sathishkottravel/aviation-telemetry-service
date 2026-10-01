import logging
import secrets

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from adsb_producer.services.tracking_service import (
    AlreadyTrackingError,
    IngestUnavailableError,
    NotTrackingError,
    TrackingManager,
)
from telemetry_shared.adsb.ingestion import Area
from telemetry_shared.config import get_settings
from telemetry_shared.models import AreaAircraftList, TrackingStatus

logger = logging.getLogger(__name__)

router = APIRouter()


# A security scheme rather than a plain header parameter: Swagger UI never sends a header parameter named
# Authorization, but shows an Authorize button for this.
bearer = HTTPBearer(auto_error=False, description="PRODUCER_TOKEN (paste the raw token, without 'Bearer')")


def require_producer_token(credentials: HTTPAuthorizationCredentials | None = Security(bearer)) -> None:
    """Checks 'Authorization: Bearer <PRODUCER_TOKEN>' when PRODUCER_TOKEN is set (open when empty)."""
    expected = get_settings().producer_token
    if not expected:
        return
    if credentials is None or not secrets.compare_digest(credentials.credentials, expected):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Invalid or missing producer token",
            headers={"WWW-Authenticate": "Bearer"},
        )


# /health stays open; the tracking routes need the token.
ingestion = APIRouter(prefix="/ingestion/live", dependencies=[Depends(require_producer_token)])


def _tracking(request: Request) -> TrackingManager:
    return request.app.state.tracking


def area_query(
    latitude: float | None = Query(None, ge=-90, le=90),
    longitude: float | None = Query(None, ge=-180, le=180),
    radius_nm: float | None = Query(None, gt=0, le=250, description="ADSB.lol allows up to 250 NM"),
) -> Area | None:
    """An area from the query string, missing values taken from ADSB_LATITUDE/LONGITUDE/RADIUS_NM.
    None when no value is given."""
    if latitude is None and longitude is None and radius_nm is None:
        return None
    settings = get_settings()
    return Area(
        settings.adsb_latitude if latitude is None else latitude,
        settings.adsb_longitude if longitude is None else longitude,
        settings.adsb_radius_nm if radius_nm is None else radius_nm,
    )


@router.get("/health")
async def health(request: Request) -> dict[str, str | bool]:
    return {"status": "ok", "rabbitmq": request.app.state.rabbitmq.is_connected}


@ingestion.get("/aircraft")
async def list_area_aircraft(request: Request, area: Area | None = Depends(area_query)) -> AreaAircraftList:
    """Aircraft that can be tracked, nearest first, in the given area (default: the polled area)."""
    try:
        return await _tracking(request).list_area_aircraft(area)
    except Exception as exc:
        # The polled area falls back to its last snapshot, so this means no snapshot yet, or another area failed.
        logger.warning("Cannot list area aircraft: %s", exc)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"ADSB.lol unavailable: {exc}") from exc


@ingestion.post("/start/{aircraft_id}", status_code=status.HTTP_202_ACCEPTED)
async def start_live_ingestion(
    aircraft_id: str, request: Request, area: Area | None = Depends(area_query)
) -> TrackingStatus:
    """Start tracking. With an area, the poller moves to it (it polls one area, shared by all tracked IDs)."""
    try:
        return _tracking(request).start(aircraft_id, area)
    except AlreadyTrackingError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Already tracking {exc}") from exc
    except IngestUnavailableError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "RabbitMQ is unavailable") from exc


@ingestion.post("/stop/{aircraft_id}")
async def stop_live_ingestion(aircraft_id: str, request: Request) -> TrackingStatus:
    try:
        return _tracking(request).stop(aircraft_id)
    except NotTrackingError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Not tracking {exc}") from exc


@ingestion.get("/status/{aircraft_id}")
async def live_ingestion_status(aircraft_id: str, request: Request) -> TrackingStatus:
    return _tracking(request).status(aircraft_id)


router.include_router(ingestion)
