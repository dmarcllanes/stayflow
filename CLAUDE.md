# CLAUDE.md — StayFlow (White-Label Transient & Hotel Booking + AI Concierge)

## Project Overview
StayFlow is a single-codebase, multi-tenant web application for local transient houses, staycation condos, boutique hotels, and small resorts.
It unifies two views inside one Python FastHTML application backed by per-tenant SQLite databases:
1. **Public Guest Storefront (`/`)**: Mobile-first room catalog, 24/7 AI Room Concierge (strictly grounded in live DB rows), and manual stay request submission (`pending_audit`).
2. **Owner Admin Dashboard (`/admin`)**: Session-protected owner command center to manage room rates/statuses, audit incoming bookings with automatic SQL date-overlap conflict checks, approve stays, and generate copy-pasteable GCash/bank downpayment slips.

## Tech Stack & Environment (`uv`)
- **Package & Runtime Manager**: `uv` (Python 3.12+). Always use `uv run`, `uv add`, and `uv sync`. Never use `pip`, `poetry`, or `conda`.
- **Web Framework**: `python-fasthtml` + HTMX + Tailwind CSS (via CDN for zero Node/Vite build step).
- **Database**: Built-in `sqlite3` with WAL mode (`PRAGMA journal_mode=WAL;`) and `PRAGMA foreign_keys=ON;`. No RDS, Postgres, or DynamoDB.
- **LLM Client**: `openai` SDK pointing to an OpenAI-compatible endpoint via env vars (`LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`).
- **Deployment Target**: Single AWS Free Tier EC2 (`t3.micro` / `t2.micro`, 1 GB RAM) via Docker Compose + Caddy reverse proxy. Keep total container RAM usage under 150 MB.

## Directory Structure
```text
.
├── pyproject.toml           # Managed by uv
├── uv.lock
├── Dockerfile
├── docker-compose.yml       # Mounts ./data:/app/data for SQLite persistence
├── Caddyfile                # Wildcard HTTPS routing (*.yourdomain.com)
├── main.py                  # FastHTML routes (/, /chat/guest, /book, /admin/*)
├── db.py                    # Multi-tenant SQLite connection, schema init, overlap queries
├── ai.py                    # Grounded AI Room Concierge & Owner Ops Co-Pilot prompts
├── seed_client.py           # CLI script: `uv run python seed_client.py seeds/demo.json`
├── seeds/
│   └── demo.json            # Sample transient property seed data
└── data/                    # Git-ignored directory holding <tenant_slug>.db files