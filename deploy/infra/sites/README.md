# Project sites

Every `*.caddy` file in this folder is imported by the shared `Caddyfile`. Put one site block per project here,
then reload Caddy:

```sh
docker compose exec caddy caddy reload --config /etc/caddy/Caddyfile
```

For the aviation backend, copy [`deploy/caddy/api.caddy`](../../caddy/api.caddy) into this folder.
