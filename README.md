# aviation-telemetry-service

Backend for an aviation flight-tracking demo: live aircraft telemetry over GraphQL subscriptions, plus stored history for playback.

The repository contains three independent services and a shared package:

| Folder              | What it is                                                                                      |
| ------------------- | ----------------------------------------------------------------------------------------------- |
| `api-service/`      | FastAPI + Strawberry GraphQL: flight, airport, waypoint, route and history queries, the `liveTelemetry` subscription, and the telemetry ingest endpoint |
| `telemetry-worker/` | RabbitMQ consumer: normalizes telemetry, stores it in MongoDB and publishes live updates        |
| `adsb-producer/`    | FastAPI service that polls [ADSB.lol](https://api.adsb.lol) for tracked aircraft and publishes their telemetry; start and stop tracking over HTTP |
| `shared/`           | Models, settings, MongoDB and RabbitMQ helpers, and the ADS-B client and normalization used by the services |

## Architecture

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

### Telemetry retention

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

## API reference

### GraphQL: api-service, `http://localhost:8000/graphql`

GraphiQL is served at the same URL. Subscriptions use WebSocket on the same path and support both the `graphql-transport-ws` and `graphql-ws` protocols (Apollo Client works with either).

| Operation | Kind | Purpose |
| --------- | ---- | ------- |
| `flight(flightId)` | query | Flight metadata and planned route (waypoint idents) |
| `route(flightId)` | query | The flight's waypoints, in flight order |
| `airports` | query | All airports |
| `waypoints` | query | All waypoints |
| `telemetryHistory(flightId, start, end)` | query | Stored positions, oldest first; `start`/`end` optional |
| `trackableAircraft` | query | Aircraft in the producer's ADS-B area, nearest first |
| `trackingStatus(aircraftId)` | query | Live tracking state for an ICAO hex, a callsign, or `*` |
| `startTracking(aircraftId)` | mutation | Start live ADS-B tracking (ICAO hex, callsign or `*`) |
| `stopTracking(aircraftId)` | mutation | Stop live ADS-B tracking |
| `liveTelemetry(flightId)` | subscription | List of the latest position of every matching flight (`*` = all), sent on subscribe and when positions change |

Every operation, ready to paste into GraphiQL. Each has a name, so you can pick which one to run:

```graphql
query Flight {
  flight(flightId: "SAS123") { flightId callsign origin destination route }
}

query Route {
  route(flightId: "SAS123") { ident latitude longitude type }
}

query Airports {
  airports { icao name latitude longitude elevationFt }
}

query Waypoints {
  waypoints { ident latitude longitude type }
}

query History {
  telemetryHistory(flightId: "SAS123") {
    flightId callsign timestamp latitude longitude altitude groundSpeed track verticalRate
  }
}

query HistoryWindow {
  telemetryHistory(flightId: "SAS123", start: "2026-09-29T10:00:00Z", end: "2026-09-29T10:30:00Z") {
    timestamp latitude longitude altitude
  }
}

query TrackableAircraft {
  trackableAircraft {
    fetchedAt latitude longitude radiusNm
    aircraft { icaoHex callsign latitude longitude altitude groundSpeed track distanceNm tracked }
  }
}

query TrackingStatus {
  trackingStatus(aircraftId: "SAS87C") {
    aircraftId icaoHex running startedAt lastPollAt inArea aircraftCount lastPositionAt publishedCount lastError
  }
}

mutation StartTracking {
  startTracking(aircraftId: "SAS87C") { aircraftId icaoHex running startedAt }
}

mutation StopTracking {
  stopTracking(aircraftId: "SAS87C") { aircraftId icaoHex running publishedCount }
}

subscription LiveFlight {
  liveTelemetry(flightId: "4ab562") { flightId callsign timestamp latitude longitude altitude groundSpeed track verticalRate }
}

subscription LiveAll {
  liveTelemetry(flightId: "*") { flightId callsign latitude longitude altitude track }
}
```

- **`liveTelemetry` sends lists.** Each message is a snapshot: a list of the latest position of every matching flight, sorted by `flightId`. A single-flight subscription gets a list with 0 or 1 element.
  - **On subscribe:** the current snapshot is sent right away, so a map can draw every aircraft without waiting for the next update.
  - **When positions change:** a new snapshot is sent after a 0.5 s settle window, so one ADS-B poll becomes one message, and at most once per second.
  - **Stale flights:** a flight with no new position for 5 minutes drops out of the list.
  - **Scope:** the snapshot is kept in each API process's memory, so it's empty right after the API restarts. Use `telemetryHistory` for anything older.

  Render straight from each message, for example with Apollo Client:

  ```ts
  const { data } = useSubscription(gql`
    subscription { liveTelemetry(flightId: "*") { flightId callsign latitude longitude track } }
  `);
  const aircraft = data?.liveTelemetry ?? [];   // replace the previous markers with this list
  ```

- **IDs:** `liveTelemetry` and `telemetryHistory` take the flight ID. For ADS-B data that's the lowercase ICAO hex (`icaoHex`), even when tracking was started by callsign.
- **Errors:** tracking operations return errors with `extensions.code` set to `ALREADY_TRACKING`, `NOT_TRACKING` or `PRODUCER_UNAVAILABLE`.

### Authentication

Set `API_TOKEN` to require a token (production does); leave it empty to keep the API open (local development, the
default compose setup, tests).

| Endpoint | Requires |
| -------- | -------- |
| GraphQL over HTTP (`POST /graphql`, `GET /graphql?query=...`) | `Authorization: Bearer <API_TOKEN>` |
| GraphQL subscriptions (WebSocket `/graphql`) | `{"authorization": "Bearer <API_TOKEN>"}` in the `connection_init` payload (browsers can't set WebSocket headers); otherwise the socket closes with 4403 |
| `POST /api/telemetry` | `Authorization: Bearer <API_TOKEN>` |
| `DELETE /api/telemetry` | `X-Admin-Token: <ADMIN_TOKEN>` (separate, see [Telemetry retention](#telemetry-retention)) |
| `/health`, `/docs`, `/openapi.json`, the GraphiQL page | nothing |

Missing or wrong tokens get `401` with `WWW-Authenticate: Bearer`.

```sh
curl -X POST https://api.sathishkottravel.com/graphql \
  -H "Authorization: Bearer $API_TOKEN" -H "Content-Type: application/json" \
  -d '{"query":"{ airports { icao name } }"}'
```

In **GraphiQL**, open the page without a token, then add `{"Authorization": "Bearer <API_TOKEN>"}` in the **Headers**
tab before running operations.

With **Apollo Client**, send the token on both links:

```ts
const httpLink = new HttpLink({
  uri: "https://api.sathishkottravel.com/graphql",
  headers: { Authorization: `Bearer ${API_TOKEN}` },
});
const wsLink = new GraphQLWsLink(createClient({
  url: "wss://api.sathishkottravel.com/graphql",
  connectionParams: { authorization: `Bearer ${API_TOKEN}` },
}));
```

A token shipped in a public browser app is visible to its users; it keeps out casual traffic, not a determined client.

### REST endpoints

**api-service** (`http://localhost:8000`, OpenAPI docs at `/docs`)

| Method | Path | Purpose | Responses |
| ------ | ---- | ------- | --------- |
| `GET` | `/health` | Liveness, and whether RabbitMQ is connected | 200 |
| `POST` | `/api/telemetry` | Ingest one telemetry record; it is published to RabbitMQ, not written directly | 202, 422 invalid, 503 RabbitMQ down |
| `DELETE` | `/api/telemetry?flight_id=&before=&older_than_hours=` | Prune telemetry; needs `X-Admin-Token` | 200 `{"deleted": n}`, 401, 403 disabled, 422, 503 |
| `POST` / `GET` / WebSocket | `/graphql` | GraphQL (see above) and GraphiQL | 200 |

**adsb-producer** (`http://localhost:8001`, OpenAPI docs at `/docs`). When `PRODUCER_TOKEN` is set (Render), the
`/ingestion/live/*` routes need `Authorization: Bearer <PRODUCER_TOKEN>` (401 otherwise); the api sends it.

| Method | Path | Purpose | Responses |
| ------ | ---- | ------- | --------- |
| `GET` | `/health` | Liveness, and whether RabbitMQ is connected | 200 |
| `GET` | `/ingestion/live/aircraft` | Aircraft in the configured area, nearest first, with a `tracked` flag | 200, 503 no snapshot yet |
| `POST` | `/ingestion/live/start/{aircraft_id}` | Start tracking (ICAO hex, callsign or `*`) | 202, 409 already tracked, 503 RabbitMQ down |
| `POST` | `/ingestion/live/stop/{aircraft_id}` | Stop tracking | 200, 404 not tracked |
| `GET` | `/ingestion/live/status/{aircraft_id}` | Tracking state | 200 (`running: false` when unknown) |

**telemetry-worker** only consumes the `telemetry.ingest` RabbitMQ queue. When `PORT` is set (Render web service), it
also answers any `GET` on that port with `200 {"status": "ok", "rabbitmq": <connected>}`.

Example requests:

```sh
curl http://localhost:8000/health
curl -X POST http://localhost:8000/api/telemetry -H "Content-Type: application/json" \
  -d '{"flight_id":"SAS123","timestamp":"2026-09-29T10:00:00Z","latitude":59.35,"longitude":17.94,"altitude":3500,"ground_speed":210,"track":45,"vertical_rate":1200}'
curl -X DELETE "http://localhost:8000/api/telemetry?older_than_hours=6" -H "X-Admin-Token: $ADMIN_TOKEN"

curl http://localhost:8001/ingestion/live/aircraft
curl -X POST http://localhost:8001/ingestion/live/start/4ab562
curl http://localhost:8001/ingestion/live/status/4ab562
curl -X POST http://localhost:8001/ingestion/live/stop/4ab562
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

## Live ADS-B ingestion

The producer queries ADSB.lol for every aircraft within `ADSB_RADIUS_NM` of `ADSB_LATITUDE`/`ADSB_LONGITUDE` and picks out the tracked ones. It uses each aircraft's ICAO hex code as `flight_id` and keeps the callsign in `callsign`. Records without a position are skipped. Telemetry goes through the same RabbitMQ pipeline as `POST /api/telemetry`; the producer never writes to MongoDB.

List the aircraft you can track, nearest first. The list reuses the poller's latest snapshot, so it doesn't add ADSB.lol requests:

```sh
curl http://localhost:8001/ingestion/live/aircraft   # fetched_at, radius_nm, aircraft[{icao_hex, callsign, distance_nm, tracked, ...}]
```

Or from GraphQL (the API calls the producer at `PRODUCER_URL`):

```graphql
query TrackableAircraft {
  trackableAircraft {
    fetchedAt
    radiusNm
    aircraft { icaoHex callsign distanceNm altitude groundSpeed tracked }
  }
}
```

### Track from GraphQL

The whole flow works from the GraphQL playground (http://localhost:8000/graphql). The API forwards tracking calls to the producer, which owns the tracking state. Run the operations in order:

```graphql
query Area {
  trackableAircraft { fetchedAt aircraft { icaoHex callsign distanceNm altitude tracked } }
}

mutation Start {
  startTracking(aircraftId: "SAS87C") { aircraftId icaoHex running }
}

query Status {
  trackingStatus(aircraftId: "SAS87C") { icaoHex running inArea lastPositionAt publishedCount lastError }
}

subscription Live {
  liveTelemetry(flightId: "4ab562") { callsign timestamp latitude longitude altitude groundSpeed track }
}

mutation Stop {
  stopTracking(aircraftId: "SAS87C") { icaoHex running publishedCount }
}
```

`aircraftId` can be an ICAO hex code or a callsign. `liveTelemetry` and `telemetryHistory` always need the ICAO hex. That's `icaoHex` in `trackableAircraft`, and in `trackingStatus` once the aircraft has been found; a hex ID is resolved immediately, a callsign after the first poll.

**Track every aircraft in the area with `*`.** `startTracking(aircraftId: "*")`, `trackingStatus(aircraftId: "*")` and `stopTracking(aircraftId: "*")` work like any other ID; the REST paths accept `/*` as well.
- `trackingStatus("*")` reports `aircraftCount`, the number of aircraft in the area on the last poll, and the total `publishedCount`.
- `*` can be combined with specific IDs. Stopping `*` leaves specific aircraft tracked, and an aircraft covered by both is still published only once per new position.
- Subscribe with `liveTelemetry(flightId: "*")` to receive every flight on one subscription. Each message is the full list of latest positions, one message per poll.

```graphql
mutation StartAll { startTracking(aircraftId: "*") { aircraftId running } }
subscription LiveAll { liveTelemetry(flightId: "*") { flightId callsign latitude longitude altitude track } }
query StatusAll { trackingStatus(aircraftId: "*") { running aircraftCount publishedCount lastError } }
mutation StopAll { stopTracking(aircraftId: "*") { running publishedCount } }
```

Tracking the whole area still costs one ADSB.lol request per interval, but it writes every aircraft's positions to MongoDB. Around Stockholm that was about 20 aircraft per poll, roughly 170,000 documents per day at a 10 s interval.

Errors carry `extensions.code`:

| Code | When |
| ---- | ---- |
| `ALREADY_TRACKING` | `startTracking` for an aircraft that is already tracked |
| `NOT_TRACKING` | `stopTracking` for an aircraft that isn't tracked |
| `PRODUCER_UNAVAILABLE` | The producer can't be reached, or it can't publish because RabbitMQ is down |

### Track over REST (producer)

Track an aircraft by ICAO hex code or callsign (case-insensitive):

```sh
curl -X POST http://localhost:8001/ingestion/live/start/4ab563   # 202; 409 if already tracked; 503 if RabbitMQ is down
curl http://localhost:8001/ingestion/live/status/4ab563          # icao_hex, running, in_area, published_count, last_error, ...
curl -X POST http://localhost:8001/ingestion/live/stop/4ab563    # 200; 404 if not tracked
```

Then query `telemetryHistory(flightId: "4ab563")` or subscribe to `liveTelemetry(flightId: "4ab563")` on the API. Always use the lowercase ICAO hex (`icaoHex`) as `flightId`, even if you started tracking by callsign.

Aircraft listed in `ADSB_PRODUCER_AIRCRAFT` (comma-separated) are tracked from startup. Tracking lives in memory, so a restart forgets aircraft started over HTTP.

| Setting | Default | Meaning |
| ------- | ------- | ------- |
| `ADSB_POLL_INTERVAL` | `5` | Seconds between polls |
| `ADSB_LATITUDE`, `ADSB_LONGITUDE` | `59.3`, `18.0` | Centre of the polled area |
| `ADSB_RADIUS_NM` | `100` | Radius of the polled area in nautical miles |
| `ADSB_PRODUCER_AIRCRAFT` | empty | Aircraft tracked from startup (`*` = whole area) |
| `ADSB_USER_AGENT` | project name + repo URL | ADSB.lol rejects generic User-Agents with 403 |
| `PRODUCER_URL` | `http://localhost:8001` | Where the API reaches the producer (`trackableAircraft`, tracking mutations) |

- **One request per interval.** A single area poller fetches the area once per interval and picks out every requested aircraft locally, however many are tracked. Start and stop only change the set of requested IDs. Nothing is requested while the set is empty.
- **Rate limits.** ADSB.lol rate-limits roughly this often and answers 429. The producer then pauses requests for all trackers (10 s, doubling up to 120 s) and resumes on its own. Raising `ADSB_POLL_INTERVAL` to 10 or more avoids most 429s.

## Tracing (OpenTelemetry + Jaeger)

All three services export traces over OTLP/gRPC. `docker compose up` includes Jaeger v2 all-in-one; open http://localhost:16686. Each process has its own service name: `flight-telemetry-api`, `flight-telemetry-worker` and `flight-telemetry-producer`.

The trace context travels in the RabbitMQ message headers (W3C `traceparent`), so one telemetry record gives one trace across the services:

```
api       POST /api/telemetry                  (FastAPI, automatic)
api         telemetry.submit                   telemetry.source=rest
api           telemetry.ingest publish         producer span, injects traceparent
worker          telemetry.ingest process       consumer span, parent from the message headers
worker            telemetry.normalize
worker            telemetry.persist
worker              aviation.insert            (pymongo, automatic)
worker            telemetry.live publish
api                 graphql.live_update        graphql.subscribers = live subscribers reached
```

ADS-B data gives the same chain under a producer `adsb.poll` span, which also contains the ADSB.lol `GET` (httpx, automatic). There's one publish branch per aircraft with a new position. GraphQL requests get Strawberry spans for parsing, validation and each resolver. The API → producer calls behind `trackableAircraft` and the tracking mutations continue into the producer's spans.

- **Attributes:** `flight.id`, `aircraft.id` (the ICAO hex for ADS-B), `aircraft.callsign`, `telemetry.timestamp`, `telemetry.source` (`rest`/`adsb`), `messaging.destination`, `messaging.operation`, plus `adsb.*` on polls.
- **Finding traces:** in Jaeger, search by service and tag, e.g. `flight.id=4ab562`.
- **Never recorded:**
  - connection strings, credentials and request headers, so `X-Admin-Token` stays out
  - MongoDB query documents; pymongo spans carry only the command name

| Setting | Default | Meaning |
| ------- | ------- | ------- |
| `OTEL_ENABLED` | `false` (compose: `true`) | Turns tracing on. When off, the tracing code is a no-op |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://localhost:4317` (compose: `http://jaeger:4318`) | OTLP endpoint (Jaeger, or a hosted backend) |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | `grpc` (compose: `http/protobuf`) | `grpc`, or `http/protobuf` for HTTP-only gateways such as Grafana Cloud (`/v1/traces` is appended to the endpoint) |
| `OTEL_EXPORTER_OTLP_HEADERS` | empty | Auth headers for hosted backends, URL-encoded (`%20` for a space) |
| `OTEL_SERVICE_NAME` | per service | Overrides the service name |
| `COMPOSE_OTEL_ENABLED`, `COMPOSE_OTEL_EXPORTER_OTLP_ENDPOINT`, `COMPOSE_OTEL_EXPORTER_OTLP_PROTOCOL`, `COMPOSE_OTEL_EXPORTER_OTLP_HEADERS`, `COMPOSE_OTEL_TRACES_EXPORTER` | `true`, bundled Jaeger over HTTP (`http://jaeger:4318`), `http/protobuf`, empty, `otlp` | The same settings for the compose containers |

- **If Jaeger is unreachable,** the services keep working; spans are exported in the background and only exporter warnings are logged.
- **Local `uv run` tracing:** run `docker compose up -d jaeger`, then set `OTEL_ENABLED=true` in `.env`.

### Send traces to Grafana Cloud

Grafana Cloud's OTLP gateway accepts **HTTP only**. With the default gRPC protocol, exports fail with `StatusCode.UNAVAILABLE ... missing selected ALPN property`.

1. In the Grafana Cloud portal, open the **OpenTelemetry** tile. Copy the OTLP endpoint and the instance ID, and create a token.
2. Build the credential: `printf '%s' '<instance-id>:<token>' | base64`.
3. Set these in `.env` (the `COMPOSE_` versions apply to the containers; drop the prefix for `uv run`):

   ```
   COMPOSE_OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
   COMPOSE_OTEL_EXPORTER_OTLP_ENDPOINT=https://otlp-gateway-prod-<region>.grafana.net/otlp
   COMPOSE_OTEL_EXPORTER_OTLP_HEADERS=Authorization=Basic%20<base64 from step 2>
   ```

   The headers value must include `Authorization=Basic%20`; the base64 on its own isn't sent as a header.
4. Run `docker compose up`. The traces appear in Grafana under **Explore → Tempo**, where you can search by `service.name` or a tag such as `flight.id`.

The local Jaeger stays the default; unset the three variables to go back to it.

## Production deployment (Oracle VM)

The backend runs on an Oracle Always Free VM (x86-64 AMD shape). Every push to `main` runs
[`.github/workflows/deploy.yml`](.github/workflows/deploy.yml):
1. unit tests
2. linux/amd64 images pushed to GHCR, tagged with the commit SHA
3. deploy over SSH, with healthchecks, automatic rollback and a smoke test. **This step is currently off** (repository
   variable `DEPLOY_ENABLED`). Deploys are done by hand on the VM with `./deploy/deploy.sh pull` (CI images) or `build` (build on the VM); see
   [deploy/README.md → Manual deploy](deploy/README.md#manual-deploy).

```
Internet ─► Caddy (your /etc/caddy, in Docker) ─┬─ jaeger.* / otel.* ─► jaeger (Docker)
                                                └─ api.sathishkottravel.com ─► aviation-api:8000 (GraphQL + REST, API_TOKEN)
Docker network "edge": caddy, jaeger, api, worker, producer (traces → jaeger:4318); MongoDB Atlas + CloudAMQP external
```

- **`deploy/docker-compose.prod.yml`:** the three services from GHCR images. They join the `edge` network and publish no
  ports. Settings come from the VM-only `/opt/aviation/.env` (template:
  `deploy/.env.prod.example`).
- **`deploy/remote-deploy.sh`:** `up --wait` on healthchecks. On failure, it goes back to the previous tag and the job
  fails.
- **`deploy/caddy/api.caddy`:** the one site block to add to the VM's Caddyfile.
- **`deploy/infra/`:** Caddy (with your own `/etc/caddy` config) and Jaeger in Docker on `edge`, set up once with
  `sudo ./setup-infra.sh`; see [deploy/infra/README.md](deploy/infra/README.md).
- **Images** install exactly the versions in `uv.lock` (`uv export` + `pip install --require-hashes`), locally and in CI.

One-time setup (Caddy block, deploy user, `.env`, GitHub environment and secrets, GHCR visibility) and operations
(rollback, logs) are in **[deploy/README.md](deploy/README.md)**. To run the e2e suite against production:
`E2E_API_URL=https://api.sathishkottravel.com E2E_API_TOKEN=... uv run pytest -m e2e` (the seed step needs
`E2E_MONGODB_URI` for Atlas).

## Deployment on Render (free tier)

A second deployment runs on Render, in parallel with the VM, from [`render.yaml`](render.yaml): api, producer and
worker as three free web services sharing the same MongoDB Atlas database and CloudAMQP broker. Free services sleep
after 15 minutes idle; the api pings the others (`WAKE_URLS`) so the stack wakes and sleeps together. Setup and
caveats: **[deploy/render/README.md](deploy/render/README.md)**.

## Status

This is a skeleton. Still to come:
- real telemetry normalization
- optional ML anomaly scoring
