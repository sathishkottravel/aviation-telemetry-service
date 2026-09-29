import httpx

from telemetry_shared.config import get_settings
from telemetry_shared.models import AreaAircraftList

_http: httpx.AsyncClient | None = None


class ProducerUnavailableError(Exception):
    """The ADS-B producer could not be reached or could not list aircraft."""


def connect() -> None:
    global _http
    if _http is None:
        _http = httpx.AsyncClient(base_url=get_settings().producer_url, timeout=10)


async def close() -> None:
    global _http
    if _http is not None:
        await _http.aclose()
        _http = None


async def list_trackable_aircraft() -> AreaAircraftList:
    if _http is None:
        raise RuntimeError("Producer client is not initialised; call connect() first")
    try:
        response = await _http.get("/ingestion/live/aircraft")
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = exc.response.json().get("detail", exc.response.text) if _is_json(exc.response) else exc.response.text
        raise ProducerUnavailableError(detail) from exc
    except httpx.HTTPError as exc:
        raise ProducerUnavailableError(f"cannot reach {get_settings().producer_url}") from exc
    return AreaAircraftList.model_validate_json(response.content)


def _is_json(response: httpx.Response) -> bool:
    return response.headers.get("content-type", "").startswith("application/json")
