from datetime import datetime

from pydantic import BaseModel


class AreaAircraft(BaseModel):
    """An aircraft currently inside the configured ADS-B area that can be tracked."""

    icao_hex: str
    callsign: str | None = None
    latitude: float
    longitude: float
    altitude: float
    ground_speed: float
    track: float
    distance_nm: float | None = None
    """Distance from the area centre, as reported by ADSB.lol."""
    tracked: bool = False


class AreaAircraftList(BaseModel):
    fetched_at: datetime
    """ADSB.lol server time of the snapshot the list was built from."""
    latitude: float
    longitude: float
    radius_nm: float
    aircraft: list[AreaAircraft]


class TrackingStatus(BaseModel):
    aircraft_id: str
    """The ID tracking was started with: ICAO hex, callsign (lowercased), or '*' for every aircraft in the area."""
    icao_hex: str | None = None
    """Resolved ICAO hex (use it as flightId for telemetry). None until the aircraft has been found."""
    running: bool
    started_at: datetime | None = None
    last_poll_at: datetime | None = None
    in_area: bool | None = None
    """Whether the last poll found the aircraft (for '*': any aircraft) inside the configured ADS-B area."""
    aircraft_count: int | None = None
    """Aircraft covered on the last poll: 0 or 1, or the number in the area for '*'."""
    last_position_at: datetime | None = None
    published_count: int = 0
    last_error: str | None = None
