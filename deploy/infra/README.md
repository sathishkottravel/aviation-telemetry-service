# Caddy + Jaeger in Docker on the VM

Runs **your own Caddy config** (`/etc/caddy`) and **Jaeger v2 all-in-one** as containers on a shared Docker network
`edge`. The aviation backend joins the same network, so everything talks by name:

```
Docker network "edge"
  caddy ──► jaeger:16686, jaeger:4318, h2c://jaeger:4317, aviation-api:8000, host.docker.internal:<port>
  aviation api / worker / producer ──► jaeger:4318   (traces; internal, no token)
Only Caddy publishes ports (80, 443).
```

- **Certificates:** Caddy reuses the host Caddy's existing certificates.
- **Jaeger:** keeps traces on disk for 72 hours.
- **Not deployed by the workflow:** this is set up once by hand, and backend deploys never restart it.

## 1. Edit your Caddyfile

Inside a container, `localhost` is the container itself, so change the upstreams in `/etc/caddy/Caddyfile`:

| Site | Before | After |
| ---- | ------ | ----- |
| `jaeger.sathishkottravel.com` | `reverse_proxy localhost:16686` | `reverse_proxy jaeger:16686` |
| `otel.sathishkottravel.com` (keep your token check) | `localhost:4318` / `localhost:4317` | `jaeger:4318` / `h2c://jaeger:4317` |
| `api.sathishkottravel.com` (new; also add a DNS `A` record) | — | `reverse_proxy aviation-api:8000` (see `deploy/caddy/api.caddy`) |
| other apps running directly on the VM | `reverse_proxy localhost:PORT` | `reverse_proxy host.docker.internal:PORT` |

Apps running directly on the VM must listen on `0.0.0.0` (or `172.17.0.1`), not only on `127.0.0.1`. Keep their ports
closed in the firewall and Oracle's Security List; Caddy is the only public entry point.

## 2. Run the setup script

```sh
git clone --branch feature/vm-deploy https://github.com/sathishkottravel/aviation-telemetry-service.git ~/aviation-telemetry-service
cd ~/aviation-telemetry-service/deploy/infra
sudo ./setup-infra.sh
```

It stops at the first problem:
1. Installs Docker Compose v2 if it's missing.
2. Validates your Caddyfile **before changing anything**.
3. Creates the `edge` network.
4. Starts Jaeger and waits until it's healthy.
5. Stops the old Jaeger container(s) and the host Caddy (systemd), recording both for rollback.
6. Starts Caddy and checks port 80.

Keep this folder: the containers are managed from here.

## 3. Roll back if needed

```sh
sudo ./setup-infra.sh --rollback     # stops these containers, restarts your old Jaeger container and host Caddy
```

## Day to day

| Task | Command (in this folder) |
| ---- | ------------------------ |
| Reload after editing `/etc/caddy/Caddyfile` | `docker compose exec caddy caddy reload --config /etc/caddy/Caddyfile` |
| Logs | `docker compose logs -f caddy` / `jaeger` |
| Update images | `docker compose pull && docker compose up -d` |
| Remove the host Caddy package for good | `sudo apt remove caddy` (once you're happy) |

## Lean settings (Jaeger)

Jaeger is kept small so it doesn't slow down the VM:

- **Limits** (`docker-compose.yml`): `mem_limit: 384m`, `cpus: 0.75`, `GOMAXPROCS=1`, and `GOMEMLIMIT=300MiB`, so Go
  collects garbage harder near the limit instead of being OOM-killed.
- **Ingest** (`jaeger-config*.yaml`): the `memory_limiter` processor (250 MiB) refuses spans under memory pressure.
  The services keep working; at worst some spans are dropped. Batches are small and OTLP requests are capped at 4 MB.
- **Retention:** 24h (Badger TTL, matching `TELEMETRY_TTL_DAYS=1`), with value-log GC every minute.
- **UI** (`jaeger-ui.json`): no System Architecture (dependencies) or Monitor tabs, search look-back up to 1 day,
  at most 100 results. Jaeger's own metrics are off and its logs show warnings only.
- **Storage:**
  - Default: Badger on disk (`jaeger-config.yaml`). Traces survive restarts.
  - In-memory alternative (`jaeger-config.memory.yaml`): at most 2000 traces, no disk, traces lost on restart. Switch
    with `JAEGER_CONFIG=jaeger-config.memory.yaml docker compose up -d jaeger`, or put
    `JAEGER_CONFIG=jaeger-config.memory.yaml` in an `.env` file in this folder.

Check usage with `docker stats --no-stream infra-jaeger-1`.

## Notes

- **Files outside `/etc/caddy`:** if your Caddyfile uses other host paths (e.g. `root * /var/www/site`, log files),
  add them as volume lines to `caddy` in `docker-compose.yml`.
- **Environment variables** (`{$VAR}` in your Caddyfile, e.g. from the old systemd unit: `systemctl cat caddy`): put
  them in `caddy.env` in this folder.
- **A different certificates path:** if your host Caddy's data isn't in `/var/lib/caddy/.local/share/caddy`, run with
  `CADDY_DATA_DIR=<path> sudo -E ./setup-infra.sh`.
- **Other projects** can keep sending traces to `https://otel.sathishkottravel.com` with your token.

## Troubleshooting

**`Unable to locate package docker-compose-plugin`**: on Ubuntu's own Docker (`docker.io`), the package is called
`docker-compose-v2`. The script installs it; by hand, run `sudo apt install -y docker-compose-v2`.

**502 from a site:** the upstream isn't reachable from the Caddy container. Check that it uses a container name on
`edge` or `host.docker.internal:PORT` (not `localhost`), and that an app running directly on the VM listens on
`0.0.0.0`.
