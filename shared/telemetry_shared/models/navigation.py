from pydantic import BaseModel


class Airport(BaseModel):
    icao: str
    name: str
    latitude: float
    longitude: float
    elevation_ft: float | None = None


class Waypoint(BaseModel):
    ident: str
    latitude: float
    longitude: float
    type: str | None = None
    """For example "fix" or "VOR"."""


class Flight(BaseModel):
    flight_id: str
    callsign: str
    origin: str
    destination: str
    route: list[str] = []
    """Ordered waypoint idents from origin to destination."""
