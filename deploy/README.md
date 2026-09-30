# Production deployment on the Oracle VM

Every push to `main` runs `.github/workflows/deploy.yml`:

```
push to main ─► test (uv run pytest)
             ─► build api / worker / producer for linux/arm64 on a native ARM runner
                → ghcr.io/sathishkottravel/aviation-telemetry-service/<svc>:<commit-sha>
             ─► deploy (GitHub environment "production")
                scp docker-compose.prod.yml + remote-deploy.sh → /opt/aviation
                ssh: remote-deploy.sh <sha>  → up --wait on healthchecks, roll back to the previous sha on failure
                smoke test: /health, GraphQL with the token, 401 without it
```

## What runs where

```
Internet ─► Caddy (shared, already on the VM) ─┬─ jaeger.sathishkottravel.com ─► Jaeger UI            (unchanged)
                                               ├─ otel.sathishkottravel.com   ─► OTLP + token        (unchanged, other projects)
                                               └─ api.sathishkottravel.com    ─► aviation-api:8000   (this repo)

Docker network "edge" (external): caddy, jaeger, api, worker, producer
  api / worker / producer ──OTLP http──► jaeger:4318  (internal)
  api ──► producer:8001                               (internal only; the producer is not routed by Caddy)
  all ──► MongoDB Atlas, CloudAMQP                    (external)
```

Caddy and Jaeger are shared VM infrastructure and are **not** managed by this repo; deploys never restart them.
This repo deploys only the three backend services (`deploy/docker-compose.prod.yml`, compose project `aviation`).

Public API surface (`https://api.sathishkottravel.com`):

| Path | Auth |
| ---- | ---- |
| `/graphql` (POST, GET operations) and GraphQL subscriptions (WebSocket) | `API_TOKEN`: `Authorization: Bearer <token>`; subscriptions send `{"authorization": "Bearer <token>"}` in `connection_init` |
| `POST /api/telemetry` | `API_TOKEN` |
| `DELETE /api/telemetry` | `X-Admin-Token: <ADMIN_TOKEN>` |
| `/health`, `/docs`, `/openapi.json`, the GraphiQL page (`GET /graphql` in a browser) | none |

## One-time setup

Do these before merging the PR that adds the workflow: merging triggers the first deploy.

### 1. Shared infrastructure: Caddy, Jaeger, and the `edge` network
Set up the sample stack in [`deploy/infra/`](infra/README.md) on the VM by hand. It covers:
- DNS for `api.sathishkottravel.com`, next to `jaeger.*` and `otel.*`
- opening ports 80 and 443 in Oracle's Security List and the VM's own firewall
- replacing your current Caddy and Jaeger
- `docker network create edge`
- copying [`caddy/api.caddy`](caddy/api.caddy) into `/opt/infra/sites/`

Afterwards, `https://api.sathishkottravel.com` returns 502 until the first backend deploy. That's expected.

The backend sends traces to `http://jaeger:4318` over `edge` (`OTEL_EXPORTER_OTLP_ENDPOINT` in `.env`), not through
`otel.*`.

### 2. Deploy user and folder
```sh
sudo useradd --create-home --shell /bin/bash deploy
sudo usermod -aG docker deploy              # docker only; no sudo
sudo install -d -o deploy -g deploy /opt/aviation
sudo -u deploy install -d -m 700 /home/deploy/.ssh
# On your machine: ssh-keygen -t ed25519 -f aviation-deploy -C github-actions-deploy -N ""
# Put aviation-deploy.pub into /home/deploy/.ssh/authorized_keys (mode 600, owned by deploy).
```
Password SSH logins should be off (`PasswordAuthentication no`); `fail2ban` is recommended.

### 3. Production settings
```sh
sudo -u deploy cp .env.prod.example /opt/aviation/.env   # copy the file from this folder
sudo -u deploy chmod 600 /opt/aviation/.env
```
Fill in MongoDB Atlas, CloudAMQP (`amqps://`), `API_TOKEN` and `ADMIN_TOKEN` (two different long random values). The
workflow never overwrites `.env`. In **Atlas → Network Access**, allow the VM's public IP, and seed the navigation data
once from your machine: `MONGODB_URI=<atlas-uri> uv run seed-db`.

### 4. GitHub
- **Settings → Environments → New environment `production`** (optionally add yourself as a required reviewer, so each
  deploy waits for approval).
- Environment secrets:

  | Secret | Value |
  | ------ | ----- |
  | `VM_HOST` | the VM's public IP (or a hostname for it) |
  | `VM_USER` | `deploy` |
  | `VM_SSH_KEY` | the private key `aviation-deploy` |
  | `VM_KNOWN_HOSTS` | output of `ssh-keyscan -t ed25519 <VM_HOST>`; compare its fingerprint with the VM's (`ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` on the VM) |
  | `API_DOMAIN` | `api.sathishkottravel.com` |
  | `API_TOKEN` | same value as in the VM's `.env` (used by the smoke test) |

- After the first run has pushed images: **your profile → Packages →** `aviation-telemetry-service/api`, `/worker`,
  `/producer` **→ Package settings → Change visibility → Public**, so the VM pulls without credentials. (The first deploy
  fails to pull until then; re-run the failed job afterwards.)

## Operations

| Task | How |
| ---- | --- |
| Deploy | Push/merge to `main` |
| Roll back / redeploy a version | **Actions → deploy → Run workflow** on `main` with `image_tag` = an earlier commit SHA (skips test/build) |
| Status | `cd /opt/aviation && IMAGE_TAG=$(cat .deployed-tag) docker compose -f docker-compose.prod.yml ps` |
| Logs | `... docker compose -f docker-compose.prod.yml logs -f api` (logs rotate at 3 × 10 MB per container) |
| Change settings | edit `/opt/aviation/.env`, then re-run the latest deploy (or `up -d` with `IMAGE_TAG=$(cat .deployed-tag)`) |
| Disk | each deploy removes this project's images except the current and previous tag |

A failed deploy (a service not healthy within 180 s) prints the container state and logs in the job output and puts the
previous version back; the job fails so you notice. Oracle may reclaim Always Free instances that stay idle for a week;
continuous ADS-B polling usually keeps the VM busy enough, but check Oracle's current policy.
