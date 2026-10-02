# API reference

GraphQL and REST endpoints of the api and the producer, authentication, CORS, and live ADS-B tracking.
Running locally: [development.md](development.md).

## GraphQL: api-service, `http://localhost:8000/graphql`

GraphiQL is served at the same URL. Subscriptions use WebSocket on the same path and support both the `graphql-transport-ws` and `graphql-ws` protocols (Apollo Client works with either).

| Operation | Kind | Purpose |
| --------- | ---- | ------- |
| `flight(flightId)` | query | Flight metadata and planned route (waypoint idents) |
| `route(flightId)` | query | The flight's waypoints, in flight order |
| `airports` | query | All airports |
| `waypoints` | query | All waypoints |
| `telemetryHistory(flightId, start, end)` | query | Stored positions, oldest first; `start`/`end` optional |
| `trackableAircraft(latitude, longitude, radiusNm)` | query | Aircraft in an ADS-B area, nearest first; no arguments = the producer's polled area |
| `trackingStatus(aircraftId)` | query | Live tracking state for an ICAO hex, a callsign, or `*` |
| `startTracking(aircraftId, latitude, longitude, radiusNm)` | mutation | Start live ADS-B tracking (ICAO hex, callsign or `*`); area arguments move the producer's polled area |
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

query TrackableAircraftNearLondon {
  trackableAircraft(latitude: 51.47, longitude: -0.45, radiusNm: 40) {
    fetchedAt latitude longitude radiusNm
    aircraft { icaoHex callsign distanceNm altitude tracked }
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

mutation StartTrackingAreaNearLondon {
  startTracking(aircraftId: "*", latitude: 51.47, longitude: -0.45, radiusNm: 40) { aircraftId running startedAt }
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

## Authentication

Set `API_TOKEN` to require a token (production does); leave it empty to keep the API open (local development, the
default compose setup, tests).

| Endpoint | Requires |
| -------- | -------- |
| GraphQL over HTTP (`POST /graphql`, `GET /graphql?query=...`) | `Authorization: Bearer <API_TOKEN>` |
| GraphQL subscriptions (WebSocket `/graphql`) | `{"authorization": "Bearer <API_TOKEN>"}` in the `connection_init` payload (browsers can't set WebSocket headers); otherwise the socket closes with 4403 |
| `POST /api/telemetry` | `Authorization: Bearer <API_TOKEN>` |
| `DELETE /api/telemetry` | `X-Admin-Token: <ADMIN_TOKEN>` (separate, see [Telemetry retention](development.md#telemetry-retention)) |
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

## REST endpoints

**api-service** (`http://localhost:8000`, OpenAPI docs at `/docs`)

| Method | Path | Purpose | Responses |
| ------ | ---- | ------- | --------- |
| `GET` | `/health` | Liveness, and whether RabbitMQ is connected | 200 |
| `GET` | `/health/all` | api, producer (`PRODUCER_URL`) and worker (`WORKER_URL`) in one call: `{"status": "ok" \| "degraded", "services": {...}}`, each `ok`, `unreachable` or `not_configured` | 200 |
| `POST` | `/api/telemetry` | Ingest one telemetry record; it is published to RabbitMQ, not written directly | 202, 422 invalid, 503 RabbitMQ down |
| `DELETE` | `/api/telemetry?flight_id=&before=&older_than_hours=` | Prune telemetry; needs `X-Admin-Token` | 200 `{"deleted": n}`, 401, 403 disabled, 422, 503 |
| `POST` / `GET` / WebSocket | `/graphql` | GraphQL (see above) and GraphiQL | 200 |

**adsb-producer** (`http://localhost:8001`, OpenAPI docs at `/docs`). When `PRODUCER_TOKEN` is set (Render), the
`/ingestion/live/*` routes need `Authorization: Bearer <PRODUCER_TOKEN>` (401 otherwise); the api sends it.

| Method | Path | Purpose | Responses |
| ------ | ---- | ------- | --------- |
| `GET` | `/health` | Liveness, and whether RabbitMQ is connected | 200 |
| `GET` | `/ingestion/live/aircraft?latitude=&longitude=&radius_nm=` | Aircraft in an area, nearest first, with a `tracked` flag; no parameters = the polled area | 200, 422 invalid area, 503 no snapshot yet |
| `POST` | `/ingestion/live/start/{aircraft_id}?latitude=&longitude=&radius_nm=` | Start tracking (ICAO hex, callsign or `*`); area parameters move the polled area | 202, 409 already tracked, 422 invalid area, 503 RabbitMQ down |
| `POST` | `/ingestion/live/stop/{aircraft_id}` | Stop tracking | 200, 404 not tracked |
| `GET` | `/ingestion/live/status/{aircraft_id}` | Tracking state | 200 (`running: false` when unknown) |

**CORS:** set `CORS_ORIGINS` (comma-separated origins, e.g. `https://sathishkottravel.github.io`) to let a browser frontend call the api, the producer and the worker's `/health`. Empty = no CORS headers. `GET /health/all` on the api checks all three services in one call.

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

## Live ADS-B ingestion

The producer queries ADSB.lol for every aircraft in one area and picks out the tracked ones. The area starts as `ADSB_RADIUS_NM` around `ADSB_LATITUDE`/`ADSB_LONGITUDE`; start tracking with area arguments to move it (see **Choose the area** below). It uses each aircraft's ICAO hex code as `flight_id` and keeps the callsign in `callsign`. Records without a position are skipped. Telemetry goes through the same RabbitMQ pipeline as `POST /api/telemetry`; the producer never writes to MongoDB.

List the aircraft you can track, nearest first. Without parameters the list reuses the poller's latest snapshot, so it doesn't add ADSB.lol requests; with area parameters it fetches that area once:

```sh
curl http://localhost:8001/ingestion/live/aircraft   # fetched_at, latitude, longitude, radius_nm, aircraft[{icao_hex, callsign, distance_nm, tracked, ...}]
curl "http://localhost:8001/ingestion/live/aircraft?latitude=51.47&longitude=-0.45&radius_nm=40"   # another area
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

query TrackableAircraftElsewhere {
  trackableAircraft(latitude: 51.47, longitude: -0.45, radiusNm: 40) {
    fetchedAt latitude longitude radiusNm
    aircraft { icaoHex callsign distanceNm altitude tracked }
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

**Choose the area.** The producer polls one area, shared by every tracked aircraft (one ADSB.lol request per poll). It starts as `ADSB_LATITUDE`/`ADSB_LONGITUDE`/`ADSB_RADIUS_NM`. `startTracking` with any of `latitude`, `longitude`, `radiusNm` (REST: `radius_nm`) moves it there; values left out come from those env defaults, and a start without area arguments leaves the area as it is. `trackableAircraft` with area arguments lists that area without moving the polled one (one extra ADSB.lol request). Limits: latitude ±90, longitude ±180, radius up to 250 NM; invalid values return `BAD_USER_INPUT` in GraphQL and 422 over REST. The area resets to the defaults when the producer restarts.

```graphql
# Look around Heathrow first (does not move the polled area)
query AreaLondon {
  trackableAircraft(latitude: 51.47, longitude: -0.45, radiusNm: 40) {
    latitude longitude radiusNm aircraft { icaoHex callsign distanceNm tracked }
  }
}

# Track every aircraft there: the producer now polls this area for all tracked aircraft
mutation TrackLondon {
  startTracking(aircraftId: "*", latitude: 51.47, longitude: -0.45, radiusNm: 40) { aircraftId running }
}

# Only the radius given: latitude/longitude come from ADSB_LATITUDE/ADSB_LONGITUDE
mutation TrackHomeWide {
  startTracking(aircraftId: "SAS87C", radiusNm: 150) { aircraftId running }
}

subscription LiveLondon {
  liveTelemetry(flightId: "*") { flightId callsign latitude longitude altitude }
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

# Track everything around Heathrow: moves the polled area (missing values come from the ADSB_* defaults)
curl -X POST "http://localhost:8001/ingestion/live/start/*?latitude=51.47&longitude=-0.45&radius_nm=40"   # 422 if out of range
```

Then query `telemetryHistory(flightId: "4ab563")` or subscribe to `liveTelemetry(flightId: "4ab563")` on the API. Always use the lowercase ICAO hex (`icaoHex`) as `flightId`, even if you started tracking by callsign.

Aircraft listed in `ADSB_PRODUCER_AIRCRAFT` (comma-separated) are tracked from startup, in the default area. Tracking lives in memory, so a restart forgets aircraft started over HTTP and resets the area to the defaults below.

| Setting | Default | Meaning |
| ------- | ------- | ------- |
| `ADSB_POLL_INTERVAL` | `5` | Seconds between polls |
| `ADSB_LATITUDE`, `ADSB_LONGITUDE` | `59.3`, `18.0` | Default centre of the polled area (overridable per request) |
| `ADSB_RADIUS_NM` | `100` | Default radius in nautical miles (overridable per request, max 250) |
| `ADSB_PRODUCER_AIRCRAFT` | empty | Aircraft tracked from startup (`*` = whole area) |
| `ADSB_USER_AGENT` | project name + repo URL | ADSB.lol rejects generic User-Agents with 403 |
| `PRODUCER_URL` | `http://localhost:8001` | Where the API reaches the producer (`trackableAircraft`, tracking mutations) |

- **One request per interval.** A single area poller fetches the area once per interval and picks out every requested aircraft locally, however many are tracked. Start and stop only change the set of requested IDs. Nothing is requested while the set is empty.
- **Rate limits.** ADSB.lol rate-limits roughly this often and answers 429. The producer then pauses requests for all trackers (10 s, doubling up to 120 s) and resumes on its own. Raising `ADSB_POLL_INTERVAL` to 10 or more avoids most 429s.
