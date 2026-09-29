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
