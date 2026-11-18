# Deployment and observability

| Where | What | Guide |
| ----- | ---- | ----- |
| Render (free tier) | api, producer, worker | [deploy/render/README.md](../deploy/render/README.md) |
| DigitalOcean droplet | Caddy + Jaeger | [deploy/infra/README.md](../deploy/infra/README.md) |
| GitHub Pages | frontend ([cesium3d-geovis](https://github.com/sathishkottravel/cesium3d-geovis#cesium3d-geovis)) | that repository |
| Any Docker host (optional) | the three services from GHCR images | [Self-hosted backend](#self-hosted-backend-docker-on-a-vm) below |

MongoDB Atlas, CloudAMQP and ADSB.lol are external services.

## Render (free tier)

The backend runs on Render from the [`render.yaml`](../render.yaml) Blueprint: api, producer and worker as three free
web services, deployed on every push to `main`. They use the `aviation-prod` database on MongoDB Atlas and the CloudAMQP
broker. Free services sleep after 15 minutes idle; the api pings the others (`WAKE_URLS`) so the stack wakes and
sleeps together. Setup, secrets, CORS and caveats: **[deploy/render/README.md](../deploy/render/README.md)**.

## DigitalOcean droplet (Caddy + Jaeger)

Caddy terminates TLS and serves the Jaeger UI (`jaeger.*`) and a token-protected OTLP ingest (`otel.*`); Jaeger keeps
traces for 24 hours with tight memory limits. Setup and lean settings: **[deploy/infra/README.md](../deploy/infra/README.md)**.

## Self-hosted backend (Docker on a VM)

Optional: the same three services can run on any x86-64 Docker host (they ran on an Oracle VM before Render), next to
the Caddy + Jaeger infra. Every push to `main` runs
[`.github/workflows/deploy.yml`](../.github/workflows/deploy.yml):
1. unit tests
2. linux/amd64 images pushed to GHCR, tagged with the commit SHA
3. deploy over SSH, with healthchecks, automatic rollback and a smoke test. **This step is currently off** (repository
   variable `DEPLOY_ENABLED`). Deploys are done by hand on the VM with `./deploy/deploy.sh pull` (CI images) or `build` (build on the VM); see
   [deploy/README.md → Manual deploy](../deploy/README.md#manual-deploy).

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
  `sudo ./setup-infra.sh`; see [deploy/infra/README.md](../deploy/infra/README.md).
- **Images** install exactly the versions in `uv.lock` (`uv export` + `pip install --require-hashes`), locally and in CI.

One-time setup (Caddy block, deploy user, `.env`, GitHub environment and secrets, GHCR visibility) and operations
(rollback, logs) are in **[deploy/README.md](../deploy/README.md)**. To run the e2e suite against production:
`E2E_API_URL=https://api.sathishkottravel.com E2E_API_TOKEN=... uv run pytest -m e2e` (the seed step needs
`E2E_MONGODB_URI` for Atlas).

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

- **Logs in traces:** log records at INFO and above that a service writes while a span is active (an API request, an ADS-B poll, a worker message) are added to that span as events. Jaeger shows them under the span's **Logs**. Records from library loggers (OpenTelemetry, pymongo, aio-pika, httpx, uvicorn) are skipped and messages are cut at 1 KB. Jaeger has no logs backend, so this is how logs reach it; the full logs stay in the container output.
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
