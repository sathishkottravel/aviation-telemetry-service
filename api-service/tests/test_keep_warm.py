"""Keep-warm pings (WAKE_URLS) for hosts that sleep when idle."""

import httpx

from api_service.services import keep_warm


async def test_pings_every_url_and_tolerates_failures(caplog):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if request.url.host == "down.test":
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(503 if request.url.host == "sick.test" else 200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await keep_warm.ping_all(client, ["http://ok.test/health", "http://sick.test/health", "http://down.test/"])

    assert sorted(seen) == ["http://down.test/", "http://ok.test/health", "http://sick.test/health"]
    assert "sick.test/health returned HTTP 503" in caplog.text
    assert "down.test/ failed" in caplog.text


def test_wake_urls_setting_is_split_and_trimmed(set_settings):
    from telemetry_shared.config import get_settings

    set_settings(wake_urls=" https://a.test/health, ,https://b.test/health ")
    assert get_settings().wake_url_list == ["https://a.test/health", "https://b.test/health"]
