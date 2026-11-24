"""StayFlow — white-label transient / hotel booking + AI concierge (FastHTML).

Routes
  Guest:  GET /            storefront (rooms, request form, concierge chat)
          POST /book       submit a pending_audit stay request
          POST /chat/guest grounded Room Concierge
  Owner:  GET/POST /admin/login, POST /admin/logout
          GET /admin       dashboard (audit queue, rooms, co-pilot)
          POST /admin/bookings/{id}/approve|reject|cancel
          GET  /admin/bookings/{id}/slip   copy-paste downpayment slip
          POST /admin/rooms, POST /admin/rooms/{id}
          POST /admin/chat Ops Co-Pilot
  Infra:  GET /healthz, GET /_tls_check?domain=  (Caddy on-demand TLS "ask")

Tenant = first label of the Host header under $BASE_DOMAIN (e.g. demo.example.com → "demo"),
or $DEFAULT_TENANT for bare hosts such as localhost.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from datetime import date
from urllib.parse import quote as urlquote

from fasthtml.common import *  # noqa: F403  (FT components, fast_app, serve, ...)
from starlette.responses import PlainTextResponse, RedirectResponse, Response

import ai
import db

BASE_DOMAIN = os.environ.get("BASE_DOMAIN", "").lower().strip(".")
DEFAULT_TENANT = os.environ.get("DEFAULT_TENANT", "").lower()
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "0") == "1"
db.DATA_DIR.mkdir(parents=True, exist_ok=True)

PUBLIC_PATHS = ("/healthz", "/_tls_check", "/favicon.ico")


# --------------------------------------------------------------------------- #
# Tenancy + auth beforeware
# --------------------------------------------------------------------------- #

def tenant_from_host(host: str) -> str | None:
    host = host.split(":")[0].lower().strip(".")
    if BASE_DOMAIN and host.endswith("." + BASE_DOMAIN):
        sub = host[: -len(BASE_DOMAIN) - 1]
        return sub if db.valid_slug(sub) else None
    if DEFAULT_TENANT:
        return DEFAULT_TENANT
    # Dev convenience: with exactly one tenant DB on disk, bare hosts (localhost) use it.
    only = [p.stem for p in db.DATA_DIR.glob("*.db")]
    return only[0] if len(only) == 1 else None


def _not_found_msg(host: str, tenant: str | None) -> str:
    if tenant:
        return f"Property not found: no tenant database '{tenant}.db' in {db.DATA_DIR}."
    tenants = sorted(p.stem for p in db.DATA_DIR.glob("*.db"))
    if not tenants:
        return ("Property not found: no tenants yet. Seed one with:\n"
                "  uv run python seed_client.py seeds/demo.json")
    return (f"Property not found for host '{host}'. Existing tenants: {', '.join(tenants)}.\n"
            "Set DEFAULT_TENANT=<slug>, or BASE_DOMAIN and visit <slug>.<BASE_DOMAIN>.")


def _redirect(req, url: str):
    if req.headers.get("hx-request"):
        return Response(status_code=204, headers={"HX-Redirect": url})
    return RedirectResponse(url, status_code=303)


def before(req, sess):
    path = req.url.path
    if path in PUBLIC_PATHS:
        return None
    host = req.headers.get("host", "")
    tenant = tenant_from_host(host)
    if not db.tenant_exists(tenant):
        return Response(_not_found_msg(host, tenant), status_code=404, media_type="text/plain")
    req.scope["tenant"] = tenant
    if path.startswith("/admin") and path != "/admin/login" and sess.get("admin") != tenant:
        return _redirect(req, "/admin/login")
    return None


app, rt = fast_app(
    pico=False,
    hdrs=(
        Meta(name="viewport", content="width=device-width, initial-scale=1, viewport-fit=cover"),
        Link(rel="preconnect", href="https://fonts.googleapis.com"),
        Link(rel="preconnect", href="https://fonts.gstatic.com", crossorigin=""),
        Link(rel="stylesheet",
             href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap"),
        Script(src="https://cdn.tailwindcss.com"),
        Script("tailwind.config={theme:{extend:{fontFamily:{sans:['\"Plus Jakarta Sans\"','ui-sans-serif',"
               "'system-ui','sans-serif']}}}}"),
    ),
    before=Beforeware(before),
    secret_key=os.environ.get("SESSION_SECRET") or None,
    key_fname=str(db.DATA_DIR / ".sesskey"),
    sess_https_only=COOKIE_SECURE,
    session_cookie="stayflow_session",
    max_age=7 * 24 * 3600,
    title="StayFlow",
)


def tenant(req) -> str:
    return req.scope["tenant"]


def chat_id(sess) -> str:
    if "chat_id" not in sess:
        sess["chat_id"] = secrets.token_urlsafe(12)
    return sess["chat_id"]


# --------------------------------------------------------------------------- #
# Shared UI bits
# --------------------------------------------------------------------------- #

def money(prop, amount: int) -> str:
    sym = "₱" if prop["currency"] == "PHP" else prop["currency"] + " "
    return f"{sym}{amount:,}"


def nice_date(iso: str) -> str:
    return date.fromisoformat(iso).strftime("%a %d %b %Y")


BTN = ("inline-flex items-center justify-center gap-1.5 rounded-full px-5 py-2.5 text-sm font-semibold "
       "transition active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-50")
BTN_PRIMARY = BTN + " bg-slate-900 text-white shadow-lg shadow-slate-900/15 hover:bg-slate-800"
BTN_ACCENT = BTN + " bg-teal-600 text-white shadow-lg shadow-teal-600/25 hover:bg-teal-700"
BTN_GHOST = BTN + " bg-white text-slate-800 ring-1 ring-slate-900/10 hover:bg-stone-50 hover:ring-slate-900/20"
BTN_DANGER = BTN + " border border-rose-200 bg-white text-rose-700 hover:bg-rose-50"
INPUT = ("w-full min-h-[3rem] rounded-2xl border border-stone-200 bg-stone-50/60 px-4 py-2.5 text-base text-slate-900 sm:text-sm "
         "placeholder:text-slate-400 transition focus:border-teal-500 focus:outline-none focus:ring-4 "
         "focus:ring-teal-500/15")
CARD = "rounded-[1.75rem] bg-white p-6 shadow-[0_8px_30px_-12px_rgba(15,23,42,0.12)] ring-1 ring-stone-200/70"


def Icon(name: str, cls: str = "h-5 w-5"):
    """Inline heroicons-style outline SVGs (no icon font / extra request)."""
    paths = {
        "chat": "M8.625 12a.375.375 0 1 1-.75 0 .375.375 0 0 1 .75 0Zm4.125 0a.375.375 0 1 1-.75 0 .375.375 0 0 1 .75 0Zm4.125 0a.375.375 0 1 1-.75 0 .375.375 0 0 1 .75 0ZM2.25 12.76c0 1.6 1.123 2.994 2.707 3.227 1.087.16 2.185.283 3.293.369V21l4.184-4.183a1.14 1.14 0 0 1 .778-.332 48.294 48.294 0 0 0 5.83-.498c1.585-.233 2.708-1.626 2.708-3.228V6.741c0-1.602-1.123-2.995-2.707-3.228A48.394 48.394 0 0 0 12 3c-2.392 0-4.744.175-7.043.513C3.373 3.746 2.25 5.14 2.25 6.741v6.018Z",
        "x": "M6 18 18 6M6 6l12 12",
        "chevron-down": "m19.5 8.25-7.5 7.5-7.5-7.5",
        "send": "M6 12 3.269 3.125A59.769 59.769 0 0 1 21.485 12 59.768 59.768 0 0 1 3.27 20.875L5.999 12Zm0 0h7.5",
        "sparkles": "M9.813 15.904 9 18.75l-.813-2.846a4.5 4.5 0 0 0-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 0 0 3.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 0 0 3.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 0 0-3.09 3.09ZM18.259 8.715 18 9.75l-.259-1.035a3.375 3.375 0 0 0-2.455-2.456L14.25 6l1.036-.259a3.375 3.375 0 0 0 2.455-2.456L18 2.25l.259 1.035a3.375 3.375 0 0 0 2.456 2.456L21.75 6l-1.035.259a3.375 3.375 0 0 0-2.456 2.456Z",
        "pin": "M15 10.5a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z M19.5 10.5c0 7.142-7.5 11.25-7.5 11.25S4.5 17.642 4.5 10.5a7.5 7.5 0 1 1 15 0Z",
        "users": "M15 19.128a9.38 9.38 0 0 0 2.625.372 9.337 9.337 0 0 0 4.121-.952 4.125 4.125 0 0 0-7.533-2.493M15 19.128v-.003c0-1.113-.285-2.16-.786-3.07M15 19.128v.106A12.318 12.318 0 0 1 8.624 21c-2.331 0-4.512-.645-6.374-1.766l-.001-.109a6.375 6.375 0 0 1 11.964-3.07M12 6.375a3.375 3.375 0 1 1-6.75 0 3.375 3.375 0 0 1 6.75 0Zm8.25 2.25a2.625 2.625 0 1 1-5.25 0 2.625 2.625 0 0 1 5.25 0Z",
        "clock": "M12 6v6h4.5m4.5 0a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z",
        "wallet": "M21 12a2.25 2.25 0 0 0-2.25-2.25H15a3 3 0 1 1-6 0H5.25A2.25 2.25 0 0 0 3 12m18 0v6a2.25 2.25 0 0 1-2.25 2.25H5.25A2.25 2.25 0 0 1 3 18v-6m18 0V9M3 12V9m18 0a2.25 2.25 0 0 0-2.25-2.25H5.25A2.25 2.25 0 0 0 3 9m18 0V6a2.25 2.25 0 0 0-2.25-2.25H5.25A2.25 2.25 0 0 0 3 6v3",
        "phone": "M2.25 6.75c0 8.284 6.716 15 15 15h2.25a2.25 2.25 0 0 0 2.25-2.25v-1.372c0-.516-.351-.966-.852-1.091l-4.423-1.106c-.44-.11-.902.055-1.173.417l-.97 1.293c-.282.376-.769.542-1.21.38a12.035 12.035 0 0 1-7.143-7.143c-.162-.441.004-.928.38-1.21l1.293-.97c.363-.271.527-.734.417-1.173L6.963 3.102a1.125 1.125 0 0 0-1.091-.852H4.5A2.25 2.25 0 0 0 2.25 4.5v2.25Z",
        "home": "m2.25 12 8.954-8.955c.44-.439 1.152-.439 1.591 0L21.75 12M4.5 9.75v10.125c0 .621.504 1.125 1.125 1.125H9.75v-4.875c0-.621.504-1.125 1.125-1.125h2.25c.621 0 1.125.504 1.125 1.125V21h4.125c.621 0 1.125-.504 1.125-1.125V9.75M8.25 21h8.25",
        "check": "m4.5 12.75 6 6 9-13.5",
        "shield": "M9 12.75 11.25 15 15 9.75m-3-7.036A11.959 11.959 0 0 1 3.598 6 11.99 11.99 0 0 0 3 9.749c0 5.592 3.824 10.29 9 11.623 5.176-1.332 9-6.03 9-11.622 0-1.31-.21-2.571-.598-3.751h-.152c-3.196 0-6.1-1.248-8.25-3.285Z",
        "sun": "M12 3v2.25m6.364.386-1.591 1.591M21 12h-2.25m-.386 6.364-1.591-1.591M12 18.75V21m-4.773-4.227-1.591 1.591M5.25 12H3m4.227-4.773L5.636 5.636M15.75 12a3.75 3.75 0 1 1-7.5 0 3.75 3.75 0 0 1 7.5 0Z",
        "truck": "M8.25 18.75a1.5 1.5 0 0 1-3 0m3 0a1.5 1.5 0 0 0-3 0m3 0h6m-9 0H3.375a1.125 1.125 0 0 1-1.125-1.125V14.25m17.25 4.5a1.5 1.5 0 0 1-3 0m3 0a1.5 1.5 0 0 0-3 0m3 0h1.125c.621 0 1.129-.504 1.09-1.124a17.902 17.902 0 0 0-3.213-9.193 2.056 2.056 0 0 0-1.58-.86H14.25M16.5 18.75h-2.25m0-11.177v-.958c0-.568-.422-1.048-.987-1.106a48.554 48.554 0 0 0-10.026 0 1.106 1.106 0 0 0-.987 1.106v7.635m12-6.677v6.677m0 4.5v-4.5m0 0h-12",
        "cake": "M12 8.25v-1.5m0 1.5c-1.355 0-2.697.056-4.024.166C6.845 8.51 6 9.473 6 10.608v2.513m6-4.871c1.355 0 2.697.056 4.024.166C17.155 8.51 18 9.473 18 10.608v2.513M15 8.25v-1.5m-6 1.5v-1.5m12 9.75-1.5.75a3.354 3.354 0 0 1-3 0 3.354 3.354 0 0 0-3 0 3.354 3.354 0 0 1-3 0 3.354 3.354 0 0 0-3 0 3.354 3.354 0 0 1-3 0L3 16.5m15-3.379a48.474 48.474 0 0 0-6-.371c-2.032 0-4.034.126-6 .371m12 0c.39.049.777.102 1.163.16 1.07.16 1.837 1.094 1.837 2.175v5.169c0 .621-.504 1.125-1.125 1.125H4.125A1.125 1.125 0 0 1 3 20.625v-5.17c0-1.08.768-2.014 1.837-2.174A47.78 47.78 0 0 1 6 13.12",
        "map": "M9 6.75V15m6-6v8.25m.503 3.498 4.875-2.437c.381-.19.622-.58.622-1.006V4.82c0-.836-.88-1.38-1.628-1.006l-3.869 1.934c-.317.159-.69.159-1.006 0L9.503 3.252a1.125 1.125 0 0 0-1.006 0L3.622 5.689C3.24 5.88 3 6.27 3 6.695V19.18c0 .836.88 1.38 1.628 1.006l3.869-1.934c.317-.159.69-.159 1.006 0l4.994 2.497c.317.158.69.158 1.006 0Z",
        "gift": "M21 11.25v8.25a1.5 1.5 0 0 1-1.5 1.5H5.25a1.5 1.5 0 0 1-1.5-1.5v-8.25M12 4.875A2.625 2.625 0 1 0 9.375 7.5H12m0-2.625V7.5m0-2.625A2.625 2.625 0 1 1 14.625 7.5H12m0 0V21m-8.625-9.75h18c.621 0 1.125-.504 1.125-1.125v-1.5c0-.621-.504-1.125-1.125-1.125h-18c-.621 0-1.125.504-1.125 1.125v1.5c0 .621.504 1.125 1.125 1.125Z",
        "mail": "M21.75 6.75v10.5a2.25 2.25 0 0 1-2.25 2.25h-15a2.25 2.25 0 0 1-2.25-2.25V6.75m19.5 0A2.25 2.25 0 0 0 19.5 4.5h-15a2.25 2.25 0 0 0-2.25 2.25m19.5 0v.243a2.25 2.25 0 0 1-1.07 1.916l-7.5 4.615a2.25 2.25 0 0 1-2.36 0L3.32 8.91a2.25 2.25 0 0 1-1.07-1.916V6.75",
        "calendar": "M6.75 3v2.25M17.25 3v2.25M3 18.75V7.5a2.25 2.25 0 0 1 2.25-2.25h13.5A2.25 2.25 0 0 1 21 7.5v11.25m-18 0A2.25 2.25 0 0 0 5.25 21h13.5A2.25 2.25 0 0 0 21 18.75m-18 0v-7.5A2.25 2.25 0 0 1 5.25 9h13.5A2.25 2.25 0 0 1 21 11.25v7.5",
        "menu": "M3.75 6.75h16.5M3.75 12h16.5m-16.5 5.25h16.5",
        "arrow-right": "M13.5 4.5 21 12m0 0-7.5 7.5M21 12H3",
        "globe": "M12 21a9.004 9.004 0 0 0 8.716-6.747M12 21a9.004 9.004 0 0 1-8.716-6.747M12 21c2.485 0 4.5-4.03 4.5-9S14.485 3 12 3m0 18c-2.485 0-4.5-4.03-4.5-9S9.515 3 12 3m0 0a8.997 8.997 0 0 1 7.843 4.582M12 3a8.997 8.997 0 0 0-7.843 4.582m15.686 0A11.953 11.953 0 0 1 12 10.5c-2.998 0-5.74-1.1-7.843-2.918m15.686 0A8.959 8.959 0 0 1 21 12c0 .778-.099 1.533-.284 2.253m0 0A17.919 17.919 0 0 1 12 16.5c-3.162 0-6.133-.815-8.716-2.247m0 0A9.015 9.015 0 0 1 3 12c0-1.605.42-3.113 1.157-4.418",
        "plus": "M12 4.5v15m7.5-7.5h-15",
        "check-circle": "M9 12.75 11.25 15 15 9.75M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z",
        "heart": "M21 8.25c0-2.485-2.099-4.5-4.688-4.5-1.935 0-3.597 1.126-4.312 2.733-.715-1.607-2.377-2.733-4.313-2.733C5.1 3.75 3 5.765 3 8.25c0 7.22 9 12 9 12s9-4.78 9-12Z",
        "search": "m21 21-5.197-5.197m0 0A7.5 7.5 0 1 0 5.196 5.196a7.5 7.5 0 0 0 10.607 10.607Z",
        "trash": "m14.74 9-.346 9m-4.788 0L9.26 9m9.968-3.21c.342.052.682.107 1.022.166m-1.022-.165L18.16 19.673a2.25 2.25 0 0 1-2.244 2.077H8.084a2.25 2.25 0 0 1-2.244-2.077L4.772 5.79m14.456 0a48.108 48.108 0 0 0-3.478-.397m-12 .562c.34-.059.68-.114 1.022-.165m0 0a48.11 48.11 0 0 1 3.478-.397m7.5 0v-.916c0-1.18-.91-2.164-2.09-2.201a51.964 51.964 0 0 0-3.32 0c-1.18.037-2.09 1.022-2.09 2.201v.916m7.5 0a48.667 48.667 0 0 0-7.5 0",
        "pencil": "m16.862 4.487 1.687-1.688a1.875 1.875 0 1 1 2.652 2.652L10.582 16.07a4.5 4.5 0 0 1-1.897 1.13L6 18l.8-2.685a4.5 4.5 0 0 1 1.13-1.897l8.932-8.931Zm0 0L19.5 7.125M18 14v4.75A2.25 2.25 0 0 1 15.75 21H5.25A2.25 2.25 0 0 1 3 18.75V8.25A2.25 2.25 0 0 1 5.25 6H10",
        "inbox": "M2.25 13.5h3.86a2.25 2.25 0 0 1 2.012 1.244l.256.512a2.25 2.25 0 0 0 2.013 1.244h3.218a2.25 2.25 0 0 0 2.013-1.244l.256-.512a2.25 2.25 0 0 1 2.013-1.244h3.859m-19.5.338V18a2.25 2.25 0 0 0 2.25 2.25h15A2.25 2.25 0 0 0 21.75 18v-4.162c0-.224-.034-.447-.1-.661L19.24 5.338a2.25 2.25 0 0 0-2.15-1.588H6.911a2.25 2.25 0 0 0-2.15 1.588L2.35 13.177a2.25 2.25 0 0 0-.1.661Z",
        "logout": "M15.75 9V5.25A2.25 2.25 0 0 0 13.5 3h-6a2.25 2.25 0 0 0-2.25 2.25v13.5A2.25 2.25 0 0 0 7.5 21h6a2.25 2.25 0 0 0 2.25-2.25V15m3 0 3-3m0 0-3-3m3 3H9",
        "undo": "M9 15 3 9m0 0 6-6M3 9h12a6 6 0 0 1 0 12h-3",
        "document": "M19.5 14.25v-2.625a3.375 3.375 0 0 0-3.375-3.375h-1.5A1.125 1.125 0 0 1 13.5 7.125v-1.5a3.375 3.375 0 0 0-3.375-3.375H8.25m0 12.75h7.5m-7.5 3H12M10.5 2.25H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 0 0-9-9Z",
        "camera": "M6.827 6.175A2.31 2.31 0 0 1 5.186 7.23c-.38.054-.757.112-1.134.175C2.999 7.58 2.25 8.507 2.25 9.574V18a2.25 2.25 0 0 0 2.25 2.25h15A2.25 2.25 0 0 0 21.75 18V9.574c0-1.067-.75-1.994-1.802-2.169a47.865 47.865 0 0 0-1.134-.175 2.31 2.31 0 0 1-1.64-1.055l-.822-1.316a2.192 2.192 0 0 0-1.736-1.039 48.774 48.774 0 0 0-5.232 0 2.192 2.192 0 0 0-1.736 1.039l-.821 1.316Z M16.5 12.75a4.5 4.5 0 1 1-9 0 4.5 4.5 0 0 1 9 0Z",
    }
    d = paths.get(name, paths["sparkles"])  # unknown names (e.g. from seed data) fall back gracefully
    return NotStr(f'<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="1.6" '
                  f'stroke="currentColor" class="{cls}" aria-hidden="true"><path stroke-linecap="round" '
                  f'stroke-linejoin="round" d="{d}"/></svg>')


# Official brand glyphs (filled, 24x24) and their brand backgrounds.
_BRANDS = {
    "facebook": ("M14 13.5h2.5l1-4H14v-2c0-1.03 0-2 2-2h1.5V2.14c-.326-.043-1.557-.14-2.857-.14C11.928 2 10 3.657 10 6.7v2.8H7v4h3V22h4v-8.5Z",
                 "#1877F2"),
    "instagram": ("M7.8 2h8.4C19.4 2 22 4.6 22 7.8v8.4a5.8 5.8 0 0 1-5.8 5.8H7.8C4.6 22 2 19.4 2 16.2V7.8A5.8 5.8 0 0 1 7.8 2m-.2 2A3.6 3.6 0 0 0 4 7.6v8.8C4 18.39 5.61 20 7.6 20h8.8a3.6 3.6 0 0 0 3.6-3.6V7.6C20 5.61 18.39 4 16.4 4H7.6m9.65 1.5a1.25 1.25 0 0 1 1.25 1.25A1.25 1.25 0 0 1 17.25 8 1.25 1.25 0 0 1 16 6.75a1.25 1.25 0 0 1 1.25-1.25M12 7a5 5 0 0 1 5 5 5 5 0 0 1-5 5 5 5 0 0 1-5-5 5 5 0 0 1 5-5m0 2a3 3 0 0 0-3 3 3 3 0 0 0 3 3 3 3 0 0 0 3-3 3 3 0 0 0-3-3Z",
                  "radial-gradient(circle at 30% 107%,#fdf497 0%,#fdf497 5%,#fd5949 45%,#d6249f 60%,#285AEB 90%)"),
    "messenger": ("M12 2C6.36 2 2 6.13 2 11.7c0 2.91 1.19 5.44 3.14 7.18.16.12.26.34.27.56l.05 1.78c.04.57.61.94 1.13.71l1.98-.87c.17-.06.36-.09.53-.06.9.27 1.9.4 2.9.4 5.64 0 10-4.13 10-9.7S17.64 2 12 2m6 7.46-2.93 4.67c-.47.73-1.47.92-2.17.37l-2.34-1.73a.6.6 0 0 0-.72 0l-3.16 2.4c-.42.33-.97-.17-.68-.63l2.93-4.67c.47-.73 1.47-.92 2.17-.4l2.34 1.76c.21.16.51.16.72 0l3.16-2.4c.42-.33.97.17.68.63Z",
                  "linear-gradient(45deg,#0099FF 0%,#A033FF 60%,#FF5280 90%,#FF7061 100%)"),
}


def BrandIcon(name: str, cls: str = "h-5 w-5"):
    return NotStr(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" class="{cls}" '
                  f'aria-hidden="true"><path d="{_BRANDS[name][0]}"/></svg>')


def BrandBadge(name: str, size: str = "h-9 w-9", icon: str = "h-5 w-5"):
    return Span(BrandIcon(name, icon), style=f"background:{_BRANDS[name][1]}",
                cls=f"grid {size} shrink-0 place-items-center rounded-full text-white shadow-sm")


# Colourful icon tiles, cycled so rows of features don't all look the same.
TILE_TONES = ("bg-teal-100 text-teal-700", "bg-amber-100 text-amber-700", "bg-sky-100 text-sky-700",
              "bg-violet-100 text-violet-700", "bg-rose-100 text-rose-700", "bg-emerald-100 text-emerald-700")


def IconTile(name: str, i: int = 0, size: str = "h-11 w-11", icon: str = "h-5 w-5", tone: str | None = None):
    return Span(Icon(name, icon), cls=f"grid {size} shrink-0 place-items-center rounded-2xl {tone or TILE_TONES[i % len(TILE_TONES)]}")


def Field(label, inp):
    return Label(Span(label, cls="mb-1.5 block text-xs font-semibold text-slate-600"), inp, cls="block")


def Badge(text, tone="slate"):
    tones = {
        "slate": "bg-slate-100 text-slate-700 ring-slate-200", "green": "bg-emerald-50 text-emerald-700 ring-emerald-200",
        "amber": "bg-amber-50 text-amber-800 ring-amber-200", "red": "bg-rose-50 text-rose-700 ring-rose-200",
        "teal": "bg-teal-50 text-teal-700 ring-teal-200",
    }
    return Span(text, cls=f"inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium ring-1 ring-inset {tones[tone]}")


STATUS_TONE = {"pending_audit": "amber", "approved": "green", "rejected": "red", "cancelled": "slate",
               "available": "green", "maintenance": "amber", "hidden": "slate"}

GLOBAL_CSS = """
html{scroll-behavior:smooth;-webkit-text-size-adjust:100%}
body{-webkit-font-smoothing:antialiased;overflow-x:clip;-webkit-tap-highlight-color:transparent}
input[type=date]{min-height:1.5em}
input[type=date]::-webkit-date-and-time-value{text-align:left}
html.sf-lock,html.sf-lock body{overflow:hidden}
@media (max-width:639px){
  #chat-launcher[aria-expanded=true]{display:none}
  #chat-panel.flex{animation-name:sf-sheet;transform-origin:bottom center}
}
@keyframes sf-sheet{from{transform:translateY(100%)}to{transform:none}}
#sf-bookbar{transition:transform .25s ease}
#sf-bookbar.sf-away{transform:translateY(120%)}
/* Chat launcher sits above the pinned booking bar while it's visible */
#sf-chat{bottom:calc(1rem + env(safe-area-inset-bottom));transition:bottom .25s ease}
@media (min-width:640px){#sf-chat{bottom:1.5rem}}
@media (max-width:1023px){body:has(#sf-bookbar:not(.sf-away)) #sf-chat{bottom:calc(.9rem + env(safe-area-inset-bottom))}}
/* Scroll reveal (only when JS is running, so content is never stuck hidden) */
html.sf-js .sf-reveal{opacity:0;transform:translateY(18px);transition:opacity .6s ease,transform .6s cubic-bezier(.2,.7,.2,1)}
html.sf-js .sf-reveal.sf-in{opacity:1;transform:none}
/* Top loading bar for page changes */
#sf-progress{position:fixed;top:0;left:0;height:3px;width:0;z-index:100;opacity:0;pointer-events:none;
  background:linear-gradient(90deg,#14b8a6,#06b6d4);transition:width .3s ease,opacity .4s ease .1s}
html.sf-loading #sf-progress{width:80%;opacity:1;transition:width 6s cubic-bezier(.1,.8,.1,1),opacity .1s}
/* Toast */
#sf-toast{position:fixed;left:50%;bottom:calc(6rem + env(safe-area-inset-bottom));z-index:70;transform:translate(-50%,16px);
  opacity:0;pointer-events:none;background:#0f172a;color:#fff;padding:.65rem 1.1rem;border-radius:9999px;font-size:.875rem;
  font-weight:600;box-shadow:0 10px 30px -10px rgba(15,23,42,.5);transition:opacity .25s ease,transform .25s ease}
#sf-toast.sf-show{opacity:1;transform:translate(-50%,0)}
/* Top announcement ticker: content is duplicated, track slides by exactly half its width */
.sf-marquee{mask-image:linear-gradient(90deg,transparent,#000 6%,#000 94%,transparent);
  -webkit-mask-image:linear-gradient(90deg,transparent,#000 6%,#000 94%,transparent)}
.sf-track{display:flex;width:max-content;animation:sf-marquee var(--sf-speed,40s) linear infinite}
.sf-marquee:hover .sf-track,.sf-marquee:focus-within .sf-track{animation-play-state:paused}
@keyframes sf-marquee{from{transform:translateX(0)}to{transform:translateX(-50%)}}
@media (prefers-reduced-motion:reduce){.sf-track{animation:none!important}.sf-marquee{overflow-x:auto}}
/* Frosted-glass chips that sit on photos */
.sf-glass{background:rgba(255,255,255,.18);-webkit-backdrop-filter:blur(14px) saturate(160%);backdrop-filter:blur(14px) saturate(160%);
  border:1px solid rgba(255,255,255,.28)}
.sf-glass-light{background:rgba(255,255,255,.82);-webkit-backdrop-filter:blur(14px);backdrop-filter:blur(14px)}
.sf-fav[aria-pressed=true] svg{fill:#f43f5e;stroke:#f43f5e}
.sf-fav:active{transform:scale(.88)}
/* Button busy state */
.sf-busy{display:none}
form.htmx-request .sf-idle{display:none}
form.htmx-request .sf-busy{display:inline-flex}
@keyframes sf-spin{to{transform:rotate(360deg)}}
.sf-spin{animation:sf-spin .8s linear infinite}
#book-avail.htmx-request{opacity:.6;transition:opacity .2s}
:focus-visible{outline:2px solid #14b8a6;outline-offset:2px}
::view-transition-old(root),::view-transition-new(root){animation-duration:.22s}
@media (prefers-reduced-motion:reduce){
  *,*::before,*::after{animation-duration:.01ms!important;transition-duration:.01ms!important;scroll-behavior:auto!important}
  html.sf-js .sf-reveal{opacity:1;transform:none}
}
@keyframes sf-in{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
@keyframes sf-pop{from{opacity:0;transform:translateY(12px) scale(.97)}to{opacity:1;transform:none}}
@keyframes sf-dot{0%,80%,100%{opacity:.25;transform:translateY(0)}40%{opacity:1;transform:translateY(-3px)}}
.sf-bubble{animation:sf-in .25s ease-out}
#chat-panel.flex{animation:sf-pop .22s ease-out;transform-origin:bottom right}
#chat-typing{display:none}
#chat-typing.htmx-request{display:flex}
.sf-dot{animation:sf-dot 1.2s infinite ease-in-out}
.sf-dot:nth-child(2){animation-delay:.15s}.sf-dot:nth-child(3){animation-delay:.3s}
#chat-launcher[aria-expanded=true] .ic-open,#chat-launcher[aria-expanded=false] .ic-close{display:none}
.sf-scroll::-webkit-scrollbar{width:6px;height:6px}
.sf-scroll::-webkit-scrollbar-thumb{background:#cbd5e1;border-radius:9999px}
.sf-noscroll::-webkit-scrollbar{display:none}.sf-noscroll{scrollbar-width:none}
"""

CHAT_JS = """
function sfChat(open){
  const p=document.getElementById('chat-panel'), b=document.getElementById('chat-launcher');
  const show = open ?? p.classList.contains('hidden');
  p.classList.toggle('hidden',!show); p.classList.toggle('flex',show);
  b.setAttribute('aria-expanded', show);
  document.documentElement.classList.toggle('sf-lock', show && innerWidth < 640);
  const dot=document.getElementById('chat-unread'); if(dot) dot.remove();
  if(show){ const l=document.getElementById('chat-log'); l.scrollTop=l.scrollHeight;
            setTimeout(()=>p.querySelector('input[name=message]').focus(),50); }
}
function sfAsk(btn){ sfSay(btn.textContent); }
function sfSay(text){
  const f=document.getElementById('chat-form'); f.elements.message.value=text; f.requestSubmit();
}
function sfDismiss(btn, focus){
  const c=btn.closest('[data-card]'); if(c) c.remove();
  if(focus) document.querySelector('#chat-form input[name=message]').focus();
}
function sfChatDone(f){
  f.reset(); const s=document.getElementById('chat-suggest'); if(s) s.remove();
  const l=document.getElementById('chat-log'); l.scrollTop=l.scrollHeight;
}
document.addEventListener('keydown',e=>{ if(e.key==='Escape' && document.getElementById('chat-panel')) sfChat(false); });
if(location.hash==='#chat') sfChat(true);  // shareable deep link that opens the concierge
"""


def page(title: str, *content):
    return Title(title), Style(GLOBAL_CSS), Body(*content, cls="flex min-h-screen flex-col bg-[#f6f4ef] font-sans text-slate-900 antialiased")


# --------------------------------------------------------------------------- #
# Floating chat widget (bottom-right corner)
# --------------------------------------------------------------------------- #

def _md(text: str) -> NotStr:
    import html, re as _re
    t = html.escape(text)
    t = _re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
    t = _re.sub(r"(?m)^\s*[-*•]\s+", "• ", t)
    return NotStr(t)


def chat_bubble(role: str, text: str):
    mine = role == "user"
    if mine:
        return Div(Div(text, cls="max-w-[85%] whitespace-pre-wrap rounded-2xl rounded-br-md bg-teal-600 px-3.5 py-2 "
                                 "text-sm text-white shadow-sm"),
                   cls="sf-bubble flex justify-end")
    return Div(
        Div(Icon("sparkles", "h-3.5 w-3.5"),
            cls="mt-0.5 grid h-7 w-7 shrink-0 place-items-center rounded-full bg-gradient-to-br from-teal-500 "
                "to-cyan-600 text-white"),
        Div(_md(text), cls="max-w-[85%] whitespace-pre-wrap rounded-2xl rounded-tl-md bg-white px-3.5 py-2 text-sm "
                      "text-slate-800 shadow-sm ring-1 ring-slate-200/70"),
        cls="sf-bubble flex items-start gap-2",
    )


def chat_widget(post_url: str, title: str, subtitle: str, greeting: str, placeholder: str,
                suggestions: tuple[str, ...] = (), footnote: str = ""):
    chip = ("shrink-0 whitespace-nowrap rounded-full border border-teal-200 bg-white px-3 py-1.5 text-xs "
            "font-medium text-teal-700 transition hover:border-teal-400 hover:bg-teal-50")
    panel = Section(
        Div(
            Div(Div(Icon("sparkles", "h-5 w-5"), cls="grid h-10 w-10 place-items-center rounded-full bg-white/15 ring-1 ring-white/25"),
                Div(P(title, cls="text-sm font-semibold leading-tight"),
                    P(Span(cls="mr-1.5 inline-block h-2 w-2 rounded-full bg-emerald-300"), subtitle,
                      cls="mt-0.5 flex items-center text-xs text-white/80")),
                cls="flex items-center gap-3"),
            Button(Icon("x", "h-6 w-6"), type="button", onclick="sfChat(false)", aria_label="Close chat",
                   cls="grid h-11 w-11 place-items-center rounded-full text-white/80 transition hover:bg-white/15 hover:text-white"),
            cls="flex shrink-0 items-center justify-between bg-gradient-to-br from-teal-600 via-teal-600 to-cyan-700 px-4 "
                "pb-3.5 pt-[max(0.875rem,env(safe-area-inset-top))] text-white sm:pt-3.5",
        ),
        Div(chat_bubble("assistant", greeting), id="chat-log",
            cls="sf-scroll flex flex-1 flex-col gap-3 overflow-y-auto bg-slate-50 px-4 py-4"),
        Div(Div(*[Span(cls="sf-dot h-1.5 w-1.5 rounded-full bg-slate-400") for _ in range(3)],
                cls="flex items-center gap-1 rounded-2xl rounded-tl-md bg-white px-3.5 py-3 shadow-sm ring-1 ring-slate-200/70"),
            id="chat-typing", cls="bg-slate-50 px-4 pb-3 pl-[3.25rem]"),
        Div(*[Button(s, type="button", onclick="sfAsk(this)", cls=chip) for s in suggestions],
            id="chat-suggest", cls="sf-noscroll flex gap-2 overflow-x-auto bg-slate-50 px-4 pb-3") if suggestions else None,
        Form(
            Input(name="message", placeholder=placeholder, required=True, maxlength="1500", autocomplete="off",
                  aria_label="Message",
                  cls="min-w-0 flex-1 rounded-full border border-slate-200 bg-slate-50 px-4 py-2.5 text-base sm:text-sm "
                      "placeholder:text-slate-400 focus:border-teal-500 focus:bg-white focus:outline-none "
                      "focus:ring-4 focus:ring-teal-500/15"),
            Button(Icon("send", "h-4 w-4"), aria_label="Send",
                   cls="grid h-11 w-11 shrink-0 place-items-center rounded-full bg-teal-600 text-white shadow-sm "
                       "transition hover:bg-teal-700 active:scale-95 disabled:opacity-50"),
            id="chat-form", hx_post=post_url, hx_target="#chat-log", hx_swap="beforeend",
            hx_disabled_elt="find button", hx_indicator="#chat-typing",
            **{"hx-on::after-request": "sfChatDone(this)"},
            cls="flex shrink-0 items-center gap-2 border-t border-slate-200 bg-white px-3 py-3",
        ),
        P(footnote, cls="shrink-0 bg-white px-4 pb-[max(0.75rem,env(safe-area-inset-bottom))] text-center text-[11px] "
                        "leading-snug text-slate-400 sm:pb-3") if footnote else None,
        id="chat-panel", role="dialog", aria_label=title,
        cls="fixed inset-0 z-10 hidden h-[100dvh] w-full flex-col overflow-hidden bg-white "
            "sm:static sm:h-[min(36rem,calc(100dvh_-_7.5rem))] sm:w-[24rem] sm:rounded-3xl sm:shadow-2xl "
            "sm:shadow-slate-900/20 sm:ring-1 sm:ring-slate-200",
    )
    launcher = Button(
        Span(Icon("chat", "h-7 w-7"), cls="ic-open"),
        Span(Icon("chevron-down", "h-7 w-7"), cls="ic-close"),
        Span(cls="absolute right-0.5 top-0.5 h-3.5 w-3.5 rounded-full bg-rose-500 ring-2 ring-white", id="chat-unread"),
        id="chat-launcher", type="button", onclick="sfChat()", aria_label=f"Open {title}", aria_expanded="false",
        aria_controls="chat-panel",
        cls="relative grid h-14 w-14 place-items-center rounded-full bg-gradient-to-br from-teal-500 to-cyan-600 "
            "text-white shadow-xl shadow-teal-700/30 ring-4 ring-white transition hover:scale-105 active:scale-95",
    )
    # Vertical position lives in GLOBAL_CSS (#sf-chat) so it can follow the mobile booking bar.
    # hx-preserve keeps the open conversation alive while the visitor moves between pages.
    return Div(panel, launcher, Script(CHAT_JS), id="sf-chat", hx_preserve="true",
               cls="fixed right-4 z-50 flex flex-col items-end gap-3 sm:right-6")


# --------------------------------------------------------------------------- #
# Guest storefront
# --------------------------------------------------------------------------- #

DEFAULT_GALLERY = ("Exterior", "Beachfront", "Lounge & dining", "Bedroom", "Sunset view", "Shared kitchen")


def placeholder_img(key: str, w: int = 1200, h: int = 800) -> str:
    """Stable stock photo for an empty image slot (same key -> same photo). Replace with real photos via the DB."""
    seed = "".join(c if c.isalnum() else "-" for c in key.lower()).strip("-") or "stayflow"
    return f"https://picsum.photos/seed/sf-{seed}/{w}/{h}"


def room_media(r, big: bool = False):
    return Img(src=safe_url(r["image_url"]) or placeholder_img(f"room-{r['id']}", 1200 if big else 800, 750 if big else 500),
               alt=r["name"], loading="lazy",
               cls="h-full w-full bg-slate-200 object-cover transition duration-700 group-hover:scale-105")


def Photo(url: str = "", label: str = "", i: int = 0, cls: str = "", small: bool = False):
    """A photo slot: the given image, or a placeholder stock photo when no URL is set yet."""
    src = safe_url(url) or placeholder_img(f"{label}-{i}", 600 if small else 1200, 450 if small else 900)
    return Div(Img(src=src, alt=label, loading="lazy",
                   cls="h-full w-full object-cover transition duration-700 group-hover:scale-105"),
               Span(label, cls="absolute bottom-2 left-2 rounded-full bg-slate-900/60 px-2 py-0.5 text-[11px] font-semibold "
                               "text-white backdrop-blur" + ("" if small else " sm:bottom-3 sm:left-3 sm:px-2.5 sm:py-1 sm:text-xs"))
               if label else None,
               cls="group relative overflow-hidden bg-slate-200 " + cls)


def gallery_items(prop) -> list[dict]:
    items = [g for g in db.json_list(prop, "gallery") if isinstance(g, dict)]
    return items or [{"url": "", "caption": c} for c in DEFAULT_GALLERY]


def gallery_grid(items):
    """Bento-style grid: first photo large, rest fill around it; swipeable row on mobile."""
    items = items[:5]
    spans = ("sm:col-span-2 sm:row-span-2", "", "", "", "")
    return Div(*[Photo(g.get("url", ""), g.get("caption", ""), i,
                       cls=f"sf-reveal aspect-[4/3] w-[80%] shrink-0 snap-center rounded-[1.75rem] "
                           f"sm:aspect-auto sm:w-auto {spans[i]}")
                 for i, g in enumerate(items)],
               cls="sf-noscroll -mx-4 flex snap-x snap-mandatory gap-3 overflow-x-auto px-4 pb-2 "
                   "sm:mx-0 sm:grid sm:auto-rows-[11rem] sm:grid-cols-4 sm:gap-4 sm:overflow-visible sm:px-0 sm:pb-0 "
                   "lg:auto-rows-[13rem]")


def room_gallery(r, open_: bool):
    """Main room photo plus placeholder shots of the other spaces."""
    shots = ("Bed", "Bathroom", "Living area", "View")
    return Div(
        Div(room_media(r, big=True), status_pill(open_),
            cls="group relative col-span-4 aspect-[16/10] overflow-hidden rounded-[2rem] bg-stone-200 "
                "sm:col-span-3 sm:row-span-2 sm:aspect-auto"),
        *[Photo("", s, r["id"] * 10 + i, small=True,
                cls="aspect-square rounded-2xl sm:aspect-auto sm:rounded-[1.5rem]") for i, s in enumerate(shots)],
        cls="grid grid-cols-4 gap-2 sm:auto-rows-[9rem] sm:grid-cols-5 sm:gap-3 lg:auto-rows-[10rem]",
    )


def status_pill(open_: bool):
    return Span(
        Span(cls=f"mr-1.5 inline-block h-1.5 w-1.5 rounded-full {'bg-emerald-500' if open_ else 'bg-amber-500'}"),
        "Available" if open_ else "Under maintenance",
        cls="sf-glass-light absolute left-3 top-3 z-10 inline-flex items-center rounded-full px-2.5 py-1 text-[11px] font-bold text-slate-800 shadow-sm",
    )


def fav_button(rid: int):
    """Save-for-later heart; remembered in this browser only."""
    return Button(Icon("heart", "h-4 w-4"), type="button", data_fav=str(rid), aria_pressed="false", aria_label="Save room",
                  onclick="event.preventDefault();sfFav(this)",
                  cls="sf-fav sf-glass absolute right-3 top-3 z-10 grid h-9 w-9 place-items-center rounded-full text-white transition")


def room_card(prop, r, extra_cls: str = "", q: str = "", free: bool | None = None):
    """`q` carries the visitor's dates/guests into the links; `free` is availability for those dates (None = unknown)."""
    open_ = r["status"] == "available"
    bookable = open_ and free is not False
    amenities = [a.strip() for a in r["amenities"].split(",") if a.strip()]
    detail = f"/rooms/{r['id']}" + (f"?{q}" if q else "")
    return Article(
        Div(A(room_media(r), href=detail, aria_label=f"View {r['name']}", cls="block h-full w-full"),
            Div(cls="pointer-events-none absolute inset-x-0 bottom-0 h-24 bg-gradient-to-t from-slate-950/60 to-transparent"),
            status_pill(open_), fav_button(r["id"]),
            Span(Icon("check" if free else "x", "h-3.5 w-3.5"), "Free on your dates" if free else "Booked on your dates",
                 cls="absolute right-3 bottom-3 inline-flex items-center gap-1 rounded-full px-2.5 py-1 text-[11px] font-bold shadow-sm "
                     + ("bg-emerald-500 text-white" if free else "bg-rose-500 text-white")) if open_ and free is not None else None,
            P(Span(money(prop, r["rate_per_night"]), cls="text-lg font-extrabold"), Span(" /night", cls="text-xs text-white/75"),
              cls="pointer-events-none absolute bottom-3 left-4 text-white"),
            cls="relative aspect-[4/3] overflow-hidden rounded-[1.4rem] bg-stone-200"),
        Div(
            Div(H3(A(r["name"], href=detail, cls="hover:text-teal-700"), cls="text-[17px] font-bold tracking-tight"),
                P(Icon("users", "h-3.5 w-3.5"), f"{r['capacity']} guests",
                  cls="flex shrink-0 items-center gap-1 text-xs font-semibold text-slate-500"),
                cls="flex items-start justify-between gap-3"),
            P(r["description"], cls="mt-1 line-clamp-2 text-sm text-slate-500") if r["description"] else None,
            Div(*[Span(a, cls="rounded-full bg-stone-100 px-2.5 py-1 text-[11px] font-semibold text-slate-600")
                  for a in amenities[:3]],
                Span(f"+{len(amenities) - 3}", cls="rounded-full bg-stone-100 px-2.5 py-1 text-[11px] font-semibold text-slate-400") if len(amenities) > 3 else None,
                cls="mt-3 flex flex-wrap gap-1.5") if amenities else None,
            Div(A("View details", href=detail, cls="text-sm font-semibold text-slate-700 underline-offset-4 hover:underline"),
                A("Book", Icon("arrow-right", "h-4 w-4"), href=f"/book?room_id={r['id']}" + (f"&{q}" if q else ""), cls=BTN_PRIMARY + " !py-2.5")
                if bookable else Span("Unavailable", cls=BTN + " pointer-events-none bg-stone-100 text-slate-400", aria_disabled="true"),
                cls="mt-auto flex items-center justify-between gap-3 pt-4"),
            cls="flex flex-1 flex-col px-2 pb-2 pt-4",
        ),
        cls="sf-reveal group flex flex-col rounded-[1.75rem] bg-white p-2 shadow-[0_8px_30px_-12px_rgba(15,23,42,0.12)] ring-1 ring-stone-200/70 "
            "transition duration-300 hover:-translate-y-1 hover:shadow-[0_20px_40px_-16px_rgba(15,23,42,0.25)]"
            + ("" if bookable else " opacity-80") + (" " + extra_cls if extra_cls else ""),
    )


def book_form(rooms, values: dict | None = None, error: str | None = None):
    v = values or {}
    today = date.today().isoformat()
    open_rooms = [r for r in rooms if r["status"] == "available"]
    return Form(
        Div(Div(Icon("sparkles", "h-5 w-5"), cls="grid h-10 w-10 place-items-center rounded-xl bg-teal-50 text-teal-600"),
            Div(H2("Request a stay", cls="text-lg font-bold tracking-tight"),
                P("Nothing is charged here.", cls="text-xs text-slate-500")),
            cls="flex items-center gap-3"),
        P("Send your dates — the owner checks availability and replies with downpayment details.",
          cls="mt-3 text-sm text-slate-600"),
        Div(error, cls="mt-4 rounded-xl bg-rose-50 p-3 text-sm text-rose-800 ring-1 ring-rose-200", role="alert") if error else None,
        Div(
            Field("Room", Select(*[Option(f"{r['name']} — up to {r['capacity']}", value=str(r["id"]),
                                          data_capacity=str(r["capacity"]), data_rate=str(r["rate_per_night"]),
                                          selected=str(r["id"]) == str(v.get("room_id", "")))
                                   for r in open_rooms],
                                 name="room_id", id="room_id", required=True, cls=INPUT)),
            Div(Field("Check-in", Input(type="date", name="check_in", min=today, required=True, oninput="sfSyncOut(this)",
                                        value=v.get("check_in", ""), cls=INPUT + " !px-2.5 sm:!px-3.5")),
                Field("Check-out", Input(type="date", name="check_out", min=v.get("check_in") or today, required=True,
                                         value=v.get("check_out", ""), cls=INPUT + " !px-2.5 sm:!px-3.5")),
                cls="grid gap-3.5 min-[400px]:grid-cols-2 min-[400px]:gap-3"),
            Field("Guests", Input(type="number", name="guests", min="1", max="50", required=True,
                                  value=v.get("guests") or "2", cls=INPUT)),
            Field("Full name", Input(name="guest_name", required=True, maxlength="120",
                                     value=v.get("guest_name", ""), autocomplete="name", cls=INPUT)),
            Field("Mobile number", Input(name="guest_phone", type="tel", required=True, maxlength="40",
                                         value=v.get("guest_phone", ""), autocomplete="tel",
                                         placeholder="09xx xxx xxxx", cls=INPUT)),
            Field("Email (optional)", Input(name="guest_email", type="email", maxlength="120",
                                            value=v.get("guest_email", ""), autocomplete="email", cls=INPUT)),
            Field("Notes (optional)", Textarea(v.get("notes", ""), name="notes", rows="2", maxlength="1000",
                                               placeholder="Arrival time, add-on services, special requests…", cls=INPUT)),
            # Honeypot: real users never see or fill this.
            Input(name="website", tabindex="-1", autocomplete="off", cls="hidden", aria_hidden="true"),
            cls="mt-5 space-y-3.5",
        ),
        # Live availability + price breakdown, refreshed whenever room/dates/guests change.
        Div(P("Checking availability…", cls="text-sm text-slate-500"), id="book-avail",
            hx_get="/availability", hx_trigger="load, change from:closest form delay:150ms",
            # innerHTML explicitly: otherwise it inherits the form's outerHTML swap and the panel
            # replaces itself on the first check, so later date/room/guest changes never update it.
            hx_swap="innerHTML",
            hx_include="closest form", hx_sync="this:replace", aria_live="polite", cls="mt-5")
        if open_rooms else None,
        Button(Span("Send request", cls="sf-idle"),
               Span(NotStr('<svg class="sf-spin h-4 w-4" viewBox="0 0 24 24" fill="none" aria-hidden="true"><circle cx="12" cy="12" r="9" stroke="currentColor" stroke-opacity=".3" stroke-width="3"/><path d="M21 12a9 9 0 0 0-9-9" stroke="currentColor" stroke-width="3" stroke-linecap="round"/></svg>'),
                    "Sending…", cls="sf-busy items-center gap-2"),
               type="submit", cls=BTN_PRIMARY + " mt-5 w-full !py-3", disabled=not open_rooms),
        P(Icon("shield", "mr-1 h-3.5 w-3.5"), "Your dates are confirmed only after owner approval.",
          cls="mt-3 flex items-center justify-center text-xs text-slate-500"),
        P("No rooms are open for requests right now.", cls="mt-2 text-sm text-amber-700") if not open_rooms else None,
        id="book", hx_post="/book", hx_swap="outerHTML", hx_disabled_elt="find button[type=submit]",
        cls=CARD + " scroll-mt-24 transition-shadow",
    )


# --------------------------------------------------------------------------- #
# Site layout (shared by every public page)
# --------------------------------------------------------------------------- #

SECTION = "scroll-mt-24 py-14 sm:py-20"
WRAP = "mx-auto max-w-6xl px-4"
NAV_LINKS = (("Home", "/"), ("About", "/about"), ("Rooms", "/rooms"), ("Services", "/services"),
             ("FAQ", "/faq"), ("Contact", "/contact"))


def Eyebrow(text):
    return P(Span(cls="h-1.5 w-1.5 rounded-full bg-teal-500"), text,
             cls="inline-flex items-center gap-2 rounded-full bg-white px-3 py-1 text-[11px] font-bold uppercase "
                 "tracking-[0.16em] text-slate-600 ring-1 ring-stone-200")


def SectionHead(eyebrow, title, sub=None, center=False, link=None):
    """Section heading; `link=(label, href)` adds a "see all" pill on the right."""
    head = Div(Eyebrow(eyebrow),
               H2(title, cls="mt-4 text-[1.9rem] font-extrabold leading-[1.1] tracking-tight text-slate-900 sm:text-[2.6rem]"),
               P(sub, cls="mt-3 text-base text-slate-500" + (" mx-auto max-w-2xl" if center else " max-w-xl")) if sub else None,
               cls="text-center" if center else "")
    if link:
        return Div(head, A(link[0], Span(Icon("arrow-right", "h-3.5 w-3.5"),
                                         cls="grid h-7 w-7 place-items-center rounded-full bg-slate-900 text-white transition group-hover:translate-x-0.5"),
                           href=link[1],
                           cls="group inline-flex shrink-0 items-center gap-2 self-start rounded-full bg-white py-1 pl-4 pr-1 text-sm "
                               "font-semibold text-slate-800 ring-1 ring-stone-200 transition hover:ring-slate-300 sm:self-auto"),
                   cls="sf-reveal mb-8 flex flex-col gap-5 sm:mb-10 sm:flex-row sm:items-end sm:justify-between")
    return Div(head, cls="sf-reveal mb-8 sm:mb-10")


def safe_url(u: str) -> str:
    return u if u and u.startswith(("https://", "http://")) else ""


SITE_JS = """
function sfFav(b){ let f=[]; try{f=JSON.parse(localStorage.getItem('sf-fav')||'[]')}catch(e){}
  const id=b.dataset.fav, on=!f.includes(id); f=on?[...f,id]:f.filter(x=>x!==id);
  try{localStorage.setItem('sf-fav',JSON.stringify(f))}catch(e){}
  document.querySelectorAll('[data-fav="'+id+'"]').forEach(x=>x.setAttribute('aria-pressed',on));
  if(window.sfToast) sfToast(on?'Saved to your favourites':'Removed from favourites'); }
function sfFavInit(){ let f=[]; try{f=JSON.parse(localStorage.getItem('sf-fav')||'[]')}catch(e){}
  document.querySelectorAll('[data-fav]').forEach(x=>x.setAttribute('aria-pressed',f.includes(x.dataset.fav))); }
document.addEventListener('DOMContentLoaded',sfFavInit); document.addEventListener('htmx:afterSettle',sfFavInit);
document.documentElement.classList.add('sf-js');
function sfSyncOut(i){ const o=i.form.elements.check_out; if(o){ o.min=i.value; if(o.value && o.value<=i.value) o.value=''; } sfNights(i.form); }
function sfNights(f){
  const el=f.querySelector('[data-nights]'); if(!el) return;
  const a=f.elements.check_in&&f.elements.check_in.value, b=f.elements.check_out&&f.elements.check_out.value;
  const n=(a&&b)?Math.round((new Date(b)-new Date(a))/864e5):0;
  el.textContent = n>0 ? '· '+n+' night'+(n>1?'s':'') : '';
}
function sfToast(msg){
  let t=document.getElementById('sf-toast');
  if(!t){ t=document.createElement('div'); t.id='sf-toast'; t.setAttribute('role','status'); document.body.appendChild(t); }
  t.textContent=msg; t.classList.add('sf-show'); clearTimeout(t._h); t._h=setTimeout(()=>t.classList.remove('sf-show'),2600);
}
async function sfShare(title){
  try{ if(navigator.share){ await navigator.share({title, url:location.href}); return; }
       await navigator.clipboard.writeText(location.href); sfToast('Link copied — paste it anywhere'); }catch(e){}
}
function sfAskAbout(q){
  sfChat(true); const f=document.getElementById('chat-form');
  if(f){ f.elements.message.value=q; setTimeout(()=>f.elements.message.focus(),60); }
}
function sfPickRoom(btn, id){
  const s=btn.form.elements.room_id; s.value=id; s.dispatchEvent(new Event('change',{bubbles:true}));
  sfToast('Room switched');
}
function sfReveal(){
  const els=document.querySelectorAll('.sf-reveal:not(.sf-in)');
  if(!('IntersectionObserver' in window)){ els.forEach(e=>e.classList.add('sf-in')); return; }
  window.sfIO = window.sfIO || new IntersectionObserver(es=>es.forEach(e=>{
    if(e.isIntersecting){ e.target.classList.add('sf-in'); window.sfIO.unobserve(e.target); } }), {rootMargin:'0px 0px -6% 0px'});
  els.forEach((el,i)=>{ el.style.transitionDelay=(i%4)*70+'ms'; window.sfIO.observe(el); });
}
if(!window.sfInit){
  window.sfInit=true;
  if(window.htmx) htmx.config.globalViewTransitions=true;  // smooth cross-fade between pages where supported
  const quiet=el=>el&&el.closest&&(el.closest('#sf-chat')||el.closest('#book-avail'));
  document.addEventListener('htmx:beforeRequest',e=>{ if(!quiet(e.detail.elt)) document.documentElement.classList.add('sf-loading'); });
  ['htmx:afterSettle','htmx:responseError','htmx:sendError'].forEach(n=>document.addEventListener(n,()=>{
    document.documentElement.classList.remove('sf-loading'); sfReveal(); }));
  document.addEventListener('click',e=>{ const d=document.querySelector('nav details[open]'); if(d&&!d.contains(e.target)) d.removeAttribute('open'); });
  if(document.readyState==='loading') document.addEventListener('DOMContentLoaded',sfReveal); else setTimeout(sfReveal);
} else { setTimeout(sfReveal); }
"""


def site_nav(prop, active: str):
    def link_cls(href, mobile=False):
        on = href == active
        if mobile:
            return "block rounded-2xl px-4 py-3 text-sm font-semibold " + ("bg-slate-900 text-white" if on else "text-slate-700 hover:bg-stone-100")
        return "rounded-full px-3.5 py-2 text-sm font-semibold transition " + (
            "bg-slate-900 text-white" if on else "text-slate-600 hover:bg-stone-100 hover:text-slate-900")

    return Nav(
        Div(A(Span(prop["name"][:1].upper(), cls="grid h-9 w-9 place-items-center rounded-full bg-slate-900 text-sm font-bold text-white"),
              Span(prop["name"], cls="truncate text-[15px] font-extrabold tracking-tight"), href="/",
              cls="flex min-w-0 items-center gap-2.5"),
            Div(*[A(t, href=h, cls=link_cls(h), aria_current="page" if h == active else None) for t, h in NAV_LINKS],
                cls="hidden items-center gap-0.5 rounded-full bg-stone-100/80 p-1 lg:flex"),
            Div(A("Book now", Icon("arrow-right", "h-4 w-4"), href="/book", cls=BTN_PRIMARY + " hidden !py-2 sm:inline-flex"),
                Details(
                    Summary(Icon("menu", "h-5 w-5"), aria_label="Menu",
                            cls="grid h-10 w-10 cursor-pointer list-none place-items-center rounded-full bg-stone-100 text-slate-800 hover:bg-stone-200 [&::-webkit-details-marker]:hidden"),
                    Div(*[A(t, href=h, cls=link_cls(h, mobile=True)) for t, h in NAV_LINKS],
                        A("Book now", href="/book", cls=BTN_PRIMARY + " mt-2 w-full !py-3"),
                        cls="absolute right-0 top-14 w-64 rounded-3xl bg-white p-2 shadow-2xl shadow-slate-900/15 ring-1 ring-stone-200"),
                    cls="relative lg:hidden"),
                cls="flex shrink-0 items-center gap-2"),
            cls="mx-auto flex max-w-6xl items-center justify-between gap-4 rounded-full bg-white/80 py-2 pl-2.5 pr-2 "
                "shadow-[0_8px_30px_-12px_rgba(15,23,42,0.18)] ring-1 ring-stone-200/80 backdrop-blur-xl"),
        cls="sticky top-0 z-40 px-3 pt-3 sm:px-4",
    )


def page_header(title: str, sub: str | None = None, tall: bool = False, image: str = ""):
    """Inset photo banner at the top of every inner page."""
    return Header(
        Div(
            Img(src=safe_url(image) or placeholder_img(f"header-{title}", 1920, 700), alt="",
                cls="absolute inset-0 h-full w-full object-cover"),
            Div(cls="absolute inset-0 bg-gradient-to-t from-slate-950/85 via-slate-900/45 to-slate-900/25"),
            Div(
                Nav(A("Home", href="/", cls="hover:text-white"), Span("/", cls="text-white/40"), Span(title, cls="text-white"),
                    aria_label="Breadcrumb", cls="sf-glass inline-flex flex-wrap items-center gap-2 rounded-full px-3 py-1 text-xs font-medium text-white/80"),
                H1(title, cls="mt-4 text-[2.25rem] font-extrabold leading-[1.05] tracking-tight sm:text-6xl"),
                P(sub, cls="mt-3 max-w-2xl text-base text-white/80 sm:text-lg") if sub else None,
                cls="relative px-6 pb-8 pt-24 sm:px-12 sm:pb-12 sm:pt-36" + (" !pb-20 sm:!pb-24" if tall else ""),
            ),
            cls="relative mx-auto max-w-[90rem] overflow-hidden rounded-[2rem] bg-slate-900 text-white sm:rounded-[2.5rem]",
        ),
        cls="px-3 pt-3 sm:px-4",
    )


SOCIALS = (("Facebook", "facebook_url", "facebook"), ("Instagram", "instagram_url", "instagram"),
           ("Messenger", "messenger_url", "messenger"))


def social_links(prop, dark=True, big=False):
    socials = [(n, safe_url(prop[k]), b) for n, k, b in SOCIALS]
    socials = [x for x in socials if x[1]]
    if not socials:
        return None
    if big:  # contact page: one colourful card per network
        return Div(*[A(BrandBadge(b, "h-12 w-12", "h-6 w-6"),
                       Div(P(n, cls="font-bold text-slate-900"),
                           P("Message us" if b == "messenger" else "Follow us", cls="text-xs text-slate-500")),
                       Icon("arrow-right", "ml-auto h-4 w-4 -rotate-45 text-slate-400 transition group-hover:text-slate-900"),
                       href=u, target="_blank", rel="noopener",
                       cls="group flex items-center gap-3 rounded-3xl bg-white p-3 pr-4 ring-1 ring-stone-200/70 transition "
                           "hover:-translate-y-0.5 hover:shadow-lg")
                     for n, u, b in socials], cls="mt-6 grid gap-3 sm:grid-cols-3")
    style = ("bg-white/5 text-white ring-1 ring-white/10 hover:bg-white/10" if dark
             else "bg-white text-slate-800 ring-1 ring-stone-200 hover:ring-slate-300")
    return Div(*[A(BrandBadge(b, "h-8 w-8", "h-[18px] w-[18px]"), n, href=u, target="_blank", rel="noopener", aria_label=n,
                   cls=f"inline-flex items-center gap-2 rounded-full py-1 pl-1 pr-4 text-sm font-semibold transition hover:-translate-y-0.5 {style}")
                 for n, u, b in socials], cls="mt-4 flex flex-wrap gap-2")


def tel_href(phone: str) -> str:
    return "tel:" + "".join(c for c in phone if c.isdigit() or c == "+")


def site_footer(prop):
    about = (prop["about"] or prop["tagline"] or "").split("\n")[0]
    if len(about) > 160:
        about = about[:157].rsplit(" ", 1)[0] + "…"
    head = "text-xs font-bold uppercase tracking-[0.16em] text-white/50"
    link = "inline-flex items-center gap-2 text-sm text-slate-300 transition hover:text-white"

    def contact(icon, i, text, href=None):
        body = (IconTile(icon, i, "h-9 w-9", "h-4 w-4", tone=("bg-teal-400/15 text-teal-300", "bg-amber-400/15 text-amber-300",
                                                              "bg-sky-400/15 text-sky-300")[i]),
                Span(text, cls="min-w-0 break-words text-sm text-slate-300"))
        return Li(A(*body, href=href, cls="flex items-center gap-3 transition hover:text-white") if href
                  else Div(*body, cls="flex items-center gap-3"))

    return Footer(
        Div(cls="pointer-events-none absolute -left-24 -top-24 h-72 w-72 rounded-full bg-teal-500/20 blur-3xl"),
        Div(cls="pointer-events-none absolute -right-24 top-1/3 h-72 w-72 rounded-full bg-violet-500/10 blur-3xl"),
        Div(
            Div(Div(Span(prop["name"][:1].upper(), cls="grid h-10 w-10 place-items-center rounded-full bg-white text-sm font-extrabold text-slate-900"),
                    Span(prop["name"], cls="text-lg font-extrabold text-white"), cls="flex items-center gap-3"),
                P(about, cls="mt-4 max-w-sm text-sm leading-relaxed text-slate-400") if about else None,
                Div(P("Follow us", cls=head + " mt-7"), social_links(prop)) if any(prop[k] for _, k, _ in SOCIALS) else None,
                cls="col-span-2 lg:col-span-1"),
            Div(P("Explore", cls=head),
                Ul(*[Li(A(Span(cls="h-1 w-1 rounded-full bg-teal-400"), t, href=h, cls=link))
                     for t, h in NAV_LINKS[1:] + (("Book a stay", "/book"),)],
                   cls="mt-4 grid grid-cols-2 gap-3 lg:grid-cols-1"), cls="col-span-2 lg:col-span-1"),
            Div(P("Contact", cls=head),
                Ul(contact("pin", 0, prop["location"]) if prop["location"] else None,
                   contact("phone", 1, prop["contact_phone"], tel_href(prop["contact_phone"])) if prop["contact_phone"] else None,
                   contact("mail", 2, prop["contact_email"], "mailto:" + prop["contact_email"]) if prop["contact_email"] else None,
                   cls="mt-4 space-y-3"), cls="col-span-2 lg:col-span-1"),
            cls=WRAP + " relative grid grid-cols-2 gap-x-6 gap-y-10 pb-10 pt-14 sm:pt-16 lg:grid-cols-[1.6fr_0.8fr_1.2fr]"),
        # oversized wordmark as a graphic flourish
        Div(P(prop["name"].split()[0] if prop["name"] else "", aria_hidden="true",
              cls="select-none whitespace-nowrap bg-gradient-to-b from-white/15 to-white/0 bg-clip-text text-center text-[22vw] "
                  "font-extrabold leading-[0.8] tracking-tighter text-transparent lg:text-[14rem]"),
            cls="relative -mb-4 overflow-hidden px-4"),
        Div(Div(P(f"© {date.today().year} {prop['name']}. All rights reserved.", cls="text-xs text-slate-500"),
                Div(A("Owner login", href="/admin", hx_boost="false", cls="text-xs text-slate-500 hover:text-slate-300"),
                    Span("Powered by StayFlow", cls="text-xs text-slate-600"), cls="flex gap-4"),
                cls=WRAP + " flex flex-col items-center justify-between gap-2 py-6 sm:flex-row"),
            cls="relative border-t border-white/10"),
        cls="relative overflow-hidden bg-slate-950",
    )


BOOKBAR_JS = """
(()=>{const bar=document.getElementById('sf-bookbar'); if(!bar||!('IntersectionObserver' in window)) return;
  const seen=new Set();  // hide the bar while a booking form or the footer is on screen
  const io=new IntersectionObserver(es=>{es.forEach(e=>e.isIntersecting?seen.add(e.target):seen.delete(e.target));
    bar.classList.toggle('sf-away', seen.size>0);});
  const watch=()=>{const b=document.getElementById('book'); if(b) io.observe(b);
    document.querySelectorAll('[data-hidebar]').forEach(x=>io.observe(x));};
  watch(); const f=document.querySelector('footer'); if(f) io.observe(f);
  document.body.addEventListener('htmx:afterSwap', watch);
})();
"""


def mobile_bookbar(prop, rooms, room=None):
    """Floating dark pill on phones (app-style). On a room page it books that room."""
    if room is not None:
        if room["status"] != "available":
            return None
        rate, sub, href, label = room["rate_per_night"], f"Up to {room['capacity']} guests", "#book", "Book"
    else:
        open_rooms = [r for r in rooms if r["status"] == "available"]
        if not open_rooms:
            return None
        rate = min(r["rate_per_night"] for r in open_rooms)
        sub = f"{len(open_rooms)} room{'s' if len(open_rooms) != 1 else ''} available"
        href, label = "/book", "Book now"
    return Div(
        Div(Div(P(Span("" if room is not None else "From ", cls="text-xs font-medium text-white/60"),
                  Span(money(prop, rate), cls="text-lg font-extrabold"), Span(" /night", cls="text-xs text-white/60"),
                  cls="whitespace-nowrap"),
                P(Span(cls="mr-1.5 inline-block h-1.5 w-1.5 rounded-full bg-emerald-400"), sub,
                  cls="flex items-center text-[11px] font-medium text-white/70")),
            A(label, Icon("arrow-right", "h-4 w-4"), href=href,
              cls=BTN + " shrink-0 whitespace-nowrap bg-white !px-5 !py-3 text-slate-900 hover:bg-stone-100"),
            cls="flex items-center justify-between gap-3 rounded-full bg-slate-900 py-2 pl-5 pr-2 text-white "
                "shadow-2xl shadow-slate-900/30 ring-1 ring-white/10"),
        Script(BOOKBAR_JS),
        id="sf-bookbar",
        cls="fixed inset-x-0 bottom-0 z-40 px-3 pb-[max(0.75rem,env(safe-area-inset-bottom))] pr-[5.5rem] lg:hidden",
    )


def announcements(prop, rooms) -> list[str]:
    """Owner-set ticker lines (`announcements` JSON list), else sensible ones built from live data."""
    custom = [str(a).strip() for a in db.json_list(prop, "announcements") if str(a).strip()]
    if custom:
        return custom
    open_rooms = [r for r in rooms if r["status"] == "available"]
    min_rate = min((r["rate_per_night"] for r in open_rooms), default=None)
    return [x for x in (
        f"Rooms from {money(prop, min_rate)} / night" if min_rate is not None else None,
        "Book direct — no booking fees",
        f"Only {prop['downpayment_pct']}% downpayment after approval" if prop["downpayment_pct"] else None,
        f"Check-in {prop['check_in_time']} · Check-out {prop['check_out_time']}",
        "24/7 AI concierge — ask anything",
        prop["location"] or None,
    ) if x]


def announcement_bar(prop, rooms):
    items = announcements(prop, rooms)
    if not items:
        return None
    def run(hidden=False):
        return Div(*[Span(Span(Icon("sparkles", "h-3.5 w-3.5 text-teal-300"), cls="shrink-0"), t,
                          cls="inline-flex items-center gap-2 whitespace-nowrap px-5") for t in items],
                   cls="flex shrink-0 items-center", aria_hidden="true" if hidden else None)
    # Repeat the line so short lists still fill wide screens, then duplicate for a seamless loop.
    reps = max(1, -(-6 // len(items)))
    one = Div(*[run(hidden=i > 0) for i in range(reps)], cls="flex shrink-0")
    two = Div(*[run(hidden=True) for _ in range(reps)], cls="flex shrink-0", aria_hidden="true")
    return Div(
        Div(one, two, cls="sf-track", style=f"--sf-speed:{max(20, 7 * len(items) * reps)}s"),
        role="region", aria_label="Announcements",
        cls="sf-marquee overflow-hidden bg-slate-900 py-2 text-[12px] font-semibold text-white/90 sm:text-[13px]",
    )


def site_page(prop, rooms, title: str, active: str, *content, bookbar=True, bookbar_room=None):
    """Every public page: nav, content, footer, pinned mobile booking bar, floating concierge."""
    bar = mobile_bookbar(prop, rooms, bookbar_room) if bookbar else None
    return page(
        title if title == prop["name"] else f"{title} · {prop['name']}",
        Script(SITE_JS),
        Div(id="sf-progress", aria_hidden="true"),
        A("Skip to content", href="#main",
          cls="sr-only focus:not-sr-only focus:fixed focus:left-4 focus:top-4 focus:z-[60] focus:rounded-lg focus:bg-white focus:px-4 focus:py-2 focus:shadow-lg"),
        # hx-boost turns every internal link/form into an in-place page swap: no white flash, chat stays open.
        Div(
            announcement_bar(prop, rooms),
            site_nav(prop, active),
            Main(*content, id="main", cls="flex-1"),
            cta_band(prop) if active not in ("/book",) and bookbar_room is None else None,
            site_footer(prop),
            Div(cls="h-[calc(4.5rem+env(safe-area-inset-bottom))] bg-slate-900 lg:hidden") if bar else None,
            bar,
            hx_boost="true", cls="flex flex-1 flex-col",
        ),
        chat_widget("/chat/guest", "Booking Agent", "Online · can check dates & book",
                    f"Hi! 👋 I'm the {prop['name']} booking agent. Tell me your dates and group size — "
                    "I'll check live availability, quote the exact price and send your request.",
                    "e.g. 2 guests, Dec 20 to 22…",
                    suggestions=("Free rooms this weekend for 2?", "Book a room for 4 next Friday", "Check my booking status",
                                 "How does payment work?"),
                    footnote="Requests are confirmed only after the owner approves them. Nothing is charged here."),
    )


def cta_band(prop):
    return Section(
        Div(Img(src=placeholder_img("cta-getaway", 1600, 700), alt="", loading="lazy",
                cls="absolute inset-0 h-full w-full object-cover"),
            Div(cls="absolute inset-0 bg-gradient-to-r from-slate-950/90 via-slate-900/70 to-slate-900/30"),
            Div(Div(H2("Ready for your next getaway?", cls="text-[1.9rem] font-extrabold leading-tight tracking-tight sm:text-4xl"),
                    P("Send a stay request in a minute, or ask our concierge anything first.",
                      cls="mt-3 max-w-md text-white/75")),
                Div(A("Book your stay", Icon("arrow-right", "h-4 w-4"), href="/book",
                      cls=BTN + " bg-white !px-6 !py-3.5 text-slate-900 hover:bg-stone-100"),
                    Button(Icon("chat", "h-4 w-4"), "Ask the concierge", type="button", onclick="sfChat(true)",
                           cls=BTN + " sf-glass !px-6 !py-3.5 text-white hover:bg-white/25"),
                    cls="flex flex-wrap gap-3"),
                cls="relative flex flex-col gap-8 lg:flex-row lg:items-end lg:justify-between"),
            cls="sf-reveal relative mx-auto max-w-6xl overflow-hidden rounded-[2rem] bg-slate-900 px-6 py-12 text-white sm:rounded-[2.5rem] sm:px-12 sm:py-16"),
        cls="px-4 pb-14 sm:pb-20",
    )


# --------------------------------------------------------------------------- #
# Page sections
# --------------------------------------------------------------------------- #

def hero(prop, rooms):
    today = date.today().isoformat()
    open_rooms = [r for r in rooms if r["status"] == "available"]
    min_rate = min((r["rate_per_night"] for r in open_rooms), default=None)
    img = safe_url(prop["hero_image_url"]) or placeholder_img("hero", 1920, 1200)
    chip = "sf-glass inline-flex items-center gap-1.5 rounded-full px-3 py-1.5 text-xs font-semibold text-white"
    qlabel = "mb-0.5 flex items-center gap-1.5 text-[11px] font-semibold text-slate-400"
    qinput = "w-full min-w-0 border-0 bg-transparent p-0 text-base font-bold text-slate-900 focus:outline-none focus:ring-0 sm:text-[15px]"
    cell = "block min-w-0 rounded-2xl px-3 py-2.5 transition hover:bg-stone-100 sm:px-4"
    quick = Form(
        Label(Span(Icon("calendar", "h-3.5 w-3.5"), "Check-in", cls=qlabel),
              Input(type="date", name="check_in", min=today, cls=qinput, oninput="sfSyncOut(this)"), cls=cell),
        Label(Span(Icon("calendar", "h-3.5 w-3.5"), "Check-out", Span(data_nights="1", cls="text-teal-600"), cls=qlabel),
              Input(type="date", name="check_out", min=today, cls=qinput, oninput="sfNights(this.form)"), cls=cell),
        Label(Span(Icon("users", "h-3.5 w-3.5"), "Guests", cls=qlabel),
              Input(type="number", name="guests", min="1", max="50", value="2", cls=qinput), cls=cell + " col-span-2 sm:col-span-1"),
        Button(Icon("search", "h-5 w-5"), Span("Search stays", cls="lg:hidden"), type="submit", aria_label="Check availability",
               cls=BTN_PRIMARY + " col-span-2 !py-4 sm:col-span-3 lg:col-span-1 lg:h-14 lg:w-14 lg:!p-0"),
        method="get", action="/book", data_hidebar="1",
        cls="sf-glass-light mt-8 grid max-w-3xl grid-cols-2 items-center gap-1 rounded-[1.75rem] p-2 shadow-2xl shadow-slate-950/30 "
            "sm:grid-cols-3 lg:grid-cols-[1fr_1fr_0.7fr_auto] lg:rounded-full lg:pl-3",
    )
    return Header(
        Div(
            Img(src=img, alt="", fetchpriority="high", cls="absolute inset-0 h-full w-full object-cover"),
            Div(cls="absolute inset-0 bg-gradient-to-t from-slate-950/90 via-slate-900/40 to-slate-900/20"),
            Div(
                Div(P(Icon("pin", "h-3.5 w-3.5"), prop["location"], cls=chip) if prop["location"] else None,
                    Span(Icon("chat", "h-3.5 w-3.5"), "24/7 AI concierge", cls=chip + " hidden sm:inline-flex"),
                    cls="flex flex-wrap gap-2"),
                H1(prop["name"], cls="mt-4 max-w-3xl text-[2.25rem] font-extrabold leading-[1.02] tracking-tight sm:text-7xl"),
                P(prop["tagline"], cls="mt-4 max-w-xl text-base text-white/80 sm:text-lg") if prop["tagline"] else None,
                Div(Div(Span(f"{len(open_rooms)}", cls="text-2xl font-extrabold"),
                        Span(f"room{'s' if len(open_rooms) != 1 else ''} open", cls="text-xs text-white/70"), cls="flex flex-col"),
                    Div(cls="h-9 w-px bg-white/25"),
                    Div(Span(money(prop, min_rate), cls="text-2xl font-extrabold"), Span("from / night", cls="text-xs text-white/70"),
                        cls="flex flex-col") if min_rate is not None else None,
                    cls="mt-6 flex items-center gap-5"),
                quick,
                cls="relative flex min-h-[min(calc(100svh-12rem),46rem)] flex-col justify-end px-4 pb-4 pt-24 sm:min-h-[44rem] sm:px-12 sm:pb-12",
            ),
            cls="relative mx-auto max-w-[90rem] overflow-hidden rounded-[2rem] bg-slate-900 text-white sm:rounded-[2.5rem]",
        ),
        cls="px-3 pt-3 sm:px-4",
    )


def about_paras(prop):
    return [p.strip() for p in (prop["about"] or "").split("\n\n") if p.strip()] or [
        f"Welcome to {prop['name']}" + (f" in {prop['location']}" if prop["location"] else "") + ". "
        + (prop["tagline"] or "We look forward to hosting you.")]


def highlight_list(items):
    return Ul(*[Li(Span(Icon("check", "h-3.5 w-3.5"), cls="grid h-6 w-6 shrink-0 place-items-center rounded-full bg-teal-100 text-teal-700"),
                   Span(h, cls="text-sm font-medium text-slate-700"), cls="flex items-center gap-3")
                for h in items], cls="mt-8 grid gap-3 sm:grid-cols-2") if items else None


def about_stats(prop, rooms):
    open_rooms = [r for r in rooms if r["status"] == "available"]
    max_guests = max((r["capacity"] for r in rooms), default=0)
    min_rate = min((r["rate_per_night"] for r in open_rooms), default=None)
    tile = "rounded-[1.75rem] p-6 ring-1"
    return Div(
        Div(Div(str(len(rooms)), cls="text-4xl font-extrabold"), P("Rooms & units", cls="mt-1 text-sm text-white/70"),
            cls=tile + " bg-slate-900 text-white ring-slate-900"),
        Div(Div(f"{max_guests}", cls="text-4xl font-extrabold text-slate-900"), P("Max guests per room", cls="mt-1 text-sm text-slate-500"),
            cls=tile + " bg-white ring-stone-200/70 sm:translate-y-8"),
        Div(Div(prop["check_in_time"], cls="text-4xl font-extrabold text-slate-900"), P("Check-in time", cls="mt-1 text-sm text-slate-500"),
            cls=tile + " bg-white ring-stone-200/70"),
        Div(Div(money(prop, min_rate) if min_rate is not None else "—", cls="text-4xl font-extrabold text-slate-900"),
            P("Starting rate / night", cls="mt-1 text-sm text-slate-500"),
            cls=tile + " bg-teal-50 ring-teal-100 sm:translate-y-8"),
        cls="sf-reveal grid grid-cols-2 gap-4 self-center",
    )


def services_grid(services):
    return Div(*[Div(
        IconTile(s.get("icon") or "sparkles", n, "h-12 w-12 transition group-hover:scale-110", "h-6 w-6"),
        Div(H3(s["title"], cls="text-base font-bold tracking-tight sm:mt-5 sm:text-lg"),
            P(s.get("description", ""), cls="mt-1 flex-1 text-sm leading-relaxed text-slate-600 sm:mt-2"),
            P(s["price"], cls="mt-3 inline-flex w-fit rounded-full bg-teal-50 px-3 py-1 text-xs font-bold text-teal-700 sm:mt-4") if s.get("price") else None,
            cls="flex min-w-0 flex-1 flex-col"),
        cls="sf-reveal group flex gap-4 rounded-[1.75rem] bg-white p-5 ring-1 ring-stone-200/70 transition duration-300 hover:-translate-y-1 hover:shadow-xl hover:shadow-slate-900/5 sm:flex-col sm:gap-0 sm:p-6")
        for n, s in enumerate(services)], cls="grid gap-3 sm:grid-cols-2 sm:gap-6 lg:grid-cols-3")


def get_services(prop):
    return [s for s in db.json_list(prop, "services") if isinstance(s, dict) and s.get("title")]


def booking_steps(prop):
    methods = [m for m, ok in (("GCash", prop["gcash_number"]), ("Bank transfer", prop["bank_account_number"])) if ok]
    steps = [
        ("Send a request", "Pick a room and your dates. It takes a minute and nothing is charged."),
        ("We confirm", "The owner checks availability and messages you with downpayment details."),
        (f"Pay {prop['downpayment_pct']}% to lock it in", "Send your downpayment and your dates are secured. Balance is paid on arrival."),
    ]
    return Div(
        H2("How booking works", cls="text-xl font-extrabold tracking-tight"),
        Ol(*[Li(Div(str(i), cls="grid h-10 w-10 shrink-0 place-items-center rounded-full bg-teal-600 text-sm font-bold text-white shadow-md shadow-teal-600/30"),
                Div(H3(t, cls="font-bold text-slate-900"), P(d, cls="mt-1 text-sm text-slate-600")),
                cls="flex gap-4") for i, (t, d) in enumerate(steps, 1)],
           cls="mt-6 space-y-6"),
        Div(P("Accepted payments", cls="text-xs font-bold uppercase tracking-wider text-slate-500"),
            Div(*[Badge(m, "teal") for m in methods] or [Badge("Ask the owner")], cls="mt-2 flex flex-wrap gap-2"),
            cls="mt-8 rounded-2xl bg-white p-5 ring-1 ring-slate-200/70"),
        Div(P(Icon("clock", "h-4 w-4"), f"Check-in {prop['check_in_time']} · Check-out {prop['check_out_time']}",
              cls="flex items-center gap-2 text-sm text-slate-600"),
            cls="mt-3 rounded-2xl bg-white p-5 ring-1 ring-slate-200/70"),
        Div(Div(Icon("chat", "h-5 w-5"), cls="grid h-10 w-10 shrink-0 place-items-center rounded-xl bg-white/15"),
            Div(P("Have a question first?", cls="font-bold"),
                P("Our AI concierge answers instantly, day or night.", cls="text-sm text-teal-50/90")),
            Button("Ask now", type="button", onclick="sfChat(true)",
                   cls=BTN + " w-full shrink-0 bg-white !py-2.5 text-teal-700 hover:bg-teal-50 sm:ml-auto sm:w-auto"),
            cls="mt-3 flex flex-wrap items-center gap-4 rounded-2xl bg-gradient-to-br from-teal-600 to-cyan-700 p-5 text-white sm:flex-nowrap"),
    )


def faq_items(prop):
    methods = [m for m, ok in (("GCash", prop["gcash_number"]), ("bank transfer", prop["bank_account_number"])) if ok]
    faqs = [(f["q"], f["a"]) for f in db.json_list(prop, "faqs") if isinstance(f, dict) and f.get("q") and f.get("a")]
    faqs += [
        ("What time is check-in and check-out?",
         f"Check-in is from {prop['check_in_time']} and check-out is until {prop['check_out_time']}."),
        ("How much is the downpayment and how do I pay?",
         f"A {prop['downpayment_pct']}% downpayment secures your dates after the owner approves your request"
         + (f". We accept {' and '.join(methods)}." if methods else ". The owner will send payment details.")
         + " The balance is paid on arrival."),
    ]
    if prop["house_rules"]:
        faqs.append(("What are the house rules?", prop["house_rules"]))
    return faqs


def contact_cards(prop):
    phone, email, loc = prop["contact_phone"], prop["contact_email"], prop["location"]
    messenger = safe_url(prop["messenger_url"])

    def item(icon, label, value, href=None, i=0):
        badge = BrandBadge("messenger", "h-12 w-12", "h-6 w-6") if icon == "messenger" else IconTile(icon, i, "h-12 w-12")
        body = Div(badge,
                   Div(P(label, cls="text-xs font-bold uppercase tracking-wider text-slate-500"),
                       P(value, cls="mt-0.5 font-semibold text-slate-900"), cls="min-w-0"),
                   Icon("arrow-right", "ml-auto h-4 w-4 shrink-0 -rotate-45 text-slate-300") if href else None,
                   cls="flex items-center gap-4 rounded-3xl bg-white p-3.5 pr-5 ring-1 ring-stone-200/70 transition"
                       + (" hover:-translate-y-0.5 hover:shadow-lg" if href else ""))
        return A(body, href=href, cls="block", **({"target": "_blank", "rel": "noopener"} if href and href.startswith("http") else {})) if href else body

    items = [
        item("phone", "Call or text", phone, tel_href(phone), 5) if phone else None,
        item("mail", "Email", email, "mailto:" + email, 1) if email else None,
        item("messenger", "Messenger", "Message us on Messenger", messenger) if messenger else None,
        item("pin", "Address", loc, None, 4) if loc else None,
        item("clock", "Check-in / out", f"{prop['check_in_time']} / {prop['check_out_time']}", None, 3),
    ]
    return Div(*[i for i in items if i is not None], cls="space-y-3")


def map_embed(prop):
    from urllib.parse import quote
    loc = prop["location"]
    if not loc:
        return None
    return Div(
        Iframe(src=f"https://maps.google.com/maps?q={quote(loc)}&z=15&output=embed", loading="lazy",
               title=f"Map of {prop['name']}", referrerpolicy="no-referrer-when-downgrade",
               cls="h-72 w-full border-0 sm:h-full sm:min-h-[26rem]"),
        cls="overflow-hidden rounded-3xl ring-1 ring-slate-200 shadow-sm")


def _site_data(req):
    with db.connect(tenant(req)) as conn:
        return db.get_property(conn), db.list_rooms(conn)



# --------------------------------------------------------------------------- #
# Date helpers, live availability, "book direct" strip
# --------------------------------------------------------------------------- #

def parse_stay(check_in: str, check_out: str):
    """(d_in, d_out) as dates if both parse and check_out > check_in, else None."""
    try:
        a, b = date.fromisoformat(check_in), date.fromisoformat(check_out)
    except (TypeError, ValueError):
        return None
    return (a, b) if b > a else None


def stay_query(check_in: str = "", check_out: str = "", guests: str = "") -> str:
    from urllib.parse import urlencode
    q = {k: v for k, v in (("check_in", check_in), ("check_out", check_out), ("guests", guests)) if v}
    return urlencode(q)


def short_range(a: date, b: date) -> str:
    return f"{a:%b} {a.day}–{b.day}" if (a.year, a.month) == (b.year, b.month) else f"{a:%b} {a.day} – {b:%b} {b.day}"


def avail_box(tone: str, icon: str, title, body=None, *extra):
    tones = {"ok": "bg-emerald-50 text-emerald-900 ring-emerald-200", "warn": "bg-amber-50 text-amber-900 ring-amber-200",
             "bad": "bg-rose-50 text-rose-900 ring-rose-200", "info": "bg-slate-50 text-slate-600 ring-slate-200"}
    return Div(Div(Icon(icon, "mt-0.5 h-5 w-5 shrink-0"),
                   Div(P(title, cls="text-sm font-semibold"), P(body, cls="mt-0.5 text-xs opacity-80") if body else None,
                       cls="min-w-0"), cls="flex gap-3"),
               *extra, cls=f"sf-bubble rounded-xl p-4 ring-1 {tones[tone]}")


def availability_panel(conn, prop, rooms, room_id: str, check_in: str, check_out: str, guests: str):
    open_rooms = [r for r in rooms if r["status"] == "available"]
    room = next((r for r in open_rooms if str(r["id"]) == str(room_id)), None)
    if room is None:
        return avail_box("info", "home", "Pick a room to see your total.")
    n_guests = int(guests) if guests.isdigit() and int(guests) > 0 else 1
    rate_line = f"{money(prop, room['rate_per_night'])} per night · up to {room['capacity']} guests"
    if not check_in or not check_out:
        return avail_box("info", "calendar", "Pick your dates to check availability", rate_line)
    stay = parse_stay(check_in, check_out)
    if stay is None:
        return avail_box("warn", "calendar", "Check-out must be after check-in.")
    d_in, d_out = stay
    nights = (d_out - d_in).days
    if d_in < date.today():
        return avail_box("warn", "calendar", "That check-in date has already passed.")
    if nights > db.MAX_NIGHTS:
        return avail_box("warn", "calendar", f"Stays over {db.MAX_NIGHTS} nights are arranged directly.",
                         "Message us and we'll sort out a long-stay rate.")

    def free(r):
        return not db.find_overlaps(conn, r["id"], check_in, check_out)

    def alternatives(title):
        alts = [r for r in open_rooms if r["id"] != room["id"] and r["capacity"] >= n_guests and free(r)]
        if not alts:
            return P("No other rooms are free on these dates — try moving your dates by a day or two.",
                     cls="mt-3 text-xs font-medium")
        return Div(P(title, cls="mb-2 text-xs font-semibold uppercase tracking-wider opacity-70"),
                   Div(*[Button(Span(r["name"], cls="font-semibold"), Span(f"{money(prop, r['rate_per_night'])}/night", cls="opacity-70"),
                                type="button", onclick=f"sfPickRoom(this,'{r['id']}')",
                                cls="inline-flex items-center gap-2 rounded-full bg-white px-3 py-1.5 text-xs text-slate-800 ring-1 ring-slate-200 transition hover:ring-teal-400")
                         for r in alts], cls="flex flex-wrap gap-2"),
                   cls="mt-3")

    if n_guests > room["capacity"]:
        if not any(r["capacity"] >= n_guests for r in open_rooms):
            biggest = max(r["capacity"] for r in open_rooms)
            return avail_box("warn", "users", f"No single room fits {n_guests} guests (max {biggest}).",
                             "For bigger groups, send a request for each room you need, or message us and we'll help plan it.")
        return avail_box("warn", "users", f"{room['name']} fits up to {room['capacity']} guests.",
                         "Pick a bigger room for your group:", alternatives("Rooms that fit"))
    clashes = db.find_overlaps(conn, room["id"], check_in, check_out)
    if clashes:
        return avail_box("bad", "x", f"{room['name']} is already booked for {short_range(d_in, d_out)}.",
                         "Try different dates, or one of these rooms that's free:", alternatives("Free on your dates"))
    pending = db.find_overlaps(conn, room["id"], check_in, check_out, statuses=("pending_audit",))
    total, down = db.quote(room["rate_per_night"], nights, prop["downpayment_pct"])
    row = "flex items-center justify-between gap-4"
    return avail_box(
        "ok", "check-circle", f"Available · {short_range(d_in, d_out)}",
        f"{nights} night{'s' if nights != 1 else ''} in {room['name']} for {n_guests} guest{'s' if n_guests != 1 else ''}",
        Div(Div(Span(f"{money(prop, room['rate_per_night'])} × {nights} night{'s' if nights != 1 else ''}"),
                Span(money(prop, total)), cls=row),
            Div(Span(f"Downpayment after approval ({prop['downpayment_pct']}%)"), Span(money(prop, down)), cls=row),
            Div(Span("Balance on arrival"), Span(money(prop, total - down)), cls=row),
            Div(Span("Estimated total", cls="font-bold"), Span(money(prop, total), cls="text-base font-extrabold"),
                cls=row + " border-t border-emerald-200 pt-2"),
            cls="mt-3 space-y-1.5 rounded-lg bg-white/70 p-3 text-sm text-slate-700"),
        P(Icon("clock", "mr-1 h-3.5 w-3.5"), "Someone else has also requested these dates — send yours soon.",
          cls="mt-2 flex items-center text-xs font-medium text-amber-700") if pending else None,
    )


@rt("/availability")
def get(req, room_id: str = "", check_in: str = "", check_out: str = "", guests: str = ""):
    with db.connect(tenant(req)) as conn:
        prop, rooms = db.get_property(conn), db.list_rooms(conn)
        return availability_panel(conn, prop, rooms, room_id, check_in, check_out, guests)


def direct_strip():
    items = [
        ("wallet", "No booking fees", "Book straight with the owner."),
        ("shield", "Pay after approval", "Nothing is charged online."),
        ("phone", "Real people", "Talk to the owner directly."),
        ("chat", "24/7 concierge", "Instant answers, any time."),
    ]
    return Section(
        Div(*[Div(IconTile(i, n),
                  Div(P(t, cls="text-sm font-bold text-slate-900"), P(d, cls="mt-0.5 text-xs text-slate-500")),
                  cls="sf-reveal flex w-[72%] shrink-0 snap-start items-center gap-3 rounded-3xl bg-white p-3 pr-5 ring-1 ring-stone-200/70 "
                      "sm:w-auto")
              for n, (i, t, d) in enumerate(items)],
            cls="sf-noscroll mx-auto flex max-w-6xl snap-x gap-3 overflow-x-auto px-4 sm:grid sm:grid-cols-2 sm:overflow-visible lg:grid-cols-4"),
        cls="pt-4 sm:pt-6",
    )


# --------------------------------------------------------------------------- #
# Public pages
# --------------------------------------------------------------------------- #

@rt("/")
def get(req):
    prop, rooms = _site_data(req)
    services = get_services(prop)
    featured = rooms[:3]
    highlights = [str(h) for h in db.json_list(prop, "highlights")]
    return site_page(
        prop, rooms, prop["name"], "/",
        hero(prop, rooms),
        direct_strip(),
        Section(
            Div(Div(SectionHead("About us", f"Welcome to {prop['name']}"),
                    P(about_paras(prop)[0], cls="-mt-4 text-base leading-relaxed text-slate-600"),
                    highlight_list(highlights[:4]),
                    A("More about us", Icon("arrow-right", "h-4 w-4"), href="/about",
                      cls="mt-8 inline-flex items-center gap-1.5 text-sm font-semibold text-teal-700 hover:text-teal-800")),
                about_stats(prop, rooms),
                cls=WRAP + " grid items-start gap-12 lg:grid-cols-2"),
            cls=SECTION),
        Section(
            Div(SectionHead("Rooms & rates", "Find your perfect room", "Live rates and availability, straight from the property.",
                            link=(f"View all {len(rooms)} rooms", "/rooms") if len(rooms) > len(featured) else ("View all rooms", "/rooms")),
                P("Swipe to see more", Icon("arrow-right", "h-3.5 w-3.5"),
                  cls="-mt-4 mb-4 flex items-center gap-1 text-xs font-medium text-slate-400 sm:hidden") if len(featured) > 1 else None,
                Div(*[room_card(prop, r, "w-[85%] shrink-0 snap-center sm:w-auto") for r in featured],
                    cls="sf-noscroll -mx-4 flex snap-x snap-mandatory gap-4 overflow-x-auto px-4 pb-2 "
                        "sm:mx-0 sm:grid sm:grid-cols-2 sm:gap-6 sm:overflow-visible sm:px-0 sm:pb-0 lg:grid-cols-3")
                if featured else Div("No rooms listed yet.", cls=CARD + " text-center text-slate-500"),
                cls=WRAP),
            cls=SECTION),
        Section(
            Div(SectionHead("Gallery", "Take a look around", f"A glimpse of life at {prop['name']}."),
                gallery_grid(gallery_items(prop)), cls=WRAP),
            cls=SECTION + " !pt-0"),
        Section(
            Div(SectionHead("Services & add-ons", "Make the most of your stay",
                            "Extras we can arrange for you during your stay.", link=("See all services", "/services")),
                services_grid(services[:3]), cls=WRAP),
            cls=SECTION) if services else None,
        Div(cls="h-14 sm:h-20"),
    )


@rt("/about")
def get(req):
    prop, rooms = _site_data(req)
    highlights = [str(h) for h in db.json_list(prop, "highlights")]
    return site_page(
        prop, rooms, "About us", "/about",
        page_header("About us", prop["tagline"] or None),
        Section(
            Div(Div(Eyebrow("Our story"),
                    H2(f"Welcome to {prop['name']}", cls="mt-2 text-[1.75rem] font-extrabold leading-tight tracking-tight sm:text-4xl"),
                    Div(*[P(p, cls="text-base leading-relaxed text-slate-600") for p in about_paras(prop)], cls="mt-6 space-y-4"),
                    highlight_list(highlights)),
                about_stats(prop, rooms),
                cls=WRAP + " grid items-start gap-12 lg:grid-cols-2"),
            cls=SECTION + " bg-white"),
        Section(
            Div(SectionHead("Around the property", "See where you'll stay"),
                gallery_grid(gallery_items(prop)[1:] + gallery_items(prop)[:1]), cls=WRAP),
            cls=SECTION),
        Section(
            Div(SectionHead("House rules", "Good to know before you arrive"),
                Div(Div(Icon("shield", "h-6 w-6"), cls="grid h-12 w-12 shrink-0 place-items-center rounded-2xl bg-teal-50 text-teal-600"),
                    P(prop["house_rules"], cls="whitespace-pre-line text-base leading-relaxed text-slate-700"),
                    cls="-mt-2 flex gap-5 rounded-2xl bg-slate-50 p-6 ring-1 ring-slate-200/70"),
                cls=WRAP),
            cls=SECTION + " bg-white") if prop["house_rules"] else Div(cls="h-14 sm:h-20"),
    )


def rooms_filter(v: dict):
    today = date.today().isoformat()
    lab = "mb-1 block text-[11px] font-bold uppercase tracking-wider text-slate-500"
    box = "block rounded-xl bg-slate-50 px-3 py-2 ring-1 ring-slate-200 transition focus-within:bg-white focus-within:ring-2 focus-within:ring-teal-500"
    inp = "w-full border-0 bg-transparent p-0 text-base font-semibold text-slate-900 focus:outline-none focus:ring-0 sm:text-sm"
    return Form(
        Label(Span("Check-in", cls=lab), Input(type="date", name="check_in", min=today, value=v["check_in"], cls=inp,
                                                oninput="sfSyncOut(this)"), cls=box),
        Label(Span("Check-out", Span(data_nights="1", cls="ml-1 normal-case tracking-normal text-teal-600"), cls=lab),
              Input(type="date", name="check_out", min=v["check_in"] or today, value=v["check_out"], cls=inp,
                    oninput="sfNights(this.form)"), cls=box),
        Label(Span("Guests", cls=lab), Input(type="number", name="guests", min="1", max="50", value=v["guests"],
                                              placeholder="Any", cls=inp), cls=box),
        Label(Span("Sort by", cls=lab),
              Select(*[Option(t, value=k, selected=v["sort"] == k) for k, t in
                       (("", "Recommended"), ("price", "Price: low to high"), ("-price", "Price: high to low"),
                        ("capacity", "Most guests"))],
                     name="sort", cls=inp + " cursor-pointer"), cls=box),
        Button(Icon("calendar", "h-4 w-4"), "Update", cls=BTN_PRIMARY + " col-span-2 !rounded-xl !py-3 sm:col-span-1"),
        hx_get="/rooms", hx_target="#room-results", hx_select="#room-results", hx_swap="outerHTML",
        hx_push_url="true", hx_trigger="change delay:250ms, submit",
        cls="sf-reveal grid grid-cols-2 gap-2 rounded-2xl bg-white p-3 shadow-lg shadow-slate-900/5 ring-1 ring-slate-200/70 "
            "sm:grid-cols-[1fr_1fr_0.6fr_1fr_auto] sm:items-end",
    )


@rt("/rooms")
def get(req, check_in: str = "", check_out: str = "", guests: str = "", sort: str = ""):
    prop, rooms = _site_data(req)
    stay = parse_stay(check_in, check_out)
    if stay and stay[0] < date.today():
        stay = None
    n_guests = int(guests) if guests.isdigit() and int(guests) > 0 else 0
    v = dict(check_in=check_in if stay else "", check_out=check_out if stay else "",
             guests=str(n_guests) if n_guests else "", sort=sort)
    q = stay_query(v["check_in"], v["check_out"], v["guests"])

    free = {}
    if stay:
        with db.connect(tenant(req)) as conn:
            free = {r["id"]: not db.find_overlaps(conn, r["id"], v["check_in"], v["check_out"]) for r in rooms}
    shown = [r for r in rooms if r["capacity"] >= n_guests]
    keys = {"price": lambda r: r["rate_per_night"], "-price": lambda r: -r["rate_per_night"],
            "capacity": lambda r: -r["capacity"]}
    if sort in keys:
        shown.sort(key=keys[sort])
    # Bookable rooms first, then booked-on-your-dates, then maintenance.
    shown.sort(key=lambda r: (r["status"] != "available", stay is not None and not free.get(r["id"], True)))
    n_free = sum(1 for r in shown if r["status"] == "available" and (not stay or free.get(r["id"])))

    bits = []
    if stay:
        bits.append(short_range(*stay))
    if n_guests:
        bits.append(f"{n_guests} guest{'s' if n_guests != 1 else ''}")
    summary = (f"{n_free} of {len(shown)} room{'s' if len(shown) != 1 else ''} available" if stay
               else f"{len(shown)} room{'s' if len(shown) != 1 else ''}") + (" · " + " · ".join(bits) if bits else "")
    results = Div(
        Div(P(summary, cls="text-sm font-semibold text-slate-700"),
            A("Clear filters", href="/rooms", cls="text-sm font-medium text-teal-700 hover:underline") if (stay or n_guests or sort) else None,
            cls="mb-5 flex items-center justify-between gap-4"),
        Div(*[room_card(prop, r, q=q, free=free.get(r["id"]) if stay else None) for r in shown],
            cls="grid gap-6 sm:grid-cols-2 lg:grid-cols-3") if shown else
        Div(Icon("users", "mx-auto h-8 w-8 text-slate-400"),
            P(f"No rooms fit {n_guests} guests in one booking.", cls="mt-3 font-semibold text-slate-800"),
            P("Book two rooms, or message us for group arrangements.", cls="mt-1 text-sm text-slate-500"),
            A("Contact us", href="/contact", cls=BTN_PRIMARY + " mt-4"),
            cls=CARD + " text-center"),
        id="room-results",
    )
    return site_page(
        prop, rooms, "Rooms & rates", "/rooms",
        page_header("Rooms & rates", "Add your dates to see live availability and prices for your stay.", tall=True),
        Section(Div(Div(rooms_filter(v), cls="relative z-10 -mt-16 mb-10 sm:-mt-20"), results, cls=WRAP),
                cls="pb-14 sm:pb-20"),
    )

@rt("/rooms/{rid}")
def get(req, rid: int, check_in: str = "", check_out: str = "", guests: str = ""):
    prop, rooms = _site_data(req)
    r = next((x for x in rooms if x["id"] == rid), None)  # list_rooms already excludes hidden rooms
    if r is None:
        return site_page(prop, rooms, "Room not found", "/rooms",
                         page_header("Room not found", "This room isn't available anymore."),
                         Section(Div(A("See all rooms", href="/rooms", cls=BTN_PRIMARY), cls=WRAP), cls=SECTION))
    open_ = r["status"] == "available"
    amenities = [a.strip() for a in r["amenities"].split(",") if a.strip()]
    others = [x for x in rooms if x["id"] != rid][:3]
    fact = ("flex flex-col items-start gap-1.5 rounded-3xl bg-white p-3.5 text-xs ring-1 ring-stone-200/70 "
            "sm:flex-row sm:items-center sm:gap-3 sm:p-4 sm:text-sm")
    return site_page(
        prop, rooms, r["name"], "/rooms",
        Section(
            Div(
                Nav(A("Home", href="/", cls="hover:text-teal-700"), Span("/"), A("Rooms", href="/rooms", cls="hover:text-teal-700"),
                    Span("/"), Span(r["name"], cls="text-slate-900"),
                    aria_label="Breadcrumb", cls="flex flex-wrap items-center gap-2 text-xs font-medium text-slate-500"),
                Div(
                    Div(
                        room_gallery(r, open_),
                        Div(H1(r["name"], cls="text-3xl font-extrabold tracking-tight sm:text-4xl"),
                            P(Span(money(prop, r["rate_per_night"]), cls="text-2xl font-extrabold text-teal-700"),
                              Span(" / night", cls="text-slate-500"), cls="shrink-0"),
                            cls="mt-6 flex flex-col gap-2 sm:flex-row sm:items-end sm:justify-between"),
                        P(r["description"], cls="mt-4 text-base leading-relaxed text-slate-600") if r["description"] else None,
                        Div(Button(Icon("chat", "h-4 w-4"), "Ask about this room", type="button",
                                   onclick=f"sfAskAbout({json.dumps('Tell me more about the ' + r['name'] + ' — is it good for my group?')})",
                                   cls=BTN_GHOST),
                            Button(Icon("arrow-right", "h-4 w-4 -rotate-45"), "Share", type="button",
                                   onclick=f"sfShare({json.dumps(r['name'] + ' · ' + prop['name'])})", cls=BTN_GHOST),
                            cls="mt-5 flex flex-wrap gap-2"),
                        Div(Div(Icon("users", "h-5 w-5 text-teal-600"), P(Span("Guests", cls="block text-slate-500"), Span(f"Up to {r['capacity']}", cls="font-semibold")), cls=fact),
                            Div(Icon("clock", "h-5 w-5 text-teal-600"), P(Span("Check-in", cls="block text-slate-500"), Span(prop["check_in_time"], cls="font-semibold")), cls=fact),
                            Div(Icon("clock", "h-5 w-5 text-teal-600"), P(Span("Check-out", cls="block text-slate-500"), Span(prop["check_out_time"], cls="font-semibold")), cls=fact),
                            cls="mt-6 grid grid-cols-3 gap-2 sm:gap-3"),
                        Div(H2("Amenities", cls="text-lg font-bold tracking-tight"),
                            Ul(*[Li(Span(Icon("check", "h-3.5 w-3.5"), cls="grid h-6 w-6 shrink-0 place-items-center rounded-full bg-teal-100 text-teal-700"),
                                    Span(a, cls="text-sm font-medium text-slate-700"), cls="flex items-center gap-3") for a in amenities],
                               cls="mt-4 grid gap-3 sm:grid-cols-2"),
                            cls="mt-8") if amenities else None,
                        Div(H2("House rules", cls="text-lg font-bold tracking-tight"),
                            P(prop["house_rules"], cls="mt-3 whitespace-pre-line text-sm leading-relaxed text-slate-600"),
                            cls="mt-8") if prop["house_rules"] else None,
                        P(Icon("wallet", "mr-2 h-4 w-4 shrink-0"),
                          f"{prop['downpayment_pct']}% downpayment after approval secures your dates. Balance is paid on arrival.",
                          cls="mt-8 flex items-start rounded-2xl bg-amber-50 p-4 text-sm text-amber-900 ring-1 ring-amber-200/70"),
                        cls="min-w-0 lg:col-span-3",
                    ),
                    Div(Div(book_form(rooms, {"room_id": str(r["id"]), "check_in": check_in if parse_stay(check_in, check_out) else "",
                                              "check_out": check_out if parse_stay(check_in, check_out) else "",
                                              "guests": guests if guests.isdigit() else ""}) if open_ else
                            Div(H2("Not bookable right now", cls="text-lg font-bold"),
                                P("This room is under maintenance. Have a look at our other rooms.", cls="mt-2 text-sm text-slate-600"),
                                A("See other rooms", href="/rooms", cls=BTN_PRIMARY + " mt-4 w-full"), cls=CARD),
                            cls="lg:sticky lg:top-24"),
                        cls="lg:col-span-2"),
                    cls="mt-6 grid gap-10 lg:grid-cols-5",
                ),
                cls=WRAP),
            cls="py-8 sm:py-12"),
        Section(
            Div(SectionHead("More rooms", "You might also like", link=("All rooms", "/rooms")),
                Div(*[room_card(prop, x) for x in others], cls="grid gap-6 sm:grid-cols-2 lg:grid-cols-3"),
                cls=WRAP),
            cls=SECTION + " bg-white") if others else None,
        bookbar_room=r,
    )


@rt("/services")
def get(req):
    prop, rooms = _site_data(req)
    services = get_services(prop)
    return site_page(
        prop, rooms, "Services & add-ons", "/services",
        page_header("Services & add-ons", "Extras we can arrange for you. Mention them in your booking notes or ask the concierge."),
        Section(Div(services_grid(services) if services else
                    Div("Ask us about extras for your stay — we're happy to help.", cls=CARD + " text-center text-slate-600"),
                    cls=WRAP), cls=SECTION),
    )


@rt("/faq")
def get(req):
    prop, rooms = _site_data(req)
    return site_page(
        prop, rooms, "FAQ", "/faq",
        page_header("Frequently asked questions", "Everything you need to know about staying with us."),
        Section(
            Div(Div(*[Details(
                Summary(Span(q, cls="font-semibold text-slate-900"),
                        Span(Icon("plus", "h-5 w-5"), cls="shrink-0 text-teal-600 transition group-open:rotate-45"),
                        cls="flex cursor-pointer list-none items-center justify-between gap-4 p-5 [&::-webkit-details-marker]:hidden"),
                P(a, cls="-mt-1 whitespace-pre-line px-5 pb-5 text-sm leading-relaxed text-slate-600"),
                cls="group rounded-2xl bg-white ring-1 ring-slate-200/70 transition open:shadow-md open:ring-teal-200",
                open=(i == 0))
                for i, (q, a) in enumerate(faq_items(prop))], cls="mx-auto max-w-3xl space-y-3"),
                P("Still have questions? ", A("Contact us", href="/contact", cls="font-semibold text-teal-700 hover:underline"),
                  " or ask the concierge in the corner.", cls="mx-auto mt-8 max-w-3xl text-center text-sm text-slate-600"),
                cls=WRAP),
            cls=SECTION),
    )


@rt("/contact")
def get(req):
    prop, rooms = _site_data(req)
    return site_page(
        prop, rooms, "Contact us", "/contact",
        page_header("Contact & location", "Questions about your stay, group bookings or events? Reach us any way you like."),
        Section(
            Div(Div(H2("Get in touch", cls="mb-5 text-xl font-extrabold tracking-tight"),
                    contact_cards(prop), social_links(prop, dark=False, big=True)),
                map_embed(prop),
                cls=WRAP + " grid items-stretch gap-10 lg:grid-cols-2"),
            cls=SECTION),
    )


@rt("/book")
def get(req, room_id: str = "", check_in: str = "", check_out: str = "", guests: str = ""):
    prop, rooms = _site_data(req)
    open_rooms = [r for r in rooms if r["status"] == "available"]
    iso = lambda s: s if len(s) == 10 and s[4] == "-" and s[7] == "-" else ""  # noqa: E731  (only prefill YYYY-MM-DD)
    if not room_id and guests.isdigit():  # from the home search: pick the first room that fits the party
        fit = min((r for r in open_rooms if r["capacity"] >= int(guests)),
                  key=lambda r: (r["capacity"], r["rate_per_night"]), default=None)
        room_id = str(fit["id"]) if fit else ""
    values = dict(room_id=room_id, check_in=iso(check_in), check_out=iso(check_out),
                  guests=guests if guests.isdigit() else "")
    return site_page(
        prop, rooms, "Book a stay", "/book",
        page_header("Book your stay", "Book directly with us — the best rate, and a real person on the other end."),
        Section(
            Div(Div(book_form(rooms, values), cls="lg:order-2"),
                Div(booking_steps(prop), cls="lg:order-1"),
                cls=WRAP + " grid items-start gap-10 lg:grid-cols-2"),
            cls="py-10 sm:py-16"),
        bookbar=False,
    )


@rt("/book")
def post(req, room_id: str = "", check_in: str = "", check_out: str = "", guests: str = "1",
         guest_name: str = "", guest_phone: str = "", guest_email: str = "", notes: str = "",
         website: str = ""):
    values = dict(room_id=room_id, check_in=check_in, check_out=check_out, guests=guests,
                  guest_name=guest_name, guest_phone=guest_phone, guest_email=guest_email, notes=notes)
    with db.connect(tenant(req)) as conn:
        prop = db.get_property(conn)
        rooms = db.list_rooms(conn)
        if website:  # honeypot tripped: pretend success, store nothing
            return Div(H2("Request sent!", cls="text-xl font-semibold"), cls=CARD, id="book")
        try:
            bid = db.create_booking_request(
                conn, room_id=int(room_id or 0), guest_name=guest_name, guest_phone=guest_phone,
                guest_email=guest_email, guests=int(guests or 0), check_in=check_in,
                check_out=check_out, notes=notes)
        except ValueError as e:  # BookingError subclasses ValueError; int() errors too
            msg = str(e) if isinstance(e, db.BookingError) else "Please check the form and try again."
            return book_form(rooms, values, msg)
        b = db.get_booking(conn, bid)
    return Div(
        Div(Icon("check", "h-6 w-6"), cls="grid h-12 w-12 place-items-center rounded-full bg-emerald-100 text-emerald-600"),
        H2("Request sent! 🎉", cls="mt-4 text-xl font-extrabold tracking-tight"),
        P(f"Reference SF-{tenant(req).upper()}-{b['id']} · {b['room_name']}", cls="mt-2 text-sm font-semibold"),
        P(f"{nice_date(b['check_in'])} → {nice_date(b['check_out'])} ({b['nights']} night"
          f"{'s' if b['nights'] != 1 else ''}, {b['guests']} guest{'s' if b['guests'] != 1 else ''})",
          cls="text-sm text-slate-600"),
        P(f"Estimated total {money(prop, b['total_amount'])} · downpayment "
          f"{money(prop, b['downpayment_amount'])} after approval.", cls="mt-1 text-sm text-slate-600"),
        P("The owner will review your request and message you on "
          f"{b['guest_phone']} with payment instructions. Your dates are not held until then.",
          cls="mt-3 rounded-lg bg-amber-50 p-3 text-sm text-amber-900"),
        Div(A("Back to home", href="/", cls=BTN_GHOST), A("Browse services", href="/services", cls=BTN_GHOST),
            cls="mt-4 flex flex-wrap gap-2"),
        id="book", cls=CARD,
    )


# --------------------------------------------------------------------------- #
# Agent cards (rendered under the agent's reply)
# --------------------------------------------------------------------------- #

CARD_WRAP = "sf-bubble ml-9 rounded-2xl bg-white p-3 shadow-sm ring-1 ring-slate-200/70"


def _row(label, value, strong=False):
    return Div(Span(label, cls="text-slate-500"), Span(value, cls="font-bold text-slate-900" if strong else "font-semibold text-slate-800"),
               cls="flex justify-between gap-3 text-xs")


def card_rooms(prop, c, rooms_by_id):
    nights = c["nights"]
    items = []
    for r in sorted(c["rooms"], key=lambda r: (not r["available"], r["total"])):
        room = rooms_by_id.get(r["room_id"])
        items.append(Div(
            Div(room_media(room) if room else None, cls="h-20 overflow-hidden rounded-xl bg-stone-200"),
            P(r["name"], cls="mt-2 truncate text-xs font-bold text-slate-900"),
            P(Span(money(prop, r["total"]), cls="font-extrabold"), f" · {nights} night{'s' if nights != 1 else ''}",
              cls="text-[11px] text-slate-600"),
            Button("Book this", type="button", cls=BTN_PRIMARY + " mt-2 w-full !py-1.5 !text-xs",
                   onclick=f"sfSay({json.dumps('I want to book the ' + r['name'])})")
            if r["available"] else P(r["reason"], cls="mt-2 rounded-lg bg-stone-100 px-2 py-1 text-center text-[11px] font-semibold text-slate-500"),
            cls="w-36 shrink-0" + ("" if r["available"] else " opacity-60")))
    free = sum(r["available"] for r in c["rooms"])
    return Div(
        P(Icon("calendar", "h-3.5 w-3.5"), f"{short_date(c['check_in'])} → {short_date(c['check_out'])} · {free} of {len(c['rooms'])} rooms free",
          cls="flex items-center gap-1.5 text-[11px] font-bold uppercase tracking-wide text-slate-500"),
        Div(*items, cls="sf-noscroll mt-2 flex gap-2 overflow-x-auto pb-1"),
        data_card="rooms", cls=CARD_WRAP)


def card_booking_draft(prop, c):
    return Div(
        Div(Span(Icon("document", "h-4 w-4"), cls="grid h-8 w-8 place-items-center rounded-full bg-teal-100 text-teal-700"),
            Div(P("Review your stay request", cls="text-sm font-bold"), P("Nothing is charged now.", cls="text-[11px] text-slate-500")),
            cls="flex items-center gap-2"),
        Div(_row("Room", c["room_name"]), _row("Dates", f"{short_date(c['check_in'])} → {short_date(c['check_out'])}"),
            _row("Guests", str(c["guests"])), _row("Name", c["guest_name"]), _row("Mobile", c["guest_phone"]),
            _row("Email", c["guest_email"]) if c.get("guest_email") else None,
            _row("Notes", c["notes"][:60]) if c.get("notes") else None,
            Div(cls="my-1 border-t border-dashed border-slate-200"),
            _row(f"{c['nights']} night{'s' if c['nights'] != 1 else ''} × {money(prop, c['rate_per_night'])}", money(prop, c["total_amount"]), True),
            _row(f"Downpayment after approval ({prop['downpayment_pct']}%)", money(prop, c["downpayment_amount"])),
            cls="mt-3 space-y-1.5 rounded-xl bg-stone-50 p-3"),
        Div(Button(Icon("send", "h-4 w-4"), "Send request", type="button", cls=BTN_ACCENT + " flex-1 !py-2",
                   hx_post=f"/chat/guest/confirm/{c['id']}", hx_target="closest [data-card]", hx_swap="outerHTML",
                   hx_disabled_elt="this"),
            Button("Change", type="button", cls=BTN_GHOST + " !py-2", onclick="sfDismiss(this,true)"),
            cls="mt-3 flex gap-2"),
        data_card="draft", cls=CARD_WRAP + " ring-2 !ring-teal-200")


def card_status(prop, c):
    tone = STATUS_TONE[c["status"]]
    return Div(Div(P(f"Request #{c['id']}", cls="text-xs font-bold text-slate-500"), Badge(c["status"].replace("_", " "), tone),
                   cls="flex items-center justify-between"),
               P(c["room_name"], cls="mt-1 text-sm font-bold"),
               P(f"{short_date(c['check_in'])} → {short_date(c['check_out'])} · {money(prop, c['total_amount'])}", cls="text-xs text-slate-600"),
               data_card="status", cls=CARD_WRAP)


OWNER_VERB = {"approve": ("Approve", BTN_ACCENT), "reject": ("Reject", BTN_DANGER), "cancel": ("Cancel stay", BTN_DANGER),
              "reopen": ("Reopen", BTN_PRIMARY), "update_room": ("Apply change", BTN_PRIMARY)}


def card_owner_action(prop, c):
    verb, btn = OWNER_VERB[c["action"]]
    if c["action"] == "update_room":
        ch, before = c["changes"], c["before"]
        lines = [_row("Room", c["room_name"])]
        if "rate_per_night" in ch:
            lines.append(_row("Rate", f"{money(prop, before['rate_per_night'])} → {money(prop, ch['rate_per_night'])}", True))
        if "status" in ch:
            lines.append(_row("Status", f"{before['status']} → {ch['status']}", True))
        title = f"Update {c['room_name']}"
    else:
        lines = [_row("Guest", c["guest_name"]), _row("Room", c["room_name"]),
                 _row("Dates", f"{short_date(c['check_in'])} → {short_date(c['check_out'])}"),
                 _row("Total", money(prop, c["total_amount"]), True)]
        if c.get("reason"):
            lines.append(_row("Why", c["reason"][:70]))
        title = f"{verb} booking #{c['booking_id']}"
    return Div(P(Icon("sparkles", "h-3.5 w-3.5"), title, cls="flex items-center gap-1.5 text-sm font-bold"),
               Div(*lines, cls="mt-2 space-y-1.5 rounded-xl bg-stone-50 p-3"),
               Div(Button(Icon("check", "h-4 w-4"), "Confirm", type="button", cls=btn + " flex-1 !py-2",
                          hx_post=f"/admin/chat/confirm/{c['id']}", hx_target="closest [data-card]", hx_swap="outerHTML",
                          hx_disabled_elt="this"),
                   Button("Dismiss", type="button", cls=BTN_GHOST + " !py-2", onclick="sfDismiss(this)"),
                   cls="mt-3 flex gap-2"),
               data_card="action", cls=CARD_WRAP)


def render_cards(conn, prop, cards):
    rooms_by_id = {r["id"]: r for r in db.list_rooms(conn, include_hidden=True)}
    out = []
    for c in cards:
        if c["type"] == "rooms":
            out.append(card_rooms(prop, c, rooms_by_id))
        elif c["type"] == "booking_draft":
            out.append(card_booking_draft(prop, c))
        elif c["type"] == "status":
            out.append(card_status(prop, c))
        elif c["type"] == "owner_action":
            out.append(card_owner_action(prop, c))
    return out


def result_card(ok: bool, title: str, body: str = ""):
    return Div(Div(Icon("check" if ok else "x", "h-4 w-4"),
                   cls="grid h-8 w-8 shrink-0 place-items-center rounded-full " + ("bg-emerald-500 text-white" if ok else "bg-rose-100 text-rose-700")),
               Div(P(title, cls="text-sm font-bold"), P(body, cls="text-xs text-slate-600") if body else None, cls="min-w-0"),
               data_card="result", cls=CARD_WRAP + " flex items-start gap-3" + (" ring-2 !ring-emerald-200" if ok else ""))


@rt("/chat/guest")
async def post(req, sess, message: str = ""):
    with db.connect(tenant(req)) as conn:
        prop = db.get_property(conn)
        answer, cards = await ai.guest_reply(conn, tenant(req), chat_id(sess), message)
        return (chat_bubble("user", message.strip()[:ai.MAX_USER_CHARS]), chat_bubble("assistant", answer),
                *render_cards(conn, prop, cards))


@rt("/chat/guest/confirm/{aid}")
def post(req, sess, aid: str):
    with db.connect(tenant(req)) as conn:
        prop = db.get_property(conn)
        ok, ref, bid = ai.confirm_guest_request(conn, tenant(req), chat_id(sess), aid)
        if not ok:
            return result_card(False, "Couldn't send the request", ref)
        b = db.get_booking(conn, bid)
    return result_card(True, f"Request sent · {ref}",
                       f"The owner will review it and message you at {b['guest_phone']} with "
                       f"{prop['downpayment_pct']}% downpayment instructions. Keep your reference to check the status here.")


# --------------------------------------------------------------------------- #
# Owner auth
# --------------------------------------------------------------------------- #

_LOGIN_WINDOW, _LOGIN_MAX = 15 * 60, 5
_login_failures: dict[tuple[str, str], list[float]] = {}


def client_ip(req) -> str:
    # Caddy overwrites X-Forwarded-For with the real client IP (it doesn't trust inbound XFF by default).
    xff = req.headers.get("x-forwarded-for", "")
    return xff.split(",")[-1].strip() if xff else (req.client.host if req.client else "?")


def _too_many_attempts(key) -> bool:
    now = time.monotonic()
    hits = [t for t in _login_failures.get(key, []) if now - t < _LOGIN_WINDOW]
    if hits:
        _login_failures[key] = hits
    else:
        _login_failures.pop(key, None)
    if len(_login_failures) > 10_000:
        _login_failures.clear()
    return len(hits) >= _LOGIN_MAX


def login_page(prop, error: str | None = None):
    return page(
        f"Owner login · {prop['name']}",
        Main(
            Div(Img(src=safe_url(prop["hero_image_url"]) or placeholder_img("hero", 1600, 1200), alt="",
                    cls="absolute inset-0 h-full w-full object-cover"),
                Div(cls="absolute inset-0 bg-gradient-to-t from-slate-950/90 via-slate-900/40 to-slate-900/10"),
                Div(P("Owner dashboard", cls="sf-glass inline-flex rounded-full px-3 py-1 text-xs font-semibold"),
                    H2(prop["name"], cls="mt-4 text-4xl font-extrabold tracking-tight"),
                    P("Audit requests, approve stays and send payment slips — all in one place.", cls="mt-2 max-w-sm text-white/75"),
                    cls="relative mt-auto"),
                cls="relative hidden overflow-hidden rounded-[2rem] p-10 text-white lg:flex lg:flex-col"),
            Div(Form(
                Span(prop["name"][:1].upper(), cls="grid h-12 w-12 place-items-center rounded-full bg-slate-900 text-lg font-extrabold text-white"),
                H1("Welcome back", cls="mt-6 text-3xl font-extrabold tracking-tight"),
                P(f"Sign in to manage {prop['name']}.", cls="mt-1 text-sm text-slate-500"),
                Div(Icon("x", "h-4 w-4 shrink-0"), error, role="alert",
                    cls="mt-5 flex items-center gap-2 rounded-2xl bg-rose-50 p-3 text-sm font-medium text-rose-800 ring-1 ring-rose-200") if error else None,
                Div(Field("Password", Input(type="password", name="password", required=True,
                                            autocomplete="current-password", autofocus=True, cls=INPUT)), cls="mt-6"),
                Button("Sign in", Icon("arrow-right", "h-4 w-4"), cls=BTN_PRIMARY + " mt-4 w-full !py-3.5"),
                A("← Back to storefront", href="/", cls="mt-6 block text-center text-xs font-semibold text-slate-500 hover:text-slate-900"),
                method="post", action="/admin/login", cls="w-full max-w-sm"),
                cls="flex items-center justify-center rounded-[2rem] bg-white p-8 shadow-[0_8px_30px_-12px_rgba(15,23,42,0.12)] ring-1 ring-stone-200/70 sm:p-12"),
            cls="mx-auto grid min-h-screen max-w-6xl gap-4 p-3 sm:p-4 lg:grid-cols-2",
        ),
    )


@rt("/admin/login")
def get(req, sess):
    if sess.get("admin") == tenant(req):
        return RedirectResponse("/admin", status_code=303)
    with db.connect(tenant(req)) as conn:
        return login_page(db.get_property(conn))


@rt("/admin/login")
def post(req, sess, password: str = ""):
    t = tenant(req)
    key = (t, client_ip(req))
    with db.connect(t) as conn:
        prop = db.get_property(conn)
    if _too_many_attempts(key):
        return login_page(prop, "Too many attempts. Try again in 15 minutes.")
    if not db.verify_password(password, prop["admin_password_hash"]):
        _login_failures.setdefault(key, []).append(time.monotonic())
        return login_page(prop, "Wrong password.")
    _login_failures.pop(key, None)
    sess.clear()
    sess["admin"] = t
    return RedirectResponse("/admin", status_code=303)


@rt("/admin/logout")
def post(sess):
    sess.clear()
    return RedirectResponse("/admin/login", status_code=303)


# --------------------------------------------------------------------------- #
# Owner dashboard
# --------------------------------------------------------------------------- #

def short_date(iso: str) -> str:
    return date.fromisoformat(iso).strftime("%a %d %b")


def sms_href(phone: str) -> str:
    return "sms:" + "".join(c for c in phone if c.isdigit() or c == "+")


ADMIN_TARGET = dict(hx_target="#admin-main", hx_swap="outerHTML")


def booking_card(conn, prop, b, today: str):
    """One booking as a card (works on phones and desktops). Shows SQL-overlap conflicts inline."""
    st = b["status"]
    pending, approved = st == "pending_audit", st == "approved"
    hard = soft = []
    if pending:
        hard = db.find_overlaps(conn, b["room_id"], b["check_in"], b["check_out"], exclude_booking_id=b["id"])
        soft = db.find_overlaps(conn, b["room_id"], b["check_in"], b["check_out"],
                                statuses=("pending_audit",), exclude_booking_id=b["id"])
    expired = pending and b["check_in"] < today
    in_house = approved and b["check_in"] <= today < b["check_out"]
    arriving = approved and b["check_in"] == today

    def act(label, action, cls, icon=None, confirm=None, disabled=False):
        return Button(Icon(icon, "h-4 w-4") if icon else None, label, type="button", cls=cls + " !px-4 !py-2",
                      disabled=disabled, hx_post=f"/admin/bookings/{b['id']}/{action}", hx_confirm=confirm, **ADMIN_TARGET)

    actions = []
    if pending:
        actions += [act("Approve", "approve", BTN_ACCENT, "check", disabled=bool(hard) or expired),
                    act("Reject", "reject", BTN_DANGER, "x", confirm=f"Reject request #{b['id']} from {b['guest_name']}?")]
    elif approved:
        actions += [A(Icon("document", "h-4 w-4"), "Payment slip", href=f"/admin/bookings/{b['id']}/slip", cls=BTN_PRIMARY + " !px-4 !py-2"),
                    act("Cancel", "cancel", BTN_DANGER, None, confirm=f"Cancel approved stay #{b['id']}? The dates open up again.")]
    elif st == "rejected" and b["check_in"] >= today:
        actions.append(act("Reopen", "reopen", BTN_GHOST, "undo"))

    flags = []
    if hard:
        flags.append(P(Icon("x", "h-3.5 w-3.5 shrink-0"), "Clashes with approved " + ", ".join(
            f"#{c['id']} ({short_date(c['check_in'])} → {short_date(c['check_out'])})" for c in hard),
            cls="flex items-center gap-1.5 rounded-xl bg-rose-50 px-3 py-2 text-xs font-semibold text-rose-700"))
    if soft:
        flags.append(P(Icon("sparkles", "h-3.5 w-3.5 shrink-0"), "Overlaps other pending " + ", ".join(f"#{c['id']}" for c in soft)
                       + " — approving this one will block them.",
                       cls="flex items-center gap-1.5 rounded-xl bg-amber-50 px-3 py-2 text-xs font-semibold text-amber-800"))
    if expired:
        flags.append(P(Icon("clock", "h-3.5 w-3.5 shrink-0"), "Check-in date has passed — reject or ask the guest for new dates.",
                       cls="flex items-center gap-1.5 rounded-xl bg-stone-100 px-3 py-2 text-xs font-semibold text-slate-600"))

    tag = (Badge("In-house", "green") if in_house else Badge("Arrives today", "teal") if arriving
           else Badge(st.replace("_", " "), STATUS_TONE[st]))
    contact = Div(A(Icon("phone", "h-3.5 w-3.5"), "Call", href=tel_href(b["guest_phone"]),
                    cls="inline-flex items-center gap-1 rounded-full bg-stone-100 px-2.5 py-1 text-[11px] font-semibold text-slate-700 hover:bg-stone-200"),
                  A(Icon("chat", "h-3.5 w-3.5"), "Text", href=sms_href(b["guest_phone"]),
                    cls="inline-flex items-center gap-1 rounded-full bg-stone-100 px-2.5 py-1 text-[11px] font-semibold text-slate-700 hover:bg-stone-200"),
                  cls="flex gap-1.5")
    return Article(
        Div(Div(Span(b["guest_name"][:1].upper(), cls="grid h-11 w-11 shrink-0 place-items-center rounded-full bg-slate-900 font-bold text-white"),
                Div(P(b["guest_name"], cls="truncate font-bold text-slate-900"),
                    P(b["guest_phone"], (" · " + b["guest_email"]) if b["guest_email"] else "", cls="truncate text-xs text-slate-500"),
                    cls="min-w-0"),
                cls="flex min-w-0 items-center gap-3"),
            Div(tag, Span(f"#{b['id']}", cls="text-xs font-semibold text-slate-400"), cls="flex shrink-0 items-center gap-2"),
            cls="flex items-start justify-between gap-3"),
        Div(Div(P("Room", cls="text-[11px] font-semibold uppercase tracking-wider text-slate-400"),
                P(b["room_name"], cls="mt-0.5 text-sm font-bold text-slate-900")),
            Div(P("Dates", cls="text-[11px] font-semibold uppercase tracking-wider text-slate-400"),
                P(f"{short_date(b['check_in'])} → {short_date(b['check_out'])}", cls="mt-0.5 text-sm font-bold text-slate-900"),
                P(f"{b['nights']} night{'s' if b['nights'] != 1 else ''} · {b['guests']} guest{'s' if b['guests'] != 1 else ''}",
                  cls="text-xs text-slate-500")),
            Div(P("Total", cls="text-[11px] font-semibold uppercase tracking-wider text-slate-400"),
                P(money(prop, b["total_amount"]), cls="mt-0.5 text-sm font-bold text-slate-900"),
                P(f"Down {money(prop, b['downpayment_amount'])}", cls="text-xs text-slate-500")),
            cls="mt-4 grid grid-cols-2 gap-3 rounded-2xl bg-stone-50 p-3.5 sm:grid-cols-3"),
        P(Icon("chat", "mr-1.5 inline h-3.5 w-3.5"), b["notes"], cls="mt-3 text-sm italic text-slate-600") if b["notes"] else None,
        Div(*flags, cls="mt-3 space-y-2") if flags else None,
        Div(contact, Div(*actions, cls="flex flex-wrap justify-end gap-2") if actions else None,
            cls="mt-4 flex flex-wrap items-center justify-between gap-3"),
        id=f"booking-{b['id']}",
        cls="rounded-[1.5rem] bg-white p-4 ring-1 ring-stone-200/70 sm:p-5" + (" opacity-70" if expired else ""),
    )


def admin_section(icon, title, sub, *content, count=None, id=None, tone=0, extra=None):
    return Section(
        Div(Div(IconTile(icon, tone, "h-10 w-10", "h-5 w-5"),
                Div(H2(title, Span(str(count), cls="ml-2 rounded-full bg-stone-100 px-2 py-0.5 align-middle text-xs font-bold text-slate-600")
                       if count is not None else None, cls="text-lg font-extrabold tracking-tight"),
                    P(sub, cls="text-xs text-slate-500") if sub else None),
                cls="flex items-center gap-3"),
            extra, cls="flex flex-wrap items-center justify-between gap-3"),
        Div(*content, cls="mt-4 space-y-3"),
        id=id, cls="scroll-mt-28 rounded-[2rem] bg-white/60 p-4 ring-1 ring-stone-200/70 sm:p-6",
    )


def empty_state(icon, text):
    return Div(Span(Icon(icon, "h-6 w-6"), cls="grid h-12 w-12 place-items-center rounded-full bg-stone-100 text-slate-400"),
               P(text, cls="mt-3 text-sm font-medium text-slate-500"),
               cls="flex flex-col items-center rounded-[1.5rem] border-2 border-dashed border-stone-200 py-10 text-center")


ROOM_STATUS_LABEL = {"available": "Available", "maintenance": "Maintenance", "hidden": "Hidden"}


def room_editor(r, values: dict | None = None, error: str | None = None):
    """Full edit form for one room (inside a <details>)."""
    v = {k: r[k] for k in ("name", "rate_per_night", "capacity", "amenities", "description", "image_url", "sort_order")}
    v.update(values or {})
    return Form(
        Div(Field("Name", Input(name="name", value=v["name"], required=True, maxlength="80", cls=INPUT)),
            Field("Rate / night", Input(name="rate_per_night", value=str(v["rate_per_night"]), type="number", min="0",
                                        inputmode="numeric", required=True, cls=INPUT)),
            Field("Max guests", Input(name="capacity", value=str(v["capacity"]), type="number", min="1", max="50", required=True, cls=INPUT)),
            Field("Display order", Input(name="sort_order", value=str(v["sort_order"]), type="number", cls=INPUT)),
            cls="grid grid-cols-2 gap-3"),
        Field("Amenities (comma-separated)", Input(name="amenities", value=v["amenities"], maxlength="300", cls=INPUT)),
        Field("Description", Textarea(v["description"], name="description", rows="3", maxlength="1000", cls=INPUT)),
        Field("Photo URL", Input(name="image_url", value=v["image_url"], type="url", maxlength="500", placeholder="https://…", cls=INPUT)),
        Input(type="hidden", name="status", value=values.get("status", r["status"]) if values else r["status"]),
        P(error, role="alert", cls="rounded-xl bg-rose-50 px-3 py-2 text-sm font-medium text-rose-700") if error else None,
        Div(Button("Delete room", Icon("trash", "h-4 w-4"), type="button", cls=BTN_DANGER + " !px-4 !py-2",
                   hx_post=f"/admin/rooms/{r['id']}/delete", hx_confirm=f"Delete {r['name']}? This can't be undone.", **ADMIN_TARGET),
            Button("Save changes", cls=BTN_PRIMARY + " !px-5 !py-2"),
            cls="flex flex-wrap justify-between gap-2"),
        hx_post=f"/admin/rooms/{r['id']}/edit", method="post", action=f"/admin/rooms/{r['id']}/edit", **ADMIN_TARGET,
        cls="mt-3 space-y-3 border-t border-stone-100 pt-4",
    )


def room_card_admin(prop, r, edit_error: str | None = None, edit_values: dict | None = None):
    status_tone = {"available": "bg-emerald-500", "maintenance": "bg-amber-500", "hidden": "bg-slate-400"}[r["status"]]
    return Article(
        Div(Div(room_media(r), cls="h-[6.5rem] w-20 shrink-0 overflow-hidden rounded-2xl bg-stone-200 sm:w-28"),
            Div(Div(H3(r["name"], cls="truncate font-bold text-slate-900"),
                    Span(Span(cls=f"mr-1.5 inline-block h-1.5 w-1.5 rounded-full {status_tone}"), ROOM_STATUS_LABEL[r["status"]],
                         cls="inline-flex shrink-0 items-center rounded-full bg-stone-100 px-2 py-0.5 text-[11px] font-bold text-slate-600"),
                    cls="flex items-center justify-between gap-2"),
                P(f"Up to {r['capacity']} guests · #{r['id']}", cls="text-xs text-slate-500"),
                # Quick rate + status update
                Form(Label(Span(prop["currency"] if prop["currency"] != "PHP" else "₱",
                                cls="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-sm font-semibold text-slate-400"),
                           Input(type="number", name="rate_per_night", value=str(r["rate_per_night"]), min="0", step="1", required=True,
                                 inputmode="numeric", aria_label="Rate per night", cls=INPUT + " !min-h-[2.5rem] !py-1.5 !pl-7"),
                           cls="relative block w-[6.5rem] shrink-0"),
                     Select(*[Option(ROOM_STATUS_LABEL[s], value=s, selected=s == r["status"]) for s in db.ROOM_STATUSES],
                            name="status", aria_label="Status", cls=INPUT + " !min-h-[2.5rem] min-w-0 flex-1 !py-1.5 !pl-3"),
                     Button("Save", cls=BTN_PRIMARY + " shrink-0 !px-4 !py-2"),
                     hx_post=f"/admin/rooms/{r['id']}", method="post", action=f"/admin/rooms/{r['id']}", **ADMIN_TARGET,
                     cls="mt-3 flex items-center gap-2"),
                cls="min-w-0 flex-1"),
            cls="flex gap-3 sm:gap-4"),
        Details(Summary(Icon("pencil", "h-4 w-4"), "Edit details",
                        cls="mt-3 inline-flex cursor-pointer list-none items-center gap-1.5 text-sm font-semibold text-slate-600 hover:text-slate-900 [&::-webkit-details-marker]:hidden"),
                room_editor(r, edit_values, edit_error), open=bool(edit_error)),
        id=f"room-{r['id']}",
        cls="rounded-[1.5rem] bg-white p-4 ring-1 ring-stone-200/70" + (" ring-2 !ring-rose-300" if edit_error else ""),
    )


def add_room_form(values: dict | None = None, error: str | None = None):
    v = values or {}
    return Details(
        Summary(Icon("plus", "h-4 w-4"), "Add a room",
                cls=BTN_GHOST + " cursor-pointer list-none [&::-webkit-details-marker]:hidden"),
        Form(
            Div(Field("Name", Input(name="name", value=v.get("name", ""), required=True, maxlength="80", cls=INPUT)),
                Field("Rate / night", Input(name="rate_per_night", value=v.get("rate_per_night", ""), type="number", min="0",
                                            inputmode="numeric", required=True, cls=INPUT)),
                Field("Max guests", Input(name="capacity", value=v.get("capacity", "2"), type="number", min="1", max="50", required=True, cls=INPUT)),
                Field("Amenities (comma-separated)", Input(name="amenities", value=v.get("amenities", ""), maxlength="300", cls=INPUT)),
                cls="grid gap-3 sm:grid-cols-2"),
            Field("Description", Textarea(v.get("description", ""), name="description", rows="2", maxlength="1000", cls=INPUT)),
            Field("Photo URL (optional)", Input(name="image_url", value=v.get("image_url", ""), type="url", maxlength="500",
                                                placeholder="https://…", cls=INPUT)),
            P(error, role="alert", cls="rounded-xl bg-rose-50 px-3 py-2 text-sm font-medium text-rose-700") if error else None,
            Button(Icon("plus", "h-4 w-4"), "Add room", cls=BTN_PRIMARY),
            hx_post="/admin/rooms", method="post", action="/admin/rooms", **ADMIN_TARGET,
            cls="mt-4 space-y-3 rounded-[1.5rem] bg-white p-4 ring-1 ring-stone-200/70",
        ),
        open=bool(error), cls="mt-4",
    )


def Stat(label, value, icon, tone, sub=None):
    return Div(Div(IconTile(icon, tone, "h-10 w-10", "h-5 w-5"), P(label, cls="text-xs font-semibold text-slate-500"),
                   cls="flex items-center gap-3"),
               P(str(value), cls="mt-3 text-3xl font-extrabold tracking-tight text-slate-900"),
               P(sub, cls="text-xs text-slate-400") if sub else None,
               cls="rounded-[1.5rem] bg-white p-4 ring-1 ring-stone-200/70 sm:p-5")


def flash_box(flash):
    if not flash:
        return None
    text, tone = flash
    style = {"ok": "bg-emerald-600 text-white", "err": "bg-rose-600 text-white", "info": "bg-slate-900 text-white"}[tone]
    return Div(Icon("check" if tone == "ok" else "x" if tone == "err" else "sparkles", "h-4 w-4 shrink-0"), Span(text),
               role="status", aria_live="polite",
               cls=f"sf-bubble flex items-center gap-2 rounded-2xl px-4 py-3 text-sm font-semibold shadow-lg {style}")


def dashboard_body(conn, prop, flash=None, add_values=None, add_error=None, edit_room=None, edit_values=None, edit_error=None):
    rooms = db.list_rooms(conn, include_hidden=True)
    pending = db.list_bookings(conn, status="pending_audit")
    upcoming = db.list_bookings(conn, status="approved", upcoming_only=True)
    history = db.recent_decisions(conn)
    today = date.today().isoformat()
    in_house = [b for b in upcoming if b["check_in"] <= today < b["check_out"]]
    arrivals = [b for b in upcoming if b["check_in"] == today]
    week_end = date.fromordinal(date.today().toordinal() + 7).isoformat()
    next_week_revenue = sum(b["total_amount"] for b in upcoming if today <= b["check_in"] < week_end)
    hour = time.localtime().tm_hour
    greet = "Good morning" if hour < 12 else "Good afternoon" if hour < 18 else "Good evening"
    clashes = sum(1 for b in pending if db.find_overlaps(conn, b["room_id"], b["check_in"], b["check_out"], exclude_booking_id=b["id"]))
    return Main(
        Div(flash_box(flash), cls="sticky top-20 z-30 mb-4 empty:hidden") if flash else None,
        Div(Div(P(date.today().strftime("%A, %d %B %Y"), cls="text-xs font-semibold uppercase tracking-wider text-slate-400"),
                H1(f"{greet} 👋", cls="mt-1 text-[1.9rem] font-extrabold tracking-tight sm:text-4xl"),
                P(f"{len(pending)} request{'s' if len(pending) != 1 else ''} waiting"
                  + (f" · {len(arrivals)} arriving today" if arrivals else "") + (f" · {clashes} with clashes" if clashes else ""),
                  cls="mt-1 text-sm text-slate-500")),
            A(Icon("arrow-right", "h-4 w-4 -rotate-45"), "View storefront", href="/", target="_blank", cls=BTN_GHOST),
            cls="flex flex-wrap items-end justify-between gap-4"),
        Div(Stat("Pending requests", len(pending), "inbox", 1, f"{clashes} clash" + ("es" if clashes != 1 else "") if clashes else "Ready to review"),
            Stat("Upcoming stays", len(upcoming), "calendar", 0, f"{len(arrivals)} arriving today" if arrivals else None),
            Stat("In-house now", len(in_house), "home", 5),
            Stat("Next 7 days", money(prop, next_week_revenue), "wallet", 3, "approved revenue"),
            cls="mt-6 grid grid-cols-2 gap-3 lg:grid-cols-4"),
        Div(
            Div(admin_section("inbox", "Audit queue", "Approve runs a date-overlap check; clashing requests can't be approved.",
                              *([booking_card(conn, prop, b, today) for b in pending] or [empty_state("check", "No requests waiting. 🎉")]),
                              count=len(pending), id="queue", tone=1),
                admin_section("calendar", "Upcoming stays", "Approved bookings, soonest first.",
                              *([booking_card(conn, prop, b, today) for b in upcoming] or [empty_state("calendar", "No upcoming stays yet.")]),
                              count=len(upcoming), id="upcoming", tone=0),
                admin_section("clock", "Recent history", "Rejected, cancelled and completed stays.",
                              *([booking_card(conn, prop, b, today) for b in history] or [empty_state("clock", "Nothing here yet.")]),
                              id="history", tone=3),
                cls="space-y-6"),
            Div(admin_section("home", "Rooms & rates", "Hidden rooms are off the storefront; maintenance shows as unavailable.",
                              Div(*[room_card_admin(prop, r, edit_error if edit_room == r["id"] else None,
                                                    edit_values if edit_room == r["id"] else None) for r in rooms],
                                  cls="space-y-3") if rooms else empty_state("home", "No rooms yet — add your first one."),
                              add_room_form(add_values, add_error),
                              count=len(rooms), id="rooms", tone=5),
                cls="space-y-6"),
            cls="mt-6 grid grid-cols-1 items-start gap-6 lg:grid-cols-[minmax(0,1.35fr)_minmax(0,1fr)] [&>*]:min-w-0"),
        id="admin-main", hx_get="/admin/body", hx_trigger="sf-admin-refresh from:body", hx_swap="outerHTML",
        cls="mx-auto w-full max-w-7xl flex-1 px-3 pb-24 pt-6 sm:px-4 sm:pt-8",
    )


def admin_header(prop):
    link = "rounded-full px-3.5 py-2 text-sm font-semibold text-slate-600 transition hover:bg-stone-100 hover:text-slate-900"
    return Header(
        Div(A(Span(prop["name"][:1].upper(), cls="grid h-9 w-9 place-items-center rounded-full bg-slate-900 text-sm font-bold text-white"),
              Div(Span(prop["name"], cls="block truncate text-[15px] font-extrabold leading-tight tracking-tight"),
                  Span("Owner dashboard", cls="block text-[11px] font-semibold text-slate-400"), cls="min-w-0"),
              href="/admin", cls="flex min-w-0 items-center gap-2.5"),
            Nav(*[A(t, href=h, cls=link) for t, h in (("Queue", "/admin#queue"), ("Upcoming", "/admin#upcoming"),
                                                         ("Rooms", "/admin#rooms"), ("History", "/admin#history"))],
                cls="hidden items-center gap-0.5 rounded-full bg-stone-100/80 p-1 md:flex"),
            Form(Button(Icon("logout", "h-4 w-4"), Span("Log out", cls="hidden sm:inline"), aria_label="Log out",
                        cls=BTN_GHOST + " !px-3 !py-2"), method="post", action="/admin/logout"),
            cls="mx-auto flex max-w-7xl items-center justify-between gap-4 rounded-full bg-white/85 py-2 pl-2.5 pr-2 "
                "shadow-[0_8px_30px_-12px_rgba(15,23,42,0.18)] ring-1 ring-stone-200/80 backdrop-blur-xl"),
        cls="sticky top-0 z-40 px-3 pt-3 sm:px-4",
    )


def admin_page(prop, title, *content):
    return page(
        f"{title} · {prop['name']}", admin_header(prop), *content,
        chat_widget("/admin/chat", "Ops Agent", "Can act on bookings — you confirm",
                    "Hi! I can review requests, approve or reject them, change rates and room status, "
                    "or draft guest messages. I'll show a card for anything that changes data — nothing happens until you tap Confirm.",
                    "Tell the agent what to do…",
                    suggestions=("Approve all clash-free requests", "Which pending requests clash?",
                                 "Set Garden Cabin to available", "Draft a reminder for tomorrow's check-ins")),
    )


def admin_respond(req, conn, prop, **kw):
    """HTMX requests get the refreshed dashboard body; plain form posts get a full page."""
    body = dashboard_body(conn, prop, **kw)
    if req.headers.get("hx-request"):
        return body
    return admin_page(prop, "Dashboard", body)


@rt("/admin")
def get(req):
    with db.connect(tenant(req)) as conn:
        prop = db.get_property(conn)
        return admin_page(prop, "Dashboard", dashboard_body(conn, prop))


ACTION_DONE = {"reject": "Request #{id} rejected.", "cancel": "Stay #{id} cancelled — the dates are open again.",
               "reopen": "Request #{id} is back in the audit queue."}


@rt("/admin/bookings/{bid}/{action}")
def post(req, bid: int, action: str):
    if action not in db.TRANSITIONS:
        return Response("Not found", status_code=404)
    with db.connect(tenant(req)) as conn:
        prop = db.get_property(conn)
        try:
            if action == "approve":
                conflicts = db.approve_booking(conn, bid)
                flash = ((f"Can't approve #{bid}: it clashes with approved #" + ", #".join(str(c["id"]) for c in conflicts) + ".", "err")
                         if conflicts else (f"Stay #{bid} approved — open its payment slip to send instructions.", "ok"))
            else:
                db.transition_booking(conn, bid, action)
                flash = (ACTION_DONE[action].format(id=bid), "ok")
        except LookupError:
            flash = (f"Booking #{bid} doesn't exist.", "err")
        except db.TransitionError as e:
            flash = (str(e), "err")
        return admin_respond(req, conn, prop, flash=flash)


def slip_text(prop, b, slug: str) -> str:
    balance = b["total_amount"] - b["downpayment_amount"]
    pay = []
    if prop["gcash_number"]:
        pay.append(f"• GCash: {prop['gcash_number']} ({prop['gcash_name'] or prop['name']})")
    if prop["bank_account_number"]:
        pay.append(f"• {prop['bank_name'] or 'Bank'}: {prop['bank_account_number']} ({prop['bank_account_name'] or prop['name']})")
    if not pay:
        pay.append("• Payment details: please message us")
    return "\n".join([
        f"✅ STAY APPROVED — {prop['name']}",
        f"Booking ref: SF-{slug.upper()}-{b['id']}",
        "",
        f"Guest: {b['guest_name']} ({b['guests']} pax)",
        f"Room: {b['room_name']}",
        f"Check-in:  {nice_date(b['check_in'])}, {prop['check_in_time']}",
        f"Check-out: {nice_date(b['check_out'])}, {prop['check_out_time']}",
        f"Nights: {b['nights']} × {money(prop, b['rate_per_night'])} = {money(prop, b['total_amount'])}",
        "",
        f"DOWNPAYMENT DUE: {money(prop, b['downpayment_amount'])} ({prop['downpayment_pct']}%)",
        f"Balance on arrival: {money(prop, balance)}",
        "",
        "Pay via:",
        *pay,
        f"Please use reference SF-{slug.upper()}-{b['id']} and send a screenshot of your payment "
        "here to lock in your dates.",
        "",
        "Salamat po! See you soon. 🏝️",
    ])


@rt("/admin/bookings/{bid}/slip")
def get(req, bid: int):
    with db.connect(tenant(req)) as conn:
        prop = db.get_property(conn)
        b = db.get_booking(conn, bid)
    if b is None:
        return Response("Not found", status_code=404)
    if b["status"] != "approved":
        body = Div(empty_state("document", f"Booking #{bid} is {b['status'].replace('_', ' ')} — slips are only for approved stays."),
                   A("Back to dashboard", href="/admin", cls=BTN_GHOST + " mt-4"))
    else:
        text = slip_text(prop, b, tenant(req))
        body = Div(
            Pre(text, id="slip", cls="whitespace-pre-wrap rounded-[1.5rem] bg-stone-50 p-5 font-mono text-[13px] leading-relaxed text-slate-800 ring-1 ring-stone-200"),
            Div(Button(Icon("document", "h-4 w-4"), "Copy slip", cls=BTN_PRIMARY + " flex-1 sm:flex-none", type="button",
                       onclick="navigator.clipboard.writeText(document.getElementById('slip').innerText)"
                               ".then(()=>{this.lastChild.textContent='Copied ✓'})"),
                A(Icon("chat", "h-4 w-4"), "Text guest", href=sms_href(b["guest_phone"]) + "?body=" + urlquote(text),
                  cls=BTN_GHOST + " flex-1 sm:flex-none"),
                A("Back", href="/admin", cls=BTN_GHOST),
                cls="mt-4 flex flex-wrap gap-2"),
        )
    return admin_page(prop, f"Slip #{bid}",
                      Main(Section(Div(IconTile("document", 0, "h-10 w-10"),
                                       Div(H1(f"Payment slip · #{bid}", cls="text-xl font-extrabold tracking-tight"),
                                           P("Copy and paste into Messenger, Viber or SMS.", cls="text-xs text-slate-500")),
                                       cls="mb-5 flex items-center gap-3"),
                                   body, cls=CARD),
                           cls="mx-auto w-full max-w-2xl px-3 py-8 sm:px-4"))


def _room_form_values(form) -> dict:
    return {k: (form.get(k) or "") for k in ("name", "rate_per_night", "capacity", "amenities", "description",
                                              "image_url", "status", "sort_order")}


@rt("/admin/rooms/{rid}")
async def post(req, rid: int):
    """Quick rate + status update."""
    form = await req.form()
    with db.connect(tenant(req)) as conn:
        prop = db.get_property(conn)
        r = db.get_room(conn, rid)
        if r is None:
            return admin_respond(req, conn, prop, flash=(f"Room #{rid} doesn't exist.", "err"))
        try:
            rate = int(str(form.get("rate_per_night", "")).strip())
            if rate < 0:
                raise ValueError
            db.update_room(conn, rid, rate_per_night=rate, status=form.get("status", ""))
            flash = (f"{r['name']} saved — {money(prop, rate)} / night, {ROOM_STATUS_LABEL[form.get('status')].lower()}.", "ok")
        except (ValueError, KeyError):
            flash = (f"Couldn't save {r['name']}: enter a whole-number rate and pick a status.", "err")
        return admin_respond(req, conn, prop, flash=flash)


@rt("/admin/rooms/{rid}/edit")
async def post(req, rid: int):
    form = await req.form()
    values = _room_form_values(form)
    with db.connect(tenant(req)) as conn:
        prop = db.get_property(conn)
        if db.get_room(conn, rid) is None:
            return admin_respond(req, conn, prop, flash=(f"Room #{rid} doesn't exist.", "err"))
        try:
            db.update_room(conn, rid, **db.clean_room(**values))
        except ValueError as e:
            return admin_respond(req, conn, prop, flash=(str(e), "err"), edit_room=rid, edit_values=values, edit_error=str(e))
        return admin_respond(req, conn, prop, flash=(f"{values['name'].strip()} updated.", "ok"))


@rt("/admin/rooms/{rid}/delete")
def post(req, rid: int):
    with db.connect(tenant(req)) as conn:
        prop = db.get_property(conn)
        r = db.get_room(conn, rid)
        if r is None:
            return admin_respond(req, conn, prop, flash=(f"Room #{rid} doesn't exist.", "err"))
        try:
            db.delete_room(conn, rid)
            flash = (f"{r['name']} deleted.", "ok")
        except ValueError as e:
            flash = (str(e), "err")
        return admin_respond(req, conn, prop, flash=flash)


@rt("/admin/rooms")
async def post(req):
    form = await req.form()
    values = _room_form_values(form)
    values["status"] = "available"
    with db.connect(tenant(req)) as conn:
        prop = db.get_property(conn)
        try:
            clean = db.clean_room(**values)
            nxt = conn.execute("SELECT COALESCE(MAX(sort_order), 0) + 1 FROM rooms").fetchone()[0]
            clean["sort_order"] = nxt
            db.add_room(conn, **clean)
        except ValueError as e:
            return admin_respond(req, conn, prop, flash=(str(e), "err"), add_values=values, add_error=str(e))
        return admin_respond(req, conn, prop, flash=(f"{clean['name']} added to your rooms.", "ok"))


@rt("/admin/chat")
async def post(req, sess, message: str = ""):
    with db.connect(tenant(req)) as conn:
        prop = db.get_property(conn)
        answer, cards = await ai.owner_reply(conn, tenant(req), chat_id(sess), message)
        return chat_bubble("user", message.strip()[:1500]), chat_bubble("assistant", answer), *render_cards(conn, prop, cards)


@rt("/admin/chat/confirm/{aid}")
def post(req, sess, aid: str):
    with db.connect(tenant(req)) as conn:
        ok, msg = ai.confirm_owner_action(conn, tenant(req), chat_id(sess), aid)
    card = result_card(ok, "Done" if ok else "Not applied", msg)
    # Tell the dashboard to reload its data so stats and lists match what the agent just did.
    return card, HtmxResponseHeaders(trigger="sf-admin-refresh")


@rt("/admin/body")
def get(req):
    with db.connect(tenant(req)) as conn:
        return dashboard_body(conn, db.get_property(conn))


# --------------------------------------------------------------------------- #
# Infra
# --------------------------------------------------------------------------- #

@rt("/healthz")
def get():
    return PlainTextResponse("ok")


@rt("/_tls_check")
def get(domain: str = ""):
    """Caddy on-demand TLS gate: only issue certs for subdomains that are real tenants."""
    domain = domain.lower().strip(".")
    # Must be <slug>.BASE_DOMAIN exactly — never fall back to DEFAULT_TENANT here, or Caddy would
    # issue certs for any hostname someone points at this server.
    ok = (bool(BASE_DOMAIN) and domain.endswith("." + BASE_DOMAIN)
          and db.tenant_exists(tenant_from_host(domain)))
    return PlainTextResponse("ok" if ok else "no", status_code=200 if ok else 404)


if __name__ == "__main__":
    serve(port=int(os.environ.get("PORT", 5001)))
