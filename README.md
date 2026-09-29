# aviation-telemetry-service

Backend for an aviation flight-tracking demo: live aircraft telemetry over GraphQL subscriptions, plus stored history for playback.

The repository contains two independent services and a shared package:

| Folder              | What it is                                                                                      |
| ------------------- | ----------------------------------------------------------------------------------------------- |
| `api-service/`      | FastAPI + Strawberry GraphQL: flight, airport, waypoint, route and history queries, the `liveTelemetry` subscription, and the telemetry ingest endpoint |
| `telemetry-worker/` | RabbitMQ consumer: normalizes telemetry, stores it in MongoDB and publishes live updates        |
| `shared/`           | Models, settings, MongoDB and RabbitMQ helpers used by both services                            |

## Architecture

```
POST /api/telemetry (api-service)
        │
        ▼
exchange telemetry.ingest (direct) ──► queue telemetry.ingest (durable)
                                                │
                                                ▼
                                telemetry-worker: normalize → [ML scoring, later] → MongoDB
                                                │
                                                ▼
exchange telemetry.live (fanout) ──► temporary queue per api-service instance
                                                │
                                                ▼
                         in-memory broadcaster ──► GraphQL liveTelemetry(flightId)
```

MongoDB collections: `airports`, `waypoints`, `flights`, `telemetry`.

## Project layout

```
api-service/api_service/
  main.py                    app + startup/shutdown
  api/controllers.py         REST: /health, POST /api/telemetry
  graphql/schema.py          GraphQL queries and subscription
  graphql/types.py           GraphQL types
  services/                  navigation, telemetry and live-broadcast logic
telemetry-worker/telemetry_worker/
  main.py                    entry point
  workers/telemetry_consumer.py   RabbitMQ message handler
  services/processing_service.py  normalize → persist → publish live
shared/telemetry_shared/
  config.py  models/  database/  messaging/
```

## Run with Docker

```sh
docker compose up --build
```

- API and GraphiQL: http://localhost:8000/graphql
- RabbitMQ management UI: http://localhost:15672 (guest / guest)

To use MongoDB Atlas instead of the bundled container, set `COMPOSE_MONGODB_URI` in `.env`.

Each service can also be built on its own. The repository root must be the build context:

```sh
docker build -f api-service/Dockerfile -t aviation-api .
docker build -f telemetry-worker/Dockerfile -t aviation-worker .
```

## Run locally with uv

Requires [uv](https://docs.astral.sh/uv/). uv installs Python 3.12 if it is missing.

```sh
cp .env.example .env
uv sync --all-packages

# terminal 1
uv run --package api-service uvicorn api_service.main:app --reload

# terminal 2
uv run --package telemetry-worker telemetry-worker
```

The API starts even when RabbitMQ is unreachable. In that case ingest returns 503 and live updates are off; `/health` shows the RabbitMQ state. The worker keeps retrying until RabbitMQ is up.

## Try it

Send telemetry:

```sh
curl -X POST http://localhost:8000/api/telemetry \
  -H "Content-Type: application/json" \
  -d '{"flight_id":"SAS123","timestamp":"2026-09-29T10:00:00Z","latitude":59.35,"longitude":17.94,"altitude":3500,"ground_speed":210,"track":45,"vertical_rate":1200}'
```

Query history:

```graphql
query {
  telemetryHistory(flightId: "SAS123") {
    timestamp latitude longitude altitude groundSpeed track verticalRate
  }
}
```

Subscribe to live updates (open this in GraphiQL, then send telemetry):

```graphql
subscription {
  liveTelemetry(flightId: "SAS123") {
    timestamp latitude longitude altitude groundSpeed track
  }
}
```

## Status

This is a skeleton. Still to come:
- seed data (airports, waypoints, the ESSP → ESSA flight)
- real telemetry normalization
- optional ML anomaly scoring
