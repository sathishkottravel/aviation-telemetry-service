# Project sites

Every `*.caddy` file in this folder is pasted into the end of the shared `Caddyfile` (`import sites/*.caddy`).
For the aviation backend, copy [`deploy/caddy/api.caddy`](../../caddy/api.caddy) here.

Rules, all checked by `setup-infra.sh` before it changes anything:

- **Site blocks and snippets `(name) { … }` only.** No global options block (`{ … }` without a hostname). Global
  options such as `email you@example.com` go in `/opt/infra/global.caddy`, without braces.
- **No `jaeger.*` / `otel.*` blocks.** The shared `Caddyfile` already serves the Jaeger UI and OTLP ingest.
- **Each hostname in one file only.**
- **Upstreams on the VM itself:** use `host.docker.internal:<port>`, not `localhost:<port>`.

Apply changes by re-running `sudo ./setup-infra.sh`, which validates first and then reloads. Or reload directly:

```sh
cd /opt/infra && docker compose exec caddy caddy reload --config /etc/caddy/Caddyfile
```
