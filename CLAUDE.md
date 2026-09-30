Building a backend project to serve realtime aviation data and also serving historical data for playback.
Preference for separate services not a single-app.

Repository layout (uv workspace, Python 3.12)
- api-service/       FastAPI + Strawberry GraphQL (package api_service), own pyproject.toml and Dockerfile
- telemetry-worker/  RabbitMQ consumer (package telemetry_worker), own pyproject.toml and Dockerfile
- adsb-producer/     FastAPI service (package adsb_producer, port 8001) that polls ADSB.lol and owns the /ingestion/live/{start,stop,status}/{aircraft_id} controllers; own pyproject.toml and Dockerfile
- shared/            telemetry_shared: config, models, database (MongoDB), messaging (RabbitMQ), adsb (ADSB.lol client + normalization) used by all services
- Controllers stay in api_service/api/controllers.py (REST), api_service/graphql/schema.py (GraphQL) and adsb_producer/api/controllers.py; business logic goes in each service's services/ folder.
- Every telemetry source publishes through RabbitMQ.publish_telemetry(); only the worker writes telemetry to MongoDB.
- The API reaches the producer over HTTP via PRODUCER_URL (trackableAircraft, trackingStatus, startTracking/stopTracking). The producer owns tracking state; the API only forwards. The API must keep working when the producer is down.
- Dockerfiles are built from the repo root as context (docker build -f api-service/Dockerfile .) so shared/ is included.

Commands
- Install: uv sync --all-packages
- API: uv run --package api-service uvicorn api_service.main:app --reload
- Worker: uv run --package telemetry-worker telemetry-worker
- Producer: uv run --package adsb-producer uvicorn adsb_producer.main:app --port 8001 --reload
- Seed: uv run seed-db
- Full stack: docker compose up --build
- Tests: uv run pytest (unit) | uv run pytest -m integration (needs mongo + rabbitmq) | uv run pytest -m e2e (needs full stack). Unit tests go in <service>/tests/, integration in tests/integration/, GraphQL e2e in tests/e2e/. Keep the README API reference in sync when adding GraphQL operations or REST endpoints.

Conventions
- Do not add Claude attribution (Co-Authored-By, "Generated with Claude Code") to commits or PRs.
- The API and producer must keep starting when MongoDB or RabbitMQ is unavailable.
- Tracing: optional OpenTelemetry via telemetry_shared.observability.tracing (setup_tracing before creating Mongo/HTTP clients; custom spans with its tracer). Trace context crosses RabbitMQ in message headers (inject_headers/extract_context). Never put connection strings, credentials, tokens or query documents in span attributes. Everything must work with OTEL_ENABLED=false or Jaeger down. OTEL_EXPORTER_OTLP_PROTOCOL selects grpc (default) or http/protobuf (Grafana Cloud). Every FastAPI() must pass telemetry={"auto_configure": False}: FastAPI >= 0.142 otherwise adds its own OTLP exporters and refuses to start with grpc.
- Production: backend on the Oracle VM via .github/workflows/deploy.yml (every push to main deploys: test → ARM64 GHCR images tagged with the SHA → SSH deploy/remote-deploy.sh with healthcheck rollback). deploy/docker-compose.prod.yml holds only api/worker/producer; the VM's Caddy and Jaeger all-in-one are shared infrastructure run by hand from deploy/infra/ (network "edge"; project sites in /opt/infra/sites/*.caddy), never by the workflow. MongoDB/RabbitMQ are external. Images must install from uv.lock (uv export + pip --require-hashes).
- Public API auth: API_TOKEN (Bearer header; connection_init payload for subscriptions) guards GraphQL and POST /api/telemetry; ADMIN_TOKEN guards DELETE; /health, /docs and the GraphiQL page stay open. Empty API_TOKEN = open (local/tests).
- Telemetry retention: TTL index on telemetry.timestamp (TELEMETRY_TTL_DAYS, default 1, applied by ensure_indexes). On-demand pruning is DELETE /api/telemetry, guarded by X-Admin-Token == ADMIN_TOKEN (disabled when unset) and requiring at least one filter.
- ADSB.lol requires a descriptive User-Agent (ADSB_USER_AGENT) and rate-limits aggressively. Keep a single area poller: start/stop only change the set of requested aircraft IDs; never add per-aircraft polling loops. The ID '*' means every aircraft in the area (start/stop/status and liveTelemetry); de-duplicate publishing by ICAO hex so overlapping IDs publish once.
- liveTelemetry returns [Telemetry]: a snapshot of the latest position of every matching flight (one flight ID or '*'), sent on subscribe and on change after a 0.5 s settle window, at most once per second; flights silent for 5 minutes drop out. The cache lives in the API's LiveTelemetryBroadcaster (per process, in memory).

