import asyncio

import httpx
import pytest

from telemetry_shared.adsb import client as client_module
from telemetry_shared.adsb.client import AdsbLolClient, AdsbRateLimitedError

BODY = {"now": 1790681094501, "ac": [{"hex": "4ab563"}]}


def make_client(responses, calls):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        status = responses.pop(0)
        return httpx.Response(status, json=BODY if status == 200 else {})

    client = AdsbLolClient("https://adsb.test", timeout_s=5, user_agent="tests/1.0 (+https://example.test)")
    client._http = httpx.AsyncClient(
        base_url="https://adsb.test", headers={"User-Agent": "tests/1.0"}, transport=httpx.MockTransport(handler)
    )
    return client


async def test_fetch_area_parses_snapshot():
    calls = []
    snapshot = await make_client([200], calls).fetch_area(59.3, 18.0, 100)
    assert calls[0].url.path == "/v2/point/59.3/18.0/100"
    assert snapshot.aircraft == [{"hex": "4ab563"}]
    assert snapshot.now.year == 2026


async def test_rate_limit_pauses_requests_then_resumes(monkeypatch):
    monkeypatch.setattr(client_module, "MIN_BACKOFF_S", 0.05)
    calls = []
    client = make_client([429, 200], calls)

    with pytest.raises(AdsbRateLimitedError):
        await client.fetch_area(1, 2, 3)
    with pytest.raises(AdsbRateLimitedError):  # paused: no request is made
        await client.fetch_area(1, 2, 3)
    assert len(calls) == 1

    await asyncio.sleep(0.06)
    await client.fetch_area(1, 2, 3)
    assert len(calls) == 2 and client._backoff_s == 0


def test_user_agent_is_sent():
    client = AdsbLolClient("https://adsb.test", timeout_s=5, user_agent="aviation-test/1.0 (+https://x.test)")
    assert client._http.headers["User-Agent"] == "aviation-test/1.0 (+https://x.test)"
