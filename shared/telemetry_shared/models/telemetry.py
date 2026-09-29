from datetime import datetime

from pydantic import BaseModel, Field


class Telemetry(BaseModel):
    """Normalized aircraft state. The same shape is used for ingest, history and live updates."""

    flight_id: str
    timestamp: datetime
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    altitude: float = Field(description="Altitude in feet")
    ground_speed: float = Field(ge=0, description="Ground speed in knots")
    track: float = Field(ge=0, lt=360, description="Track over ground in degrees true")
    vertical_rate: float = Field(description="Vertical rate in feet per minute")
    callsign: str | None = None
