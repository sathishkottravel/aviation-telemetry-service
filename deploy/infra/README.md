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
| `setup-infra.sh` | One script that installs, migrates and verifies everything; see below |
| `docker-compose.yml` | Caddy, Jaeger, and a one-shot `jaeger-init` that makes the data volume writable for Jaeger's non-root user |
| `Caddyfile` | The Jaeger UI site, the token-protected OTLP site, and `import sites/*.caddy` |
| `jaeger-config.yaml` | Jaeger v2: OTLP receivers on 4317/4318, Badger storage with a 72 h TTL, UI on 16686, health on 13133 |
| `.env.example` | `JAEGER_DOMAIN`, `OTEL_DOMAIN`, `OTEL_INGEST_TOKEN` |
| `sites/` | One `*.caddy` file per project |

## 1. Prerequisites (outside the VM)

- **DNS `A` records** pointing to the VM's public IP: `jaeger.sathishkottravel.com`, `otel.sathishkottravel.com` and
  `api.sathishkottravel.com`.
- **Oracle Console → VCN → Security List:** ingress rules for TCP 80 and 443, and UDP 443 for HTTP/3 (optional).
  This is the one firewall the script can't change from inside the VM.

Everything else is done by the script, including installing Docker if it's missing and opening the VM's own firewall.

## 2. Copy your site files (the only manual step)

```sh
git clone https://github.com/sathishkottravel/aviation-telemetry-service.git ~/aviation-telemetry-service
sudo install -d /opt/infra/sites
sudo cp ~/aviation-telemetry-service/deploy/caddy/api.caddy /opt/infra/sites/
```

Then move **every other site block** from your current `/etc/caddy/Caddyfile` into its own
`/opt/infra/sites/<name>.caddy`. `jaeger.*` and `otel.*` are already in the stack's `Caddyfile`, so skip those. Change
upstreams that point to the VM itself:

| In `/etc/caddy/Caddyfile` (host) | In `/opt/infra/sites/<name>.caddy` (container) |
| -------------------------------- | ---------------------------------------------- |
| `reverse_proxy localhost:3000` | `reverse_proxy host.docker.internal:3000` |
| `reverse_proxy 127.0.0.1:3000` | `reverse_proxy host.docker.internal:3000` |
| `reverse_proxy some-container:3000` (published port) | attach that container to `edge` and use its name, or keep `host.docker.internal:<published port>` |
| `root * /var/www/site` + `file_server` | also mount the folder into the Caddy container (`- /var/www/site:/var/www/site:ro` in `docker-compose.yml`) |

- **`host.docker.internal`** is the VM itself, as seen from the Caddy container.
- **Services that listen only on `127.0.0.1`** can't be reached from a container. Make them listen on `0.0.0.0` (or on
  the Docker bridge address `172.17.0.1`). Keep their ports closed in the VM firewall and the Security List.
- **OTLP token header:** the stack's `otel.*` site expects `Authorization: Bearer <token>`. If your old block checked a
  different header, adjust the `@authorized` matcher in `deploy/infra/Caddyfile` before running the script.

## 3. Run the setup script

```sh
cd ~/aviation-telemetry-service/deploy/infra
sudo ./setup-infra.sh --otel-token '<your existing OTLP token>'
```

It asks before installing Docker or stopping anything. Add `--yes` to skip the questions. Without `--otel-token`, it
asks for the token; an empty answer generates a new one, and then your other projects need updating. Domains default
to `jaeger.sathishkottravel.com` / `otel.sathishkottravel.com` (change them with `--jaeger-domain` / `--otel-domain`).

What it does, in order (it stops at the first problem):

1. **Checks** it's running as root, installs Docker if it's missing (with the official `get.docker.com` script), and
   checks the compose plugin and `curl` are present.
