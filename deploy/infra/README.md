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

## 2. Prepare (no downtime; your current Caddy keeps serving)

These steps assume your current Caddy is installed **directly on the VM** (not in Docker), usually as the systemd
service `caddy` reading `/etc/caddy/Caddyfile`.

### 2.1 Inspect what runs today
```sh
systemctl status caddy --no-pager      # host Caddy service (or: ps aux | grep caddy)
cat /etc/caddy/Caddyfile               # plus any files it imports
docker ps                              # is the current Jaeger a container?
sudo ss -ltnp | grep -E ':(80|443|4317|4318|16686)\b'   # who holds these ports
```
Note your current **OTLP token** and how the other project sends it. The new `otel.*` site expects
`Authorization: Bearer <token>`; if your old block checked something else, adjust the `@authorized` matcher in the
`Caddyfile`.

### 2.2 Put the stack in place
```sh
docker network create edge                        # once; shared by every stack on the VM
sudo install -d -o $USER /opt/infra
# copy the contents of deploy/infra/ to /opt/infra, e.g. from your machine:
#   scp -r deploy/infra/* deploy/infra/.env.example <user>@<vm>:/opt/infra/
cd /opt/infra
cp .env.example .env && chmod 600 .env            # set the domains and OTEL_INGEST_TOKEN (your existing token)
cp <repo>/deploy/caddy/api.caddy sites/           # the aviation API site (api.sathishkottravel.com → aviation-api:8000)
```

### 2.3 Move your other sites from the host Caddyfile
The stack's `Caddyfile` already covers `jaeger.*` and `otel.*`. Put **every other site block** from
`/etc/caddy/Caddyfile` into its own file in `/opt/infra/sites/`, and change upstreams that point to the VM itself:

| In `/etc/caddy/Caddyfile` (host) | In `/opt/infra/sites/<name>.caddy` (container) |
| -------------------------------- | ---------------------------------------------- |
| `reverse_proxy localhost:3000` | `reverse_proxy host.docker.internal:3000` |
| `reverse_proxy 127.0.0.1:3000` | `reverse_proxy host.docker.internal:3000` |
| `reverse_proxy some-container:3000` (published port) | attach that container to `edge` and use its name, or keep `host.docker.internal:<published port>` |
| `root * /var/www/site` + `file_server` | also mount the folder into the Caddy container (`- /var/www/site:/var/www/site:ro` in `docker-compose.yml`) |

`host.docker.internal` is the VM itself, as seen from the Caddy container (`extra_hosts` in `docker-compose.yml`). A
service on the VM that listens **only on `127.0.0.1` can't be reached from a container**. Make it listen on `0.0.0.0`
(or on the Docker bridge address `172.17.0.1`). The VM firewall and Oracle's Security List keep that port closed to
the internet, as long as you don't open it there.

### 2.4 Validate before switching
```sh
cd /opt/infra
docker run --rm --env-file .env -v "$PWD/Caddyfile:/etc/caddy/Caddyfile:ro" -v "$PWD/sites:/etc/caddy/sites:ro" \
  caddy:2 caddy validate --config /etc/caddy/Caddyfile        # must end with "Valid configuration"
docker compose up -d jaeger                                   # Jaeger publishes no ports, so it can start now
docker compose ps                                             # jaeger (healthy); jaeger-init exited 0
```

## 3. Switch over (a short outage while ports 80/443 change hands)

```sh
sudo systemctl disable --now caddy          # stop the host Caddy and keep it from starting at boot
cd /opt/infra && docker compose up -d       # starts the Caddy container on 80/443
docker compose logs -f caddy                # wait for "certificate obtained successfully" for each domain
```
Then stop the old Jaeger, which the stack's Jaeger replaces. If it's a container, run
`docker stop <old-jaeger> && docker rm <old-jaeger>`; if it runs on the host, stop its service or process. Traces in
the old Jaeger aren't migrated.

**Rollback** (your old config and certificates were never touched):
```sh
cd /opt/infra && docker compose stop caddy && sudo systemctl enable --now caddy
```

**Later, once everything works:** `sudo apt remove caddy`, or leave the package installed but disabled. The container
issues and stores its own certificates in the `caddy-data` volume.

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

## Alternative: keep Caddy on the host (not recommended)

If you'd rather keep the Caddy installed on the VM, it can't reach containers by name, so publish their ports on
loopback and point the host Caddyfile at them:

1. **Stack Jaeger:** add `ports: ["127.0.0.1:16686:16686", "127.0.0.1:4317:4317", "127.0.0.1:4318:4318"]` to `jaeger`.
   Run only Jaeger: `docker compose up -d jaeger`.
2. **Backend API:** add `ports: ["127.0.0.1:8000:8000"]` to `api` in `deploy/docker-compose.prod.yml`.
3. **Host Caddyfile:** `reverse_proxy 127.0.0.1:16686` for the UI, and `127.0.0.1:4318` / `h2c://127.0.0.1:4317` for
   OTLP (keep your token check). For the API, use `reverse_proxy 127.0.0.1:8000`.

Everything else stays the same; the backend still sends traces to `jaeger` over `edge`. The downsides: Caddy's config
stays outside the repo and is managed by hand, and ports have to be coordinated per project.
