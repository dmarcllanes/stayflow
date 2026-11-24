"""Create or update a tenant database from a JSON seed.

    uv run python seed_client.py seeds/demo.json            # create new tenant
    uv run python seed_client.py seeds/demo.json --update   # upsert property + rooms, keep bookings
    uv run python seed_client.py seeds/demo.json --reset    # DELETE the tenant DB and start fresh

Admin password source (first match): $STAYFLOW_ADMIN_PASSWORD, "admin_password" in the
JSON, or an interactive prompt. Only a PBKDF2 hash is stored.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

import db

PROPERTY_FIELDS = ("name", "tagline", "location", "contact_phone", "contact_email", "check_in_time",
                   "check_out_time", "house_rules", "currency", "downpayment_pct", "gcash_name",
                   "gcash_number", "bank_name", "bank_account_name", "bank_account_number",
                   "about", "highlights", "services", "faqs", "hero_image_url", "facebook_url",
                   "instagram_url", "messenger_url", "gallery", "announcements")
JSON_PROPERTY_FIELDS = ("highlights", "services", "faqs", "gallery", "announcements")
ROOM_FIELDS = ("name", "description", "capacity", "rate_per_night", "amenities", "image_url",
               "status", "sort_order")


def admin_password(seed: dict) -> str:
    pw = os.environ.get("STAYFLOW_ADMIN_PASSWORD") or seed.get("admin_password")
    if not pw:
        if not sys.stdin.isatty():
            sys.exit("Set STAYFLOW_ADMIN_PASSWORD (no TTY to prompt).")
        pw = getpass.getpass("Owner admin password: ")
        if pw != getpass.getpass("Repeat: "):
            sys.exit("Passwords don't match.")
    if len(pw) < 8:
        sys.exit("Admin password must be at least 8 characters.")
    return pw


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("seed", type=Path)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--update", action="store_true", help="upsert property/rooms, keep bookings")
    mode.add_argument("--reset", action="store_true", help="delete existing tenant DB first")
    args = ap.parse_args()

    seed = json.loads(args.seed.read_text())
    slug = seed.get("slug", "")
    if not db.valid_slug(slug):
        sys.exit(f"Invalid slug {slug!r}: use lowercase letters, digits and hyphens.")
    path = db.db_path(slug)

    if path.exists() and not (args.update or args.reset):
        sys.exit(f"{path} already exists. Use --update to upsert or --reset to wipe it.")
    if args.reset and path.exists():
        if input(f"Type '{slug}' to permanently delete {path}: ").strip() != slug:
            sys.exit("Aborted.")
        for p in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
            p.unlink(missing_ok=True)

    existing = path.exists()
    conn = db.create_tenant_db(slug)
    prop = {k: seed["property"][k] for k in PROPERTY_FIELDS if k in seed["property"]}
    for k in JSON_PROPERTY_FIELDS:
        if k in prop:
            prop[k] = json.dumps(prop[k], ensure_ascii=False)
    if "name" not in prop:
        sys.exit("property.name is required.")

    with db.transaction(conn):
        has_prop = conn.execute("SELECT 1 FROM property WHERE id=1").fetchone()
        if has_prop:
            sets = ", ".join(f"{k}=?" for k in prop)
            conn.execute(f"UPDATE property SET {sets} WHERE id=1", list(prop.values()))
        else:
            prop["admin_password_hash"] = db.hash_password(admin_password(seed))
            cols = ", ".join(prop)
            conn.execute(f"INSERT INTO property (id, {cols}) VALUES (1, {','.join('?' * len(prop))})",
                         list(prop.values()))

        for i, room in enumerate(seed.get("rooms", [])):
            r = {k: room[k] for k in ROOM_FIELDS if k in room}
            if isinstance(r.get("amenities"), list):
                r["amenities"] = ", ".join(r["amenities"])
            r.setdefault("sort_order", i)
            match = conn.execute("SELECT id FROM rooms WHERE name=?", (r["name"],)).fetchone()
            if match:
                conn.execute(f"UPDATE rooms SET {', '.join(f'{k}=?' for k in r)} WHERE id=?",
                             [*r.values(), match["id"]])
            else:
                conn.execute(f"INSERT INTO rooms ({', '.join(r)}) VALUES ({','.join('?' * len(r))})",
                             list(r.values()))

    n = conn.execute("SELECT COUNT(*) FROM rooms").fetchone()[0]
    conn.close()
    print(f"{'Updated' if existing else 'Created'} tenant '{slug}' at {path} ({n} rooms).")
    if os.environ.get("BASE_DOMAIN"):
        print(f"Storefront: https://{slug}.{os.environ['BASE_DOMAIN']}/   Admin: /admin")


if __name__ == "__main__":
    main()
