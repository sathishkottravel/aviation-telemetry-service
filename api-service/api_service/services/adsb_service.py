"""Thin HTTP client for the ADS-B producer, which owns live tracking state."""

from urllib.parse import quote

import httpx
from pydantic import BaseModel

from telemetry_shared.config import get_settings
from telemetry_shared.models import AreaAircraftList, TrackingStatus

_http: httpx.AsyncClient | None = None


class ProducerUnavailableError(Exception):
    """The ADS-B producer could not be reached, or it could not serve the request (e.g. RabbitMQ down)."""


class AlreadyTrackingError(Exception):
    pass


class NotTrackingError(Exception):
    pass


def connect() -> None:
    global _http
    if _http is None:
        settings = get_settings()
        headers = {"Authorization": f"Bearer {settings.producer_token}"} if settings.producer_token else None
        _http = httpx.AsyncClient(base_url=settings.producer_url, headers=headers, timeout=10)


async def close() -> None:
    global _http
    if _http is not None:
        await _http.aclose()
        _http = None


async def list_trackable_aircraft() -> AreaAircraftList:
    return await _call("GET", "/ingestion/live/aircraft", AreaAircraftList)


async def start_tracking(aircraft_id: str) -> TrackingStatus:
    return await _call("POST", f"/ingestion/live/start/{_path_id(aircraft_id)}", TrackingStatus)


async def stop_tracking(aircraft_id: str) -> TrackingStatus:
    return await _call("POST", f"/ingestion/live/stop/{_path_id(aircraft_id)}", TrackingStatus)


async def tracking_status(aircraft_id: str) -> TrackingStatus:
    return await _call("GET", f"/ingestion/live/status/{_path_id(aircraft_id)}", TrackingStatus)


async def _call[M: BaseModel](method: str, path: str, model: type[M]) -> M:
    if _http is None:
        raise RuntimeError("Producer client is not initialised; call connect() first")
    try:
        response = await _http.request(method, path)
    except httpx.HTTPError as exc:
        raise ProducerUnavailableError(f"cannot reach {get_settings().producer_url}") from exc

    if response.is_success:
        return model.model_validate_json(response.content)
    detail = _detail(response)
    if response.status_code == httpx.codes.CONFLICT:
        raise AlreadyTrackingError(detail)
    if response.status_code == httpx.codes.NOT_FOUND:
        raise NotTrackingError(detail)
    raise ProducerUnavailableError(detail)


def _path_id(aircraft_id: str) -> str:
    return quote(aircraft_id.strip(), safe="")


def _detail(response: httpx.Response) -> str:
    try:
        return str(response.json().get("detail", response.text))
    except ValueError:
        return response.text or f"HTTP {response.status_code}"
