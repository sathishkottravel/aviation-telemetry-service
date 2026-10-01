from datetime import UTC, datetime, timedelta

import pytest

from telemetry_shared.adsb.client import AdsbRateLimitedError, AdsbSnapshot
from telemetry_shared.adsb.ingestion import (
    ALL_AIRCRAFT,
    AdsbAreaPoller,
    Area,
    index_by_id,
    list_area_aircraft,
    normalize,
)
from telemetry_shared.config import get_settings

NOW = datetime(2026, 9, 29, 12, tzinfo=UTC)


def aircraft(hex_id="4ab563", flight="SAS709  ", **fields):
    return {"hex": hex_id, "flight": flight, "lat": 59.5, "lon": 17.9, "alt_baro": 7000, "gs": 250,
            "track": 10, "baro_rate": 500, "seen_pos": 1.5, **fields}


class TestNormalize:
    def test_maps_adsb_fields_to_telemetry(self):
        t = normalize(aircraft(hex_id="4AB563"), NOW)
        assert t.flight_id == "4ab563"
        assert t.callsign == "SAS709"
        assert t.timestamp == NOW - timedelta(seconds=1.5)
        assert (t.latitude, t.longitude, t.altitude, t.ground_speed, t.track, t.vertical_rate) == (
            59.5, 17.9, 7000, 250, 10, 500)

    @pytest.mark.parametrize("missing", ["lat", "lon"])
    def test_skips_records_without_position(self, missing):
        raw = aircraft()
        del raw[missing]
        assert normalize(raw, NOW) is None

    def test_ground_altitude_becomes_zero(self):
        assert normalize(aircraft(alt_baro="ground"), NOW).altitude == 0

    def test_falls_back_to_geometric_altitude_and_skips_without_any(self):
        assert normalize(aircraft(alt_baro=None, alt_geom=1200), NOW).altitude == 1200
        assert normalize(aircraft(alt_baro=None), NOW) is None

    def test_callsign_used_as_flight_id_without_hex(self):
        assert normalize(aircraft(hex_id=None), NOW).flight_id == "SAS709"

    def test_track_wraps_and_missing_values_default(self):
        t = normalize(aircraft(track=None, true_heading=370, gs=None, baro_rate=None, geom_rate=-300), NOW)
        assert (t.track, t.ground_speed, t.vertical_rate) == (10, 0, -300)


def test_index_by_id_prefers_hex_over_callsign():
    a = aircraft(hex_id="aaaaaa", flight="BBBBBB")
    b = aircraft(hex_id="bbbbbb", flight="OTHER")
    index = index_by_id([a, b])
    assert index["bbbbbb"] is b  # hex of b wins over the callsign of a
    assert index["other"] is b


def test_list_area_aircraft_drops_unpositioned_and_sorts_by_distance():
    snapshot = AdsbSnapshot(NOW, [
        aircraft("far001", "FAR", dst=40.0),
        aircraft("near01", "NEAR", dst=5.0),
        {"hex": "nopos1", "flight": "NOPOS", "alt_baro": 1000, "dst": 1.0},
    ])
    assert [a.icao_hex for a in list_area_aircraft(snapshot)] == ["near01", "far001"]


class FakeClient:
    def __init__(self, snapshots):
        self.snapshots = list(snapshots)
        self.fetches = 0
        self.areas = []

    async def fetch_area(self, *args):
        self.fetches += 1
        self.areas.append(args)
        result = self.snapshots.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class FakeRabbit:
    is_connected = True

    def __init__(self):
        self.published = []

    async def publish_telemetry(self, telemetry, source):
        self.published.append((telemetry.flight_id, source))


def snapshot(at, *records):
    return AdsbSnapshot(at, list(records))


class TestAreaPoller:
    async def test_publishes_requested_aircraft_by_hex_or_callsign(self):
        rabbit = FakeRabbit()
        poller = AdsbAreaPoller(FakeClient([snapshot(NOW, aircraft(), aircraft("4ab567", "SAS4225"))]),
                                rabbit, get_settings())
        results = await poller.poll_once({"4ab563", "sas4225", "zzzzzz"})
        assert sorted(rabbit.published) == [("4ab563", "adsb"), ("4ab567", "adsb")]
        assert results["zzzzzz"].matched == 0 and not results["zzzzzz"].found
        assert results["sas4225"].telemetry.flight_id == "4ab567"

    async def test_skips_unchanged_positions(self):
        rabbit = FakeRabbit()
        client = FakeClient([snapshot(NOW, aircraft()), snapshot(NOW, aircraft()),
                             snapshot(NOW + timedelta(seconds=5), aircraft())])
        poller = AdsbAreaPoller(client, rabbit, get_settings())
        published = [(await poller.poll_once({"4ab563"}))["4ab563"].published for _ in range(3)]
        assert published == [1, 0, 1]

    async def test_wildcard_covers_area_and_overlapping_ids_publish_once(self):
        rabbit = FakeRabbit()
        area = snapshot(NOW, aircraft(), aircraft("4ab567", "SAS4225"), {"hex": "nopos1", "alt_baro": 1})
        poller = AdsbAreaPoller(FakeClient([area]), rabbit, get_settings())
        results = await poller.poll_once({ALL_AIRCRAFT, "sas709"})
        assert results[ALL_AIRCRAFT].matched == 2 and results[ALL_AIRCRAFT].telemetry is None
        assert sorted(rabbit.published) == [("4ab563", "adsb"), ("4ab567", "adsb")]  # 4ab563 not twice

    async def test_latest_snapshot_reuses_recent_fetch_and_falls_back_when_rate_limited(self, set_settings):
        set_settings(adsb_poll_interval=0)  # every call counts as stale
        client = FakeClient([snapshot(NOW, aircraft()), AdsbRateLimitedError("limited")])
        poller = AdsbAreaPoller(client, FakeRabbit(), get_settings())
        first = await poller.latest_snapshot()
        assert await poller.latest_snapshot() is first  # rate limited -> previous snapshot
        assert client.fetches == 2

    async def test_latest_snapshot_raises_without_any_snapshot(self):
        poller = AdsbAreaPoller(FakeClient([AdsbRateLimitedError("limited")]), FakeRabbit(), get_settings())
        with pytest.raises(AdsbRateLimitedError):
            await poller.latest_snapshot()

    async def test_area_defaults_to_settings_and_set_area_moves_polling(self):
        settings = get_settings()
        client = FakeClient([snapshot(NOW, aircraft()), snapshot(NOW, aircraft())])
        poller = AdsbAreaPoller(client, FakeRabbit(), settings)
        assert poller.area == Area(settings.adsb_latitude, settings.adsb_longitude, settings.adsb_radius_nm)
        await poller.poll_once({"4ab563"})
        poller.set_area(Area(51.5, -0.1, 50))
        await poller.poll_once({"4ab563"})
        assert client.areas == [(settings.adsb_latitude, settings.adsb_longitude, settings.adsb_radius_nm),
                                (51.5, -0.1, 50)]

    async def test_snapshot_of_another_area_is_fetched_but_not_kept(self):
        client = FakeClient([snapshot(NOW, aircraft()), snapshot(NOW, aircraft("4ab567", "SAS4225"))])
        poller = AdsbAreaPoller(client, FakeRabbit(), get_settings())
        polled = await poller.latest_snapshot()
        other = await poller.latest_snapshot(Area(51.5, -0.1, 50))
        assert other is not polled and client.areas[1] == (51.5, -0.1, 50)
        assert await poller.latest_snapshot() is polled  # the polled area's snapshot is untouched
