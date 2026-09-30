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
Internet ─► Caddy (Docker, your /etc/caddy) ─┬─ jaeger.* / otel.* ─► jaeger (Docker)
                                              ├─ your other sites
                                              └─ api.sathishkottravel.com ─► aviation-api:8000
Docker network "edge": caddy, jaeger, api, worker, producer
  api / worker / producer ──► jaeger:4318 (traces)   api ──► producer:8001 (internal only)
  all ──► MongoDB Atlas, CloudAMQP (external)
```

Caddy and Jaeger are set up once by hand ([`infra/`](infra/README.md)) and deploys never restart them. This repo's
workflow deploys only the three backend services (`deploy/docker-compose.prod.yml`, compose project `aviation`). They
publish no ports: Caddy reaches the api as `aviation-api:8000` on `edge`.

Public API surface (`https://api.sathishkottravel.com`):

| Path | Auth |
| ---- | ---- |
| `/graphql` (POST, GET operations) and GraphQL subscriptions (WebSocket) | `API_TOKEN`: `Authorization: Bearer <token>`; subscriptions send `{"authorization": "Bearer <token>"}` in `connection_init` |
| `POST /api/telemetry` | `API_TOKEN` |
| `DELETE /api/telemetry` | `X-Admin-Token: <ADMIN_TOKEN>` |
| `/health`, `/docs`, `/openapi.json`, the GraphiQL page (`GET /graphql` in a browser) | none |

## One-time setup

Do these before merging the PR that adds the workflow: merging triggers the first deploy.

### 1. Caddy, Jaeger and the `edge` network
Follow [`infra/README.md`](infra/README.md):
1. Point your Caddyfile's upstreams at containers (`jaeger:16686`, `aviation-api:8000`, `host.docker.internal:PORT`),
   and add the `api.sathishkottravel.com` block and its DNS record.
2. Run `sudo ./setup-infra.sh`. It also creates the `edge` network, which the backend deploy needs.

`https://api.sathishkottravel.com` returns 502 until the first backend deploy. That's expected.

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
The deploy uses `docker compose`. On Ubuntu's own Docker, install it with `sudo apt install -y docker-compose-v2`.

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
