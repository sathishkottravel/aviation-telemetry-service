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
| `telemetry` | `flight_id`, `timestamp`, `latitude`, `longitude`, `altitude`, `ground_speed`, `track`, `vertical_rate`, `callsign` (optional) | `flight_id` + `timestamp` |

The worker and the seed script create the indexes automatically.

## Project layout

```
api-service/api_service/
  main.py                    app + startup/shutdown
  api/controllers.py         REST: /health, POST /api/telemetry
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
```

## Run with Docker

```sh
docker compose up --build
```

- API and GraphiQL: http://localhost:8000/graphql
- ADS-B producer: http://localhost:8001/docs
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

Subscribe to live updates (open this in GraphiQL, then send telemetry):

```graphql
subscription {
  liveTelemetry(flightId: "SAS123") {
    timestamp latitude longitude altitude groundSpeed track
  }
}
```

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
- Subscribe with `liveTelemetry(flightId: "*")` to receive every flight on one subscription.

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

## Status

This is a skeleton. Still to come:
- real telemetry normalization
- optional ML anomaly scoring
