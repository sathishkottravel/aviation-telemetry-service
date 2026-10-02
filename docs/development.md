# Development

How to run, seed, try and test the services locally, and how data is stored. API details: [api.md](api.md);
deployment and tracing: [deployment.md](deployment.md).

## Message flow

```
POST /api/telemetry (api-service)      adsb-producer: ADSB.lol → filter → normalize
        │                                       │
        ▼                                       ▼
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

MongoDB collections and indexes:

| Collection  | Fields                                                                                   | Index                              |
| ----------- | ---------------------------------------------------------------------------------------- | ---------------------------------- |
| `airports`  | `icao`, `name`, `latitude`, `longitude`, `elevation_ft`                                  | `icao` (unique)                    |
| `waypoints` | `ident`, `latitude`, `longitude`, `type` (optional)                                      | `ident` (unique)                   |
| `flights`   | `flight_id`, `callsign`, `origin`, `destination`, `route` (ordered waypoint idents)      | `flight_id` (unique)               |
| `telemetry` | `flight_id`, `timestamp`, `latitude`, `longitude`, `altitude`, `ground_speed`, `track`, `vertical_rate`, `callsign` (optional) | `flight_id` + `timestamp`; TTL on `timestamp` |

The worker and the seed script create the indexes automatically.

## Telemetry retention

- **Automatic (TTL):** MongoDB deletes telemetry whose `timestamp` is older than `TELEMETRY_TTL_DAYS` (default `1`).
  - The TTL monitor runs about once a minute.
  - The worker applies the setting at startup, so restart it after changing the value.
  - `0` turns expiry off and drops the TTL index.
- **On demand:** `DELETE /api/telemetry` on the API. It needs the `X-Admin-Token` header to match `ADMIN_TOKEN`. When `ADMIN_TOKEN` is empty, the endpoint returns 403.

```sh
# older than 6 hours, for every flight
curl -X DELETE "http://localhost:8000/api/telemetry?older_than_hours=6" -H "X-Admin-Token: $ADMIN_TOKEN"
# one flight's whole history
curl -X DELETE "http://localhost:8000/api/telemetry?flight_id=4ab562" -H "X-Admin-Token: $ADMIN_TOKEN"
# one flight, before a point in time
curl -X DELETE "http://localhost:8000/api/telemetry?flight_id=4ab562&before=2026-09-29T12:00:00Z" -H "X-Admin-Token: $ADMIN_TOKEN"
# -> {"deleted": 42, "flight_id": "4ab562", "before": "..."}
```

At least one of `flight_id`, `before` or `older_than_hours` is required, so a bare call can't empty the collection. `before` and `older_than_hours` can't be combined.

| Status | Meaning |
| ------ | ------- |
| 401 | Missing or wrong `X-Admin-Token` |
| 403 | `ADMIN_TOKEN` isn't set, so pruning is disabled |
| 422 | No filter, or both `before` and `older_than_hours` |
| 503 | MongoDB unavailable |

## Project layout

```
api-service/api_service/
  main.py                    app + startup/shutdown
  api/controllers.py         REST: /health, POST /api/telemetry, DELETE /api/telemetry (admin prune)
  graphql/schema.py          GraphQL queries and subscription
  graphql/types.py           GraphQL types
  services/                  navigation, telemetry, live-broadcast and producer-client logic
telemetry-worker/telemetry_worker/
  main.py                    entry point
  workers/telemetry_consumer.py   RabbitMQ message handler
  services/processing_service.py  normalize → persist → publish live
adsb-producer/adsb_producer/
  main.py                    app + startup/shutdown (connects RabbitMQ, starts ADSB_PRODUCER_AIRCRAFT)
  api/controllers.py         REST: /health, /ingestion/live/aircraft, /ingestion/live/{start,stop,status}/{aircraft_id}
  services/tracking_service.py   set of requested aircraft + the single area poller (in memory)
shared/telemetry_shared/
  config.py  models/  database/  messaging/
  adsb/client.py             ADSB.lol area query, 429 back-off
  adsb/ingestion.py          single area poller: fetch once, filter locally, normalize, publish
  observability/tracing.py   optional OpenTelemetry setup, custom-span helpers, RabbitMQ context propagation
