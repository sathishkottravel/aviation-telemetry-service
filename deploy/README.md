# Production deployment on the Oracle VM

Every push to `main` runs `.github/workflows/deploy.yml`:

```
push to main ─► test (uv run pytest)
             ─► build api / worker / producer for linux/amd64 (the VM is an x86 AMD shape)
                → ghcr.io/sathishkottravel/aviation-telemetry-service/<svc>:<commit-sha>
             ─► deploy: OFF for now (only runs when the repository variable DEPLOY_ENABLED is "true")
```

**Deploying is currently manual:** see [Manual deploy](#manual-deploy). The images CI builds are what you deploy. To
switch CI deploys on later, follow [Enable CI deploys](#enable-ci-deploys-later).

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

## Manual deploy

**One time on the VM** (steps 1 and 3 of [One-time setup](#one-time-setup) below): Caddy, Jaeger and the `edge`
network are running, and `/opt/aviation/.env` is filled in.

**Each deploy** (on the VM, from a clone of the repo):

```sh
cd ~/aviation-telemetry-service && git switch main && git pull
./deploy/deploy.sh pull      # A. images CI built for this commit (GHCR)
./deploy/deploy.sh build     # B. or build the images here on the VM (no registry needed)
```

- **A. `pull [sha]`** deploys the images CI pushed for this commit (or for the given SHA). Wait until that commit's
  workflow run has finished (**Actions** tab), and set the three packages to public once (see step 4 below).
- **B. `build`** builds all three images from this checkout, tagged `local-<short-sha>`, and deploys them.

`deploy.sh` checks everything before it changes anything, and stops with a ✗ and a fix hint if something's missing:

1. **Tools:** docker (and your access to it), `docker compose`, curl.
2. **Infra:** the `edge` network, `/opt/aviation` writable, and (warnings only) Jaeger and Caddy running on `edge`.
3. **`/opt/aviation/.env`:** it exists with mode 600. `MONGODB_URI` and `RABBITMQ_URL` are set and valid. `API_TOKEN`
   and `ADMIN_TOKEN` are at least 16 characters and different. With `OTEL_ENABLED=true`, the OTLP endpoint is set.
   No `<placeholder>` values are left. Values are never printed.
4. **Images:** `pull` checks that all three exist in GHCR; `build` builds them.
5. **Deploy:** copies `docker-compose.prod.yml` and `remote-deploy.sh` to `/opt/aviation` and runs
   `remote-deploy.sh`. That starts the services and waits for their healthchecks. If they don't become healthy, it
   goes back to the previous version and exits with an error.
6. **Verify** `https://api.sathishkottravel.com`: `/health` returns 200, and GraphQL returns 200 with `API_TOKEN` and
   401 without it.

Then look for `flight-telemetry-api`, `-worker` and `-producer` in the Jaeger UI. Use `API_DOMAIN=...` or `DEST=...`
to override the domain or the folder.

**Roll back** to an earlier version: `./deploy/deploy.sh pull <earlier-sha>`. Or, for any tag still on the VM
(`docker images | grep aviation`): `cd /opt/aviation && PULL_POLICY=missing ./remote-deploy.sh <tag>`.

## One-time setup

### 1. Caddy, Jaeger and the `edge` network
Follow [`infra/README.md`](infra/README.md):
1. Point your Caddyfile's upstreams at containers (`jaeger:16686`, `aviation-api:8000`, `host.docker.internal:PORT`),
   and add the `api.sathishkottravel.com` block and its DNS record.
2. Run `sudo ./setup-infra.sh`. It also creates the `edge` network, which the backend deploy needs.

`https://api.sathishkottravel.com` returns 502 until the first backend deploy. That's expected.

### 2. Deploy user and folder
For manual deploys, `/opt/aviation` only needs to be writable by the user you deploy as
(`sudo install -d -o $USER /opt/aviation`), and that user needs to be in the `docker` group. The dedicated `deploy`
user below is for CI deploys.
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

### 4. GitHub packages
After the first push to `main` has built the images: **your profile → Packages →** `aviation-telemetry-service/api`,
`/worker`, `/producer` **→ Package settings → Change visibility → Public**, so the VM can pull without credentials.
This is only needed for manual deploy option A, and for CI deploys.

## Enable CI deploys (later)

- Create the `deploy` user (step 2).
- **Settings → Secrets and variables → Actions → Variables → New variable `DEPLOY_ENABLED` = `true`**. From then on,
  every push to `main` deploys.
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

## Operations

| Task | How |
| ---- | --- |
| Deploy | [Manual deploy](#manual-deploy). With CI deploys enabled: push or merge to `main` |
| Roll back / redeploy a version | `./deploy/deploy.sh pull <earlier-sha>` on the VM. With CI deploys enabled: **Actions → deploy → Run workflow** on `main` with `image_tag` = an earlier commit SHA |
| Status | `cd /opt/aviation && IMAGE_TAG=$(cat .deployed-tag) docker compose -f docker-compose.prod.yml ps` |
| Logs | `... docker compose -f docker-compose.prod.yml logs -f api` (logs rotate at 3 × 10 MB per container) |
| Change settings | edit `/opt/aviation/.env`, then re-run the latest deploy (or `up -d` with `IMAGE_TAG=$(cat .deployed-tag)`) |
| Disk | each deploy removes this project's images except the current and previous tag |

A failed deploy (a service not healthy within 180 s) prints the container state and logs in the job output and puts the
previous version back; the job fails so you notice. Oracle may reclaim Always Free instances that stay idle for a week;
continuous ADS-B polling usually keeps the VM busy enough, but check Oracle's current policy.
