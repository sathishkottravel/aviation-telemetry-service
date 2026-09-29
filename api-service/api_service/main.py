import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from strawberry.fastapi import GraphQLRouter

from api_service.api.controllers import router as rest_router
from api_service.graphql.schema import schema
from api_service.services import adsb_service, telemetry_service
from telemetry_shared.config import get_settings
from telemetry_shared.database import mongo
from telemetry_shared.messaging.rabbitmq import RabbitMQ

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

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


app = FastAPI(title="Aviation Telemetry API", lifespan=lifespan)
app.include_router(rest_router)
app.include_router(GraphQLRouter(schema), prefix="/graphql")
