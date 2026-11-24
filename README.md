# StayFlow

White-label transient / hotel booking with a grounded AI concierge. See `CLAUDE.md` for the architecture.

```bash
uv sync
STAYFLOW_ADMIN_PASSWORD='choose-a-strong-one' uv run python seed_client.py seeds/demo.json
DEFAULT_TENANT=demo uv run python main.py          # http://localhost:5001  (owner: /admin)
# or per-subdomain locally: BASE_DOMAIN=localhost → http://demo.localhost:5001
```

Deploy (EC2): `cp .env.example .env`, fill it in, point `yourdomain.com` and `*.yourdomain.com` at the
instance, then `docker compose up -d --build` and seed inside the container:
`docker compose exec -e STAYFLOW_ADMIN_PASSWORD=... app python seed_client.py seeds/demo.json`.
