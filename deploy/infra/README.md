# Caddy in Docker on the VM

Runs your **existing** Caddy config (`/etc/caddy`) in a Docker container, in place of the Caddy installed on the host.
Your site files, your Jaeger and `otel.sathishkottravel.com` stay exactly as they are.

- **Host networking:** the container uses `network_mode: host`, so `localhost` inside it is the VM, and every
  `reverse_proxy localhost:PORT` keeps working.
- **Certificates:** it mounts the host Caddy's data directory, so the existing certificates are reused.

This is set up once by hand; the backend's GitHub workflow never touches it.

## Steps

**1. Add the API site to your Caddyfile** (`/etc/caddy/Caddyfile`, or any file it imports):

```
api.sathishkottravel.com {
	reverse_proxy localhost:8000
}
```

This is the same as `deploy/caddy/api.caddy`. The backend's `api` container listens on `127.0.0.1:8000`. Also add a DNS
`A` record for `api.sathishkottravel.com` pointing to the VM.

**2. Switch to the container:**

```sh
git clone --branch feature/vm-deploy https://github.com/sathishkottravel/aviation-telemetry-service.git ~/aviation-telemetry-service
cd ~/aviation-telemetry-service/deploy/infra
sudo ./setup-caddy.sh
```

The script:
1. Installs Docker Compose v2 if it's missing.
2. Validates `/etc/caddy/Caddyfile` inside the container **before changing anything**.
3. Stops and disables the host Caddy (systemd).
4. Starts the container and checks that port 80 answers.

Keep this folder: the container is managed from here (`docker compose ...`).

**3. If anything is wrong,** go back to the host Caddy:

```sh
sudo ./setup-caddy.sh --rollback
```

## Day to day

| Task | Command (in this folder) |
| ---- | ------------------------ |
| Reload after editing `/etc/caddy/Caddyfile` | `docker compose exec caddy caddy reload --config /etc/caddy/Caddyfile` |
| Logs | `docker compose logs -f` |
| Update Caddy | `docker compose pull && docker compose up -d` |
| Remove the host Caddy package for good | `sudo apt remove caddy` (once you're happy) |

## Notes

- **Files outside `/etc/caddy`:** if your Caddyfile uses other host paths (e.g. `root * /var/www/site`, log files),
  add them as volume lines in `docker-compose.yml`.
- **Environment variables** (`{$VAR}` in your Caddyfile): if the old systemd unit set them (see
  `systemctl cat caddy`), put them in `caddy.env` in this folder. It's picked up automatically.
- **A different certificates path:** if your host Caddy stored its data somewhere other than
  `/var/lib/caddy/.local/share/caddy`, run with `CADDY_DATA_DIR=<path> sudo -E ./setup-caddy.sh`.

## Troubleshooting

**`Unable to locate package docker-compose-plugin`**: on Ubuntu's own Docker (`docker.io`), the package is called
`docker-compose-v2`. The script installs it; by hand, run `sudo apt install -y docker-compose-v2`.
