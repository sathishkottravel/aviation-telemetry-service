import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from adsb_producer.api.controllers import router
from adsb_producer.services.tracking_service import AlreadyTrackingError, TrackingManager
from telemetry_shared.adsb.client import AdsbLolClient
from telemetry_shared.adsb.ingestion import AdsbAreaPoller
from telemetry_shared.config import get_settings
from telemetry_shared.messaging.rabbitmq import RabbitMQ
from telemetry_shared.observability.tracing import setup_tracing, shutdown_tracing

logger = logging.getLogger("adsb_producer")

logging.basicConfig(level=get_settings().log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# Before the ADSB.lol HTTP client exists: instrumentation hooks into clients created afterwards.
tracing_enabled = setup_tracing("flight-telemetry-producer")


async def connect_and_autostart(rabbitmq: RabbitMQ, tracking: TrackingManager, aircraft_ids: list[str]) -> None:
    await rabbitmq.connect_with_retry()
    logger.info("Connected to RabbitMQ")
    if not aircraft_ids:
        logger.info("ADSB_PRODUCER_AIRCRAFT is empty; use POST /ingestion/live/start/{aircraft_id} to track aircraft")
    for aircraft_id in aircraft_ids:
        with contextlib.suppress(AlreadyTrackingError):
            tracking.start(aircraft_id)
            logger.info("Tracking %s from ADSB_PRODUCER_AIRCRAFT", aircraft_id)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    client = AdsbLolClient(settings.adsb_base_url, settings.adsb_request_timeout, settings.adsb_user_agent)
    rabbitmq = RabbitMQ(settings)
    tracking = TrackingManager(AdsbAreaPoller(client, rabbitmq, settings), rabbitmq, settings)
    app.state.rabbitmq = rabbitmq
    app.state.tracking = tracking

    # One poller for all aircraft; it makes no requests while nothing is tracked.
    tracking.start_polling()
    # Connect in the background so the HTTP server is up (and /health answers) while RabbitMQ is still starting.
    startup = asyncio.create_task(connect_and_autostart(rabbitmq, tracking, settings.adsb_producer_aircraft_ids))

    yield

    startup.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await startup
    await tracking.stop_polling()
    await rabbitmq.close()
    await client.close()
    shutdown_tracing()


app = FastAPI(title="ADS-B Producer", lifespan=lifespan)
app.include_router(router)
if tracing_enabled:
    # Skip the per-message ASGI receive/send spans; the request span and our custom spans tell the story.
    FastAPIInstrumentor.instrument_app(app, excluded_urls="health", exclude_spans=["receive", "send"])
