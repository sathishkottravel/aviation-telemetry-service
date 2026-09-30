# Shared VM infrastructure: Caddy + Jaeger

This folder is a sample stack for the Oracle VM that you run **by hand**. It is not deployed by GitHub Actions, and
backend deploys never restart it.

| Service | What it does |
| ------- | ------------ |
| **Caddy** (`caddy:2`) | HTTPS for every hostname on the VM, with automatic Let's Encrypt certificates |
| **Jaeger v2 all-in-one** (`jaegertracing/jaeger:2.21.0`) | Receives OTLP traces and serves the Jaeger UI, with persistent Badger storage (72 h retention) |

```
Internet ─► Caddy :80/:443 ─┬─ jaeger.sathishkottravel.com ─► jaeger:16686    Jaeger UI (public, read-only)
                            ├─ otel.sathishkottravel.com   ─► jaeger:4318 / :4317 (gRPC)
                            │                                 OTLP ingest, requires Authorization: Bearer <OTEL_INGEST_TOKEN>
                            └─ sites/*.caddy               ─► per-project sites, e.g. api.sathishkottravel.com → aviation-api:8000

Docker network "edge" (external, shared): caddy, jaeger, and each project's containers
  project containers ──OTLP──► http://jaeger:4318  (internal; no token needed, never leaves the VM)
```

The aviation backend (`deploy/docker-compose.prod.yml`, deployed by the GitHub workflow) joins `edge`. It is served
through `sites/api.caddy` and sends its traces to `jaeger` internally. Other projects can keep sending traces to
`otel.sathishkottravel.com` with the token.

## Files

| File | Purpose |
| ---- | ------- |
| `docker-compose.yml` | Caddy, Jaeger, and a one-shot `jaeger-init` that makes the data volume writable for Jaeger's non-root user |
| `Caddyfile` | The Jaeger UI site, the token-protected OTLP site, and `import sites/*.caddy` |
| `jaeger-config.yaml` | Jaeger v2: OTLP receivers on 4317/4318, Badger storage with a 72 h TTL, UI on 16686, health on 13133 |
| `.env.example` | `JAEGER_DOMAIN`, `OTEL_DOMAIN`, `OTEL_INGEST_TOKEN` |
| `sites/` | One `*.caddy` file per project |

## 1. Prerequisites

- **Docker Engine and the compose plugin** on the VM.
- **DNS `A` records** pointing to the VM's public IP: `jaeger.sathishkottravel.com`, `otel.sathishkottravel.com` and
  `api.sathishkottravel.com`.
- **Ports 80 and 443 open in two places:**
  - **Oracle Console:** in the VCN's Security List, add ingress rules for TCP 80 and 443, and UDP 443 for HTTP/3
    (optional).
  - **The VM's own firewall:** Oracle's Ubuntu images block these ports by default:
    ```sh
    sudo iptables -I INPUT 6 -p tcp -m multiport --dports 80,443 -j ACCEPT
    sudo iptables -I INPUT 6 -p udp --dport 443 -j ACCEPT
    sudo netfilter-persistent save
    ```
    On Oracle Linux, use `sudo firewall-cmd --permanent --add-service=http --add-service=https && sudo firewall-cmd --reload`.

## 2. Replace your current Caddy and Jaeger

Ports 80 and 443 can only be used by one Caddy.

1. **Note your current OTLP token** and the header your other project sends it in. The new `otel.*` site expects
   `Authorization: Bearer <token>`. If your project sends it differently, adjust the `@authorized` matcher in the
   `Caddyfile`.
2. **Stop and remove the old containers** (`docker ps` shows their names), or stop a Caddy that runs as a system service:
   ```sh
   docker stop <old-caddy> <old-jaeger> && docker rm <old-caddy> <old-jaeger>
   # or: sudo systemctl disable --now caddy
   ```
3. **Nothing else carries over.** The new Caddy gets fresh certificates on its first start, which is well within Let's
   Encrypt's limits. Traces in the old Jaeger aren't migrated; an all-in-one without configured storage kept them
   in memory anyway.

## 3. Install

```sh
docker network create edge                        # once; shared by every stack on the VM
sudo install -d -o $USER /opt/infra
# copy the contents of deploy/infra/ to /opt/infra, e.g. from your machine:
#   scp -r deploy/infra/* deploy/infra/.env.example <user>@<vm>:/opt/infra/
cd /opt/infra
cp .env.example .env && chmod 600 .env            # set the domains and OTEL_INGEST_TOKEN (your existing token)
cp <repo>/deploy/caddy/api.caddy sites/           # the aviation API site (api.sathishkottravel.com → aviation-api:8000)
docker compose up -d
docker compose ps                                 # jaeger (healthy), caddy (running); jaeger-init exited 0
```

## 4. Check

```sh
curl -sI https://jaeger.sathishkottravel.com | head -1                                            # HTTP/2 200
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://otel.sathishkottravel.com/v1/traces      # 401
curl -s -o /dev/null -w '%{http_code}\n' -X POST -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/x-protobuf' https://otel.sathishkottravel.com/v1/traces           # 200
docker network inspect edge --format '{{range .Containers}}{{.Name}} {{end}}'                     # infra-caddy-1 infra-jaeger-1
```

`https://api.sathishkottravel.com` returns **502 until the backend is deployed**, which is expected. Then continue
with [the backend runbook](../README.md).

Exporter settings for other projects:

| Protocol | Endpoint | Headers |
| -------- | -------- | ------- |
| OTLP/HTTP | `OTEL_EXPORTER_OTLP_ENDPOINT=https://otel.sathishkottravel.com` with `OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf` | `OTEL_EXPORTER_OTLP_HEADERS=Authorization=Bearer%20<token>` |
| OTLP/gRPC | `OTEL_EXPORTER_OTLP_ENDPOINT=https://otel.sathishkottravel.com:443` | same |

## 5. Operations

| Task | Command (in `/opt/infra`) |
| ---- | ------------------------- |
| Add or change a project site | put a `*.caddy` file in `sites/`, then `docker compose exec caddy caddy reload --config /etc/caddy/Caddyfile` (no restart; a bad file is rejected and the old config keeps running) |
| Change the Caddyfile | edit it, then run the same reload |
| Change `.env` (domains, token) | `docker compose up -d` (recreates Caddy; certificates are kept in the `caddy-data` volume) |
| Update images | bump the tags in `docker-compose.yml`, then `docker compose pull && docker compose up -d` |
| Logs | `docker compose logs -f caddy` / `jaeger` (rotated at 3 × 10 MB) |
| Jaeger disk usage | `docker system df -v \| grep jaeger-data`. Traces older than 72 h are deleted automatically; change `ttl.spans` in `jaeger-config.yaml` and `docker compose up -d jaeger` to tune it |
| Wipe all traces | `docker compose down jaeger && docker volume rm infra_jaeger-data && docker compose up -d` |

The stack keeps running when the backend is redeployed or removed. Caddy only proxies to `aviation-api` while that
container exists.
