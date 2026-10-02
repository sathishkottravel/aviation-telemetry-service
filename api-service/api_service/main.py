import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from api_service.api.auth import ApiTokenMiddleware, AuthenticatedGraphQLRouter, add_cors
from api_service.api.controllers import router as rest_router
from api_service.graphql.schema import schema
from api_service.services import adsb_service, keep_warm, telemetry_service
from telemetry_shared.config import get_settings
from telemetry_shared.database import mongo
from telemetry_shared.messaging.rabbitmq import RabbitMQ
from telemetry_shared.observability.tracing import setup_tracing, shutdown_tracing

logger = logging.getLogger(__name__)

logging.basicConfig(level=get_settings().log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# Before the app and the MongoDB client exist: instrumentation hooks into clients created afterwards.
tracing_enabled = setup_tracing("flight-telemetry-api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    mongo.connect()
    adsb_service.connect()

    rabbitmq = RabbitMQ(settings)
    try:
        await rabbitmq.connect()
        await rabbitmq.consume_live(telemetry_service.handle_live_message)
    except Exception:
        # aio-pika raises several unrelated types here (AMQPConnectionError, OSError, TimeoutError).
        logger.warning("RabbitMQ unavailable at startup; ingest and live updates are disabled", exc_info=True)
    app.state.rabbitmq = rabbitmq
    wake = asyncio.create_task(keep_warm.run(settings.wake_url_list)) if settings.wake_url_list else None

    yield

    if wake is not None:
        wake.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await wake
    await rabbitmq.close()
    await adsb_service.close()
    await mongo.close()
    shutdown_tracing()


# FastAPI >= 0.142 would otherwise add its own OTLP exporters (traces, metrics, logs) whenever
# OTEL_EXPORTER_OTLP_ENDPOINT is set, duplicating ours; tracing is configured in telemetry_shared.observability.
app = FastAPI(title="Aviation Telemetry API", lifespan=lifespan, telemetry={"auto_configure": False})
app.include_router(rest_router)
app.include_router(AuthenticatedGraphQLRouter(schema), prefix="/graphql")
app.add_middleware(ApiTokenMiddleware)
add_cors(app, get_settings().cors_origin_list)
if tracing_enabled:
    # Skip the per-message ASGI receive/send spans; the request span and our custom spans tell the story.
    FastAPIInstrumentor.instrument_app(app, excluded_urls="health", exclude_spans=["receive", "send"])
