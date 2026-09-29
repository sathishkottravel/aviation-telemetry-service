import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from strawberry.fastapi import GraphQLRouter

from api_service.api.controllers import router as rest_router
from api_service.graphql.schema import schema
from api_service.services import adsb_service, telemetry_service
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

    yield

    await rabbitmq.close()
    await adsb_service.close()
    await mongo.close()
    shutdown_tracing()


app = FastAPI(title="Aviation Telemetry API", lifespan=lifespan)
app.include_router(rest_router)
app.include_router(GraphQLRouter(schema), prefix="/graphql")
if tracing_enabled:
    # Skip the per-message ASGI receive/send spans; the request span and our custom spans tell the story.
    FastAPIInstrumentor.instrument_app(app, excluded_urls="health", exclude_spans=["receive", "send"])
