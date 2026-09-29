import logging

from fastapi import APIRouter, HTTPException, Request, status

from adsb_producer.services.tracking_service import (
    AlreadyTrackingError,
    IngestUnavailableError,
    NotTrackingError,
    TrackingManager,
)
from telemetry_shared.models import AreaAircraftList, TrackingStatus

logger = logging.getLogger(__name__)

router = APIRouter()


def _tracking(request: Request) -> TrackingManager:
    return request.app.state.tracking


@router.get("/health")
async def health(request: Request) -> dict[str, str | bool]:
    return {"status": "ok", "rabbitmq": request.app.state.rabbitmq.is_connected}


@router.get("/ingestion/live/aircraft")
async def list_area_aircraft(request: Request) -> AreaAircraftList:
    """Aircraft in the configured ADS-B area that can be tracked, nearest first."""
    try:
        return await _tracking(request).list_area_aircraft()
    except Exception as exc:
        # Only reached when ADSB.lol has never answered (no snapshot to fall back on).
        logger.warning("Cannot list area aircraft: %s", exc)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"ADSB.lol unavailable: {exc}") from exc


@router.post("/ingestion/live/start/{aircraft_id}", status_code=status.HTTP_202_ACCEPTED)
async def start_live_ingestion(aircraft_id: str, request: Request) -> TrackingStatus:
    try:
        return _tracking(request).start(aircraft_id)
    except AlreadyTrackingError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Already tracking {exc}") from exc
    except IngestUnavailableError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "RabbitMQ is unavailable") from exc


@router.post("/ingestion/live/stop/{aircraft_id}")
async def stop_live_ingestion(aircraft_id: str, request: Request) -> TrackingStatus:
    try:
        return _tracking(request).stop(aircraft_id)
    except NotTrackingError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Not tracking {exc}") from exc


@router.get("/ingestion/live/status/{aircraft_id}")
async def live_ingestion_status(aircraft_id: str, request: Request) -> TrackingStatus:
    return _tracking(request).status(aircraft_id)