2. **Opens the VM firewall** for 80/tcp, 443/tcp and 443/udp. It uses `firewalld` if that's active, otherwise
   `iptables` (inserted before Oracle's REJECT rule, never duplicated, saved with `netfilter-persistent`).
3. **Copies the configs** to `/opt/infra`, backing up any changed file to `backups/`. It creates the `edge` network.
   Your `.env` and `sites/*.caddy` files are never overwritten.
4. **Writes `/opt/infra/.env`** (mode 600) with the domains and token.
5. **Lists your site files,** and asks whether to continue if there are none.
6. **Validates** the Caddyfile together with your sites. At this point **nothing has been stopped yet**.
7. **Starts Jaeger** (it publishes no ports, so it runs next to the old setup) and waits until it's healthy.
8. **Stops the old setup.** That's the host services `caddy`, `jaeger` and `jaeger-all-in-one` (stopped and disabled),
   plus any container outside this stack that publishes 80/443 or runs a Jaeger image. Those containers are
   **stopped, not removed.** Everything is recorded in `/opt/infra/.setup-state`. If some other process still holds
   80/443, it rolls back and stops.
9. **Starts Caddy** and reloads it, so re-runs pick up changed site files. If Caddy fails to start, it rolls back.
10. **Verifies over HTTPS**, retrying while certificates are issued:
    - the Jaeger UI returns 200
    - OTLP returns 401 without the token and 200 with it

    If the UI doesn't come up, it points at the likely causes (DNS, the Security List) and offers to roll back.
11. **Prints a summary** with the URLs, the rollback command, and how to remove the old setup for good.

| Command | Effect |
| ------- | ------ |
| `sudo ./setup-infra.sh --rollback` | Stops the new Caddy; restarts the recorded old containers; re-enables and starts the host Caddy/Jaeger services |
| `sudo ./setup-infra.sh` (again) | Safe to re-run: refreshes the configs and reloads Caddy. Use it after changing `sites/*.caddy` or pulling a newer repo |
| `sudo ./setup-infra.sh --help` | All options (`--yes`, `--rollback`, `--jaeger-domain`, `--otel-domain`, `--otel-token`, `--infra-dir`, `--no-firewall`) |

Once everything works, remove the old setup for good. The script's summary prints the exact `docker rm` command for
the old containers. For the host Caddy, run `sudo apt remove caddy`; it's already stopped and disabled.

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
| Add or change a project site | put a `*.caddy` file in `sites/`, then re-run `sudo ./setup-infra.sh` (validates, then reloads without a restart), or reload directly: `docker compose exec caddy caddy reload --config /etc/caddy/Caddyfile` (a bad file is rejected and the old config keeps running) |
| Change the Caddyfile | edit it, then run the same reload |
| Change `.env` (domains, token) | `docker compose up -d` (recreates Caddy; certificates are kept in the `caddy-data` volume) |
| Update images | bump the tags in `docker-compose.yml`, then `docker compose pull && docker compose up -d` |
| Logs | `docker compose logs -f caddy` / `jaeger` (rotated at 3 × 10 MB) |
| Jaeger disk usage | `docker system df -v \| grep jaeger-data`. Traces older than 72 h are deleted automatically; change `ttl.spans` in `jaeger-config.yaml` and `docker compose up -d jaeger` to tune it |
| Wipe all traces | `docker compose down jaeger && docker volume rm infra_jaeger-data && docker compose up -d` |

The stack keeps running when the backend is redeployed or removed. Caddy only proxies to `aviation-api` while that
container exists.


## Appendix: doing it by hand

The script automates these steps. They're kept here for reference.

### A. Prepare (no downtime)

These steps assume your current Caddy is installed **directly on the VM** (not in Docker), usually as the systemd
service `caddy` reading `/etc/caddy/Caddyfile`.

#### Inspect what runs today
```sh
systemctl status caddy --no-pager      # host Caddy service (or: ps aux | grep caddy)
cat /etc/caddy/Caddyfile               # plus any files it imports
docker ps                              # is the current Jaeger a container?
sudo ss -ltnp | grep -E ':(80|443|4317|4318|16686)\b'   # who holds these ports
```
Note your current **OTLP token** and how the other project sends it. The new `otel.*` site expects
`Authorization: Bearer <token>`; if your old block checked something else, adjust the `@authorized` matcher in the
`Caddyfile`.

#### Put the stack in place
```sh
docker network create edge                        # once; shared by every stack on the VM
sudo install -d -o $USER /opt/infra
# copy the contents of deploy/infra/ to /opt/infra, e.g. from your machine:
#   scp -r deploy/infra/* deploy/infra/.env.example <user>@<vm>:/opt/infra/
cd /opt/infra
cp .env.example .env && chmod 600 .env            # set the domains and OTEL_INGEST_TOKEN (your existing token)
cp <repo>/deploy/caddy/api.caddy sites/           # the aviation API site (api.sathishkottravel.com → aviation-api:8000)
```

#### Move your other sites from the host Caddyfile
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

#### Validate before switching
```sh
cd /opt/infra
docker run --rm --env-file .env -v "$PWD/Caddyfile:/etc/caddy/Caddyfile:ro" -v "$PWD/sites:/etc/caddy/sites:ro" \
  caddy:2 caddy validate --config /etc/caddy/Caddyfile        # must end with "Valid configuration"
docker compose up -d jaeger                                   # Jaeger publishes no ports, so it can start now
docker compose ps                                             # jaeger (healthy); jaeger-init exited 0
```

### B. Switch over (a short outage while ports 80/443 change hands)

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

### C. VM firewall
```sh
sudo iptables -I INPUT 6 -p tcp -m multiport --dports 80,443 -j ACCEPT
sudo iptables -I INPUT 6 -p udp --dport 443 -j ACCEPT
sudo netfilter-persistent save
# Oracle Linux: sudo firewall-cmd --permanent --add-service=http --add-service=https && sudo firewall-cmd --reload
```

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