<service>/tests/             unit tests per package (shared/tests, api-service/tests, ...)
tests/integration/           integration tests (MongoDB + RabbitMQ)
tests/e2e/                   end-to-end GraphQL tests (running stack)
conftest.py                  test-wide settings isolation
```

## Run with Docker

```sh
docker compose up --build
```

- API and GraphiQL: http://localhost:8000/graphql
- ADS-B producer: http://localhost:8001/docs
- Jaeger (traces): http://localhost:16686
- RabbitMQ management UI: http://localhost:15672 (guest / guest)

To use MongoDB Atlas instead of the bundled container, set `COMPOSE_MONGODB_URI` in `.env`.

Each service can also be built on its own. The repository root must be the build context:

```sh
docker build -f api-service/Dockerfile -t aviation-api .
docker build -f telemetry-worker/Dockerfile -t aviation-worker .
docker build -f adsb-producer/Dockerfile -t aviation-producer .
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

# terminal 3 (optional, live ADS-B data)
uv run --package adsb-producer uvicorn adsb_producer.main:app --port 8001 --reload
```

The API starts even when RabbitMQ is unreachable. In that case ingest returns 503 and live updates are off; `/health` shows the RabbitMQ state. The worker keeps retrying until RabbitMQ is up.

## Seed demo data

Adds airports ESSP and ESSA, four waypoints between them, and flight `SAS123` flying ESSP → ESSA. It's safe to run more than once, because documents are replaced by their key.

```sh
uv run seed-db                          # uses MONGODB_URI from .env (local or Atlas)
docker compose run --rm worker seed-db  # against the compose MongoDB
```

The waypoint names and positions are made up for the demo; they are not real navigation data.

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

Subscribe to live updates (open this in GraphiQL, then send telemetry). Each message is a list, here with one element:

```graphql
subscription {
  liveTelemetry(flightId: "SAS123") {
    timestamp latitude longitude altitude groundSpeed track
  }
}
```

## Testing

Tests are split into three layers. By default only the unit tests run.

| Layer | Where | Needs | Run |
| ----- | ----- | ----- | --- |
| Unit | `<service>/tests/` | nothing; fakes stand in for MongoDB, RabbitMQ, ADSB.lol and the producer | `uv run pytest` |
| Integration | `tests/integration/` | MongoDB and RabbitMQ: `docker compose up -d mongo rabbitmq` | `uv run pytest -m integration` |
| End-to-end GraphQL | `tests/e2e/` | the full stack: `docker compose up` | `uv run pytest -m e2e` |

```sh
uv sync --all-packages         # installs pytest via the dev dependency group
uv run pytest                  # unit
uv run pytest -m integration   # integration
uv run pytest -m e2e           # end-to-end
uv run pytest -m ""            # everything
```

- **Unit tests** cover:
  - ADS-B normalization, filtering, `*` handling and de-duplication
  - the ADSB.lol client's 429 back-off
  - trace-context propagation
  - the live broadcaster
  - REST controllers, including admin-token checks
  - every GraphQL operation and error code, with services stubbed
  - the producer client's error mapping
  - worker ack/drop/reject behaviour
  - the producer's tracking state and REST endpoints
- **Integration tests** use the `aviation_test` database (dropped afterwards) and `test.*` exchanges and queues, so they never touch the stack's data or feed its worker. They cover:
  - indexes, including creating, changing and dropping the TTL index
  - seed idempotency
  - history ordering and time ranges, and pruning
  - message headers (source and `traceparent`)
  - live fan-out to multiple API instances
  - the worker pipeline from queue to MongoDB and live publish
- **End-to-end tests** cover:
  - navigation queries against the seeded stack database
  - REST ingest reaching `liveTelemetry` snapshots (one flight and `*`, and the immediate snapshot for a late subscriber) and `telemetryHistory` (including time windows)
  - the full tracking lifecycle with its error codes
  - `trackableAircraft`, skipped if ADSB.lol has no snapshot yet
  - Each test uses a unique `E2E-...` flight ID. Set `E2E_ADMIN_TOKEN` (matching the stack's `ADMIN_TOKEN`) to delete that telemetry afterwards; otherwise the TTL removes it. Set `E2E_API_TOKEN` when the API requires `API_TOKEN`. Other settings: `E2E_API_URL`, `E2E_MONGODB_URI`, `E2E_MONGODB_DB`.
- **Skipping:** integration and end-to-end tests skip themselves when their services aren't reachable.
