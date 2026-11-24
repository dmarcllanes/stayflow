# StayFlow

White-label transient / hotel booking with agentic AI (guest Booking Agent + owner Ops Agent). See `CLAUDE.md` for the architecture.

```bash
uv sync
STAYFLOW_ADMIN_PASSWORD='choose-a-strong-one' uv run python seed_client.py seeds/demo.json
DEFAULT_TENANT=demo uv run python main.py          # http://localhost:5001  (owner: /admin)
# or per-subdomain locally: BASE_DOMAIN=localhost → http://demo.localhost:5001
```

Deploy (EC2): `cp .env.example .env`, fill it in, point `yourdomain.com` and `*.yourdomain.com` at the
instance, then `docker compose up -d --build` and seed inside the container:
`docker compose exec -e STAYFLOW_ADMIN_PASSWORD=... app python seed_client.py seeds/demo.json`.

## AI agents

Set `LLM_BASE_URL`, `LLM_API_KEY` and `LLM_MODEL` (any OpenAI-compatible endpoint whose model supports
**tool calling**, e.g. Groq `openai/gpt-oss-20b`). Without them the site works and the chat says AI isn't set up.

- **Booking Agent** (guest chat): checks live availability, quotes exact totals, prepares a stay request
  and looks up a request's status by reference + phone. The request is only created when the guest taps
  **Send request** on the card, and it still lands in the owner's audit queue as `pending_audit`.
- **Ops Agent** (admin chat): proposes approve / reject / cancel / reopen and room rate/status changes.
  Each appears as a card and runs only when the owner taps **Confirm**; the dashboard refreshes itself.

Agents never write to the database directly — confirmed actions go through the same validated `db.py`
functions as the forms (overlap checks, status rules, capacity, dates).

## Auto-deploy

Every push to `master` deploys to the server via `.github/workflows/deploy.yml`: GitHub Actions SSHes in
with a deploy-only key (repo secrets `DEPLOY_SSH_KEY`, `DEPLOY_HOST`, `DEPLOY_KNOWN_HOSTS`). On the server
that key is restricted in `~/.ssh/authorized_keys` to one command: `git pull --ff-only && bash deploy.sh`,
which rebuilds the containers and waits for the health check. Data in `data/` and `.env` are untouched.
Re-run a deploy from the repo's **Actions** tab, or by hand on the server: `cd ~/stayflow && git pull && ./deploy.sh`.