Backend skeleton
Create a small Python FastAPI backend for an aviation flight-tracking demo. Keep the architecture simple and easy to maintain. Use Strawberry GraphQL with FastAPI. The backend should support one selected flight at a time and expose GraphQL queries for flight metadata, planned route, waypoints, and historical telemetry. Also prepare a GraphQL subscription for live telemetry updates. Use clear folder separation for API, models, services, messaging, and persistence, but avoid overengineering.

Navigation and telemetry database with MongoDB
Use MongoDB as the persistence layer, preferably MongoDB Atlas so the deployed Render backend can connect to it using a MONGODB_URI environment variable.
Create a small initial aviation dataset containing airports, waypoints, one sample flight, and its ordered planned route. Store these using simple MongoDB collections such as airports, waypoints, flights, and telemetry.
The flights collection should contain the flight identifier, callsign, origin, destination, and an ordered list of waypoint identifiers or route points.
The telemetry collection should store historical aircraft state with fields such as flight ID, timestamp, latitude, longitude, altitude, ground speed, track, and vertical rate.
Create a small seed script that inserts enough airport, waypoint, and flight data to demonstrate one route such as ESSP → waypoints → ESSA.

FastAPI
   ↓
GraphQL
   ↓
MongoDB Atlas
   ├─ airports
   ├─ waypoints
   ├─ flights
   └─ telemetry history

Telemetry ingestion and RabbitMQ
Add a FastAPI ingestion endpoint that accepts normalized aircraft telemetry containing flight ID, timestamp, latitude, longitude, altitude, ground speed, track, and vertical rate. When telemetry is received, publish it to RabbitMQ. Add one RabbitMQ consumer that receives telemetry events and persists them to the telemetry history collection. Avoid introducing multiple workers or complicated routing.
Because the worker and API are separate processes, RabbitMQ uses exactly two exchanges:
- telemetry.ingest (direct): API publishes with routing key telemetry.raw → durable queue telemetry.ingest → worker.
- telemetry.live (fanout): worker publishes each processed record → one exclusive, auto-delete queue per API instance (5 s message TTL) → in-memory subscribers.

Live GraphQL subscription
Connect the RabbitMQ telemetry consumer to the GraphQL subscription layer. When a telemetry message is processed, publish the latest normalized telemetry to active GraphQL subscribers for that flight. The frontend should subscribe using liveTelemetry(flightId: ID!). Keep the real-time fan-out in memory. Do not introduce Redis for this version.

Backend flow
Mock / ADS-B telemetry
        ↓
FastAPI ingest (api-service)
        ↓
RabbitMQ telemetry.ingest
        ↓
Telemetry worker
       / \
      /   \
MongoDB    RabbitMQ telemetry.live
history          ↓
         api-service in-memory fan-out
                 ↓
         GraphQL subscription
                 ↓
           Apollo Client

Historical playback
Add a GraphQL query for retrieving telemetry history for a flight, ordered by timestamp. Support optional start and end timestamps. Return the same telemetry model used by the live subscription so that the frontend can use the same aircraft rendering logic for live and playback modes.

Services

Internal Services
1. API Service
   FastAPI + GraphQL
   - flight queries
   - airport/waypoint queries
   - telemetry history
   - GraphQL subscriptions
   - telemetry ingest endpoint

2. Telemetry Worker
   - consumes RabbitMQ
   - validates/normalizes telemetry
   - writes telemetry history to MongoDB
   - forwards live updates to subscription layer

3. ML Service — optional later
   - loads SageMaker-trained model or calls inference endpoint
   - calculates anomaly score
   - optional enrichment of telemetry

External Services
MongoDB Atlas
RabbitMQ / CloudAMQP
AWS S3 + SageMaker