"""The API's HTTP client for the producer: request paths and error mapping, against a mocked producer."""

import httpx
import pytest

from api_service.services import adsb_service
from api_service.services.adsb_service import (
    AlreadyTrackingError,
    InvalidArgumentError,
    NotTrackingError,
    ProducerUnavailableError,
)

STATUS = {"aircraft_id": "*", "running": True, "published_count": 0}


@pytest.fixture
def producer(monkeypatch):
    """Point the module's client at a MockTransport; returns (requests, set_response)."""
    requests, response = [], {"value": httpx.Response(200, json=STATUS)}

    def handler(request):
        requests.append(request)
        return response["value"]

    monkeypatch.setattr(adsb_service, "_http", httpx.AsyncClient(base_url="http://producer.test",
                                                                 transport=httpx.MockTransport(handler)))
    return requests, lambda r: response.update(value=r)


async def test_ids_are_url_encoded_including_wildcard(producer):
    requests, _ = producer
    status = await adsb_service.start_tracking(" * ")
    assert status.running is True
    assert requests[0].method == "POST"
    assert requests[0].url.raw_path == b"/ingestion/live/start/%2A"


@pytest.mark.parametrize("status_code,error", [
    (409, AlreadyTrackingError), (404, NotTrackingError), (503, ProducerUnavailableError),
])
async def test_producer_errors_map_to_typed_exceptions(producer, status_code, error):
    _, respond = producer
    respond(httpx.Response(status_code, json={"detail": "from producer"}))
    with pytest.raises(error, match="from producer"):
        await adsb_service.tracking_status("x")


async def test_unreachable_producer(monkeypatch):
    def refuse(request):
        raise httpx.ConnectError("refused", request=request)

    monkeypatch.setattr(adsb_service, "_http", httpx.AsyncClient(base_url="http://producer.test",
                                                                 transport=httpx.MockTransport(refuse)))
    with pytest.raises(ProducerUnavailableError, match="cannot reach"):
        await adsb_service.list_trackable_aircraft()


async def test_sends_producer_token_when_configured(set_settings, monkeypatch):
    set_settings(producer_token="p-secret")
    monkeypatch.setattr(adsb_service, "_http", None)
    adsb_service.connect()
    try:
        assert adsb_service._http.headers["Authorization"] == "Bearer p-secret"
    finally:
        await adsb_service.close()


async def test_area_arguments_become_query_params(producer):
    requests, _ = producer
    await adsb_service.start_tracking("*", latitude=51.5, radius_nm=50)
    assert dict(requests[0].url.params) == {"latitude": "51.5", "radius_nm": "50"}
    await adsb_service.tracking_status("*")
    assert not requests[1].url.params


async def test_validation_errors_become_invalid_argument(producer):
    _, respond = producer
    respond(httpx.Response(422, json={"detail": [{"loc": ["query", "latitude"], "msg": "less than or equal to 90"}]}))
    with pytest.raises(InvalidArgumentError, match="latitude: less than or equal to 90"):
        await adsb_service.start_tracking("x", latitude=91)
