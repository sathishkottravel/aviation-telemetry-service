from fastapi import APIRouter, HTTPException, Request, status

from api_service.services import telemetry_service
from api_service.services.telemetry_service import IngestUnavailableError
from telemetry_shared.models import Telemetry

router = APIRouter()


@router.get("/health")
async def health(request: Request) -> dict[str, str | bool]:
    return {"status": "ok", "rabbitmq": request.app.state.rabbitmq.is_connected}


@router.post("/api/telemetry", status_code=status.HTTP_202_ACCEPTED)
async def ingest_telemetry(telemetry: Telemetry, request: Request) -> dict[str, str]:
    try:
        await telemetry_service.submit(telemetry, request.app.state.rabbitmq)
    except IngestUnavailableError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Telemetry ingest is unavailable") from exc
    return {"status": "accepted"}
