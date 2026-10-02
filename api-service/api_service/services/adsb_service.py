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


class InvalidArgumentError(Exception):
    """The producer rejected an argument (e.g. latitude out of range)."""


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


async def health() -> dict:
    """The producer's /health body (open route, no token needed)."""
    if _http is None:
        raise RuntimeError("Producer client is not initialised; call connect() first")
    response = await _http.get("/health")
    response.raise_for_status()
    return response.json()


async def list_trackable_aircraft(
    latitude: float | None = None, longitude: float | None = None, radius_nm: float | None = None
) -> AreaAircraftList:
    """Aircraft in the given area; values left out use the producer's defaults (no values: the polled area)."""
    params = _area_params(latitude, longitude, radius_nm)
    return await _call("GET", "/ingestion/live/aircraft", AreaAircraftList, params)


async def start_tracking(
    aircraft_id: str, latitude: float | None = None, longitude: float | None = None, radius_nm: float | None = None
) -> TrackingStatus:
    """Start tracking; with any area value, the producer moves its single polled area there."""
    params = _area_params(latitude, longitude, radius_nm)
    return await _call("POST", f"/ingestion/live/start/{_path_id(aircraft_id)}", TrackingStatus, params)


async def stop_tracking(aircraft_id: str) -> TrackingStatus:
    return await _call("POST", f"/ingestion/live/stop/{_path_id(aircraft_id)}", TrackingStatus)


async def tracking_status(aircraft_id: str) -> TrackingStatus:
    return await _call("GET", f"/ingestion/live/status/{_path_id(aircraft_id)}", TrackingStatus)


async def _call[M: BaseModel](method: str, path: str, model: type[M], params: dict | None = None) -> M:
    if _http is None:
        raise RuntimeError("Producer client is not initialised; call connect() first")
    try:
        response = await _http.request(method, path, params=params)
    except httpx.HTTPError as exc:
        raise ProducerUnavailableError(f"cannot reach {get_settings().producer_url}") from exc

    if response.is_success:
        return model.model_validate_json(response.content)
    detail = _detail(response)
    if response.status_code == httpx.codes.CONFLICT:
        raise AlreadyTrackingError(detail)
    if response.status_code == httpx.codes.NOT_FOUND:
        raise NotTrackingError(detail)
    if response.status_code == httpx.codes.UNPROCESSABLE_ENTITY:
        raise InvalidArgumentError(detail)
    raise ProducerUnavailableError(detail)


def _area_params(latitude: float | None, longitude: float | None, radius_nm: float | None) -> dict:
    values = {"latitude": latitude, "longitude": longitude, "radius_nm": radius_nm}
    return {k: v for k, v in values.items() if v is not None}


def _path_id(aircraft_id: str) -> str:
    return quote(aircraft_id.strip(), safe="")


def _detail(response: httpx.Response) -> str:
    try:
        detail = response.json().get("detail", response.text)
        if isinstance(detail, list):  # FastAPI validation errors
            return "; ".join(f"{e.get('loc', ['?'])[-1]}: {e.get('msg')}" for e in detail)
        return str(detail)
    except ValueError:
        return response.text or f"HTTP {response.status_code}"
