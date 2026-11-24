"""Multi-tenant SQLite layer for StayFlow.

One SQLite file per tenant at ``$DATA_DIR/<tenant_slug>.db``. Every connection
runs in WAL mode with foreign keys enforced. Dates are stored as ISO
``YYYY-MM-DD`` strings and treated as half-open intervals ``[check_in, check_out)``
so a check-out day can be someone else's check-in day.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterator

DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).parent / "data"))
SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$")

ROOM_STATUSES = ("available", "maintenance", "hidden")
BOOKING_STATUSES = ("pending_audit", "approved", "rejected", "cancelled")

SCHEMA = """
CREATE TABLE IF NOT EXISTS property (
    id                  INTEGER PRIMARY KEY CHECK (id = 1),
    name                TEXT    NOT NULL,
    tagline             TEXT    NOT NULL DEFAULT '',
    location            TEXT    NOT NULL DEFAULT '',
    contact_phone       TEXT    NOT NULL DEFAULT '',
    contact_email       TEXT    NOT NULL DEFAULT '',
    check_in_time       TEXT    NOT NULL DEFAULT '14:00',
    check_out_time      TEXT    NOT NULL DEFAULT '12:00',
    house_rules         TEXT    NOT NULL DEFAULT '',
    currency            TEXT    NOT NULL DEFAULT 'PHP',
    downpayment_pct     INTEGER NOT NULL DEFAULT 50 CHECK (downpayment_pct BETWEEN 0 AND 100),
    gcash_name          TEXT    NOT NULL DEFAULT '',
    gcash_number        TEXT    NOT NULL DEFAULT '',
    bank_name           TEXT    NOT NULL DEFAULT '',
    bank_account_name   TEXT    NOT NULL DEFAULT '',
    bank_account_number TEXT    NOT NULL DEFAULT '',
    admin_password_hash TEXT    NOT NULL,
    -- Storefront / business-profile content (all optional)
    about               TEXT    NOT NULL DEFAULT '',
    highlights          TEXT    NOT NULL DEFAULT '[]',  -- JSON list of strings
    services            TEXT    NOT NULL DEFAULT '[]',  -- JSON list of {title, description, price?, icon?}
    faqs                TEXT    NOT NULL DEFAULT '[]',  -- JSON list of {q, a}
    hero_image_url      TEXT    NOT NULL DEFAULT '',
    gallery             TEXT    NOT NULL DEFAULT '[]',  -- JSON list of {url, caption}
    announcements       TEXT    NOT NULL DEFAULT '[]',  -- JSON list of strings for the top ticker
    facebook_url        TEXT    NOT NULL DEFAULT '',
    instagram_url       TEXT    NOT NULL DEFAULT '',
    messenger_url       TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS rooms (
    id             INTEGER PRIMARY KEY,
    name           TEXT    NOT NULL,
    description    TEXT    NOT NULL DEFAULT '',
    capacity       INTEGER NOT NULL DEFAULT 2 CHECK (capacity > 0),
    rate_per_night INTEGER NOT NULL CHECK (rate_per_night >= 0),  -- whole currency units
    amenities      TEXT    NOT NULL DEFAULT '',                    -- comma-separated
    image_url      TEXT    NOT NULL DEFAULT '',
    status         TEXT    NOT NULL DEFAULT 'available'
                   CHECK (status IN ('available', 'maintenance', 'hidden')),
    sort_order     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS bookings (
    id                 INTEGER PRIMARY KEY,
    room_id            INTEGER NOT NULL REFERENCES rooms(id) ON DELETE RESTRICT,
    guest_name         TEXT    NOT NULL,
    guest_phone        TEXT    NOT NULL,
    guest_email        TEXT    NOT NULL DEFAULT '',
    guests             INTEGER NOT NULL CHECK (guests > 0),
    check_in           TEXT    NOT NULL,
    check_out          TEXT    NOT NULL,
    notes              TEXT    NOT NULL DEFAULT '',
    nights             INTEGER NOT NULL,
    rate_per_night     INTEGER NOT NULL,
    total_amount       INTEGER NOT NULL,
    downpayment_amount INTEGER NOT NULL,
    status             TEXT    NOT NULL DEFAULT 'pending_audit'
                       CHECK (status IN ('pending_audit', 'approved', 'rejected', 'cancelled')),
    created_at         TEXT    NOT NULL DEFAULT (datetime('now')),
    decided_at         TEXT,
    CHECK (check_out > check_in)
);

CREATE INDEX IF NOT EXISTS idx_bookings_overlap
    ON bookings (room_id, status, check_in, check_out);
CREATE INDEX IF NOT EXISTS idx_bookings_status ON bookings (status, created_at);
"""


# --------------------------------------------------------------------------- #
# Tenants & connections
# --------------------------------------------------------------------------- #

def valid_slug(slug: str | None) -> bool:
    return bool(slug) and bool(SLUG_RE.match(slug))


def db_path(slug: str) -> Path:
    if not valid_slug(slug):
        raise ValueError(f"invalid tenant slug: {slug!r}")
    return DATA_DIR / f"{slug}.db"


def tenant_exists(slug: str | None) -> bool:
    return valid_slug(slug) and db_path(slug).is_file()


def _open(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=5, isolation_level=None)  # autocommit; explicit BEGIN when needed
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


# Columns added after the first release: (name, DDL). Applied lazily to older tenant DBs.
_PROPERTY_MIGRATIONS = (
    ("about", "TEXT NOT NULL DEFAULT ''"),
    ("highlights", "TEXT NOT NULL DEFAULT '[]'"),
    ("services", "TEXT NOT NULL DEFAULT '[]'"),
    ("faqs", "TEXT NOT NULL DEFAULT '[]'"),
    ("hero_image_url", "TEXT NOT NULL DEFAULT ''"),
    ("facebook_url", "TEXT NOT NULL DEFAULT ''"),
    ("instagram_url", "TEXT NOT NULL DEFAULT ''"),
    ("messenger_url", "TEXT NOT NULL DEFAULT ''"),
    ("gallery", "TEXT NOT NULL DEFAULT '[]'"),
    ("announcements", "TEXT NOT NULL DEFAULT '[]'"),
)
_migrated: set[str] = set()


def _migrate(conn: sqlite3.Connection) -> None:
    have = {r["name"] for r in conn.execute("PRAGMA table_info(property)")}
    for col, ddl in _PROPERTY_MIGRATIONS:
        if have and col not in have:
            conn.execute(f"ALTER TABLE property ADD COLUMN {col} {ddl}")


@contextmanager
def connect(slug: str) -> Iterator[sqlite3.Connection]:
    """Open an existing tenant DB. Never creates files for unknown slugs."""
    path = db_path(slug)
    if not path.is_file():
        raise LookupError(f"unknown tenant: {slug}")
    conn = _open(path)
    if slug not in _migrated:  # once per process per tenant
        _migrate(conn)
        _migrated.add(slug)
    try:
        yield conn
    finally:
        conn.close()


def create_tenant_db(slug: str) -> sqlite3.Connection:
    """Create (or open) a tenant DB and ensure the schema exists. Used by seeding."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = _open(db_path(slug))
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection, immediate: bool = True):
    """BEGIN IMMEDIATE takes the write lock up front, so check-then-write is race-free."""
    conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


# --------------------------------------------------------------------------- #
# Passwords
# --------------------------------------------------------------------------- #

_PBKDF2_ITERS = 200_000


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ITERS)
    return f"pbkdf2_sha256${_PBKDF2_ITERS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_hex, dk_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(dk.hex(), dk_hex)
    except (ValueError, TypeError):
        return False


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #

def get_property(conn: sqlite3.Connection) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM property WHERE id = 1").fetchone()
    if row is None:
        raise LookupError("tenant has no property row; re-run seed_client.py")
    return row


def json_list(prop: sqlite3.Row, key: str) -> list:
    """Parse a JSON-list property column; tolerate bad/missing data."""
    try:
        val = json.loads(prop[key] or "[]")
    except (IndexError, KeyError, ValueError, TypeError):
        return []
    return val if isinstance(val, list) else []


def list_rooms(conn: sqlite3.Connection, include_hidden: bool = False) -> list[sqlite3.Row]:
    sql = "SELECT * FROM rooms"
    if not include_hidden:
        sql += " WHERE status != 'hidden'"
    return conn.execute(sql + " ORDER BY sort_order, id").fetchall()


def get_room(conn: sqlite3.Connection, room_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM rooms WHERE id = ?", (room_id,)).fetchone()


def get_booking(conn: sqlite3.Connection, booking_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """SELECT b.*, r.name AS room_name FROM bookings b
           JOIN rooms r ON r.id = b.room_id WHERE b.id = ?""",
        (booking_id,),
    ).fetchone()


def list_bookings(conn: sqlite3.Connection, status: str | None = None,
                  upcoming_only: bool = False, limit: int = 200) -> list[sqlite3.Row]:
    where, args = [], []
    if status:
        where.append("b.status = ?")
        args.append(status)
    if upcoming_only:
        where.append("b.check_out >= ?")
        args.append(date.today().isoformat())
    sql = """SELECT b.*, r.name AS room_name FROM bookings b
             JOIN rooms r ON r.id = b.room_id"""
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY b.check_in, b.id LIMIT ?"
    args.append(limit)
    return conn.execute(sql, args).fetchall()


def find_overlaps(conn: sqlite3.Connection, room_id: int, check_in: str, check_out: str,
                  statuses: tuple[str, ...] = ("approved",),
                  exclude_booking_id: int | None = None) -> list[sqlite3.Row]:
    """Bookings for ``room_id`` whose [check_in, check_out) intersects the given range.

    Two half-open ranges overlap iff ``a.start < b.end AND a.end > b.start``.
    """
    placeholders = ",".join("?" * len(statuses))
    sql = f"""SELECT * FROM bookings
              WHERE room_id = ?
                AND status IN ({placeholders})
                AND check_in < ?
                AND check_out > ?"""
    args: list = [room_id, *statuses, check_out, check_in]
    if exclude_booking_id is not None:
        sql += " AND id != ?"
        args.append(exclude_booking_id)
    return conn.execute(sql + " ORDER BY check_in", args).fetchall()


def booked_ranges(conn: sqlite3.Connection, horizon_days: int = 120) -> dict[int, list[tuple[str, str]]]:
    """Approved, upcoming date ranges per room. No guest PII — safe for the public concierge."""
    today = date.today()
    end = date.fromordinal(today.toordinal() + horizon_days).isoformat()
    rows = conn.execute(
        """SELECT room_id, check_in, check_out FROM bookings
           WHERE status = 'approved' AND check_out > ? AND check_in < ?
           ORDER BY check_in""",
        (today.isoformat(), end),
    ).fetchall()
    out: dict[int, list[tuple[str, str]]] = {}
    for r in rows:
        out.setdefault(r["room_id"], []).append((r["check_in"], r["check_out"]))
    return out


# --------------------------------------------------------------------------- #
# Writes
# --------------------------------------------------------------------------- #

class BookingError(ValueError):
    """A guest-facing validation problem."""


MAX_NIGHTS = 30
MAX_ADVANCE_DAYS = 365


def quote(rate_per_night: int, nights: int, downpayment_pct: int) -> tuple[int, int]:
    total = rate_per_night * nights
    return total, math.ceil(total * downpayment_pct / 100)


def validate_booking_request(conn: sqlite3.Connection, *, room_id: int, guest_name: str,
                             guest_phone: str, guests: int, check_in: str, check_out: str) -> dict:
    """All guest-facing checks for a stay request, without writing. Returns the priced request.

    Raises BookingError with a message that is safe to show to the guest.
    """
    try:
        d_in, d_out = date.fromisoformat(str(check_in)), date.fromisoformat(str(check_out))
    except ValueError:
        raise BookingError("Please pick valid check-in and check-out dates.")
    today = date.today()
    if d_in < today:
        raise BookingError("Check-in date is in the past.")
    if (d_in - today).days > MAX_ADVANCE_DAYS:
        raise BookingError("We only take requests up to a year ahead.")
    nights = (d_out - d_in).days
    if nights < 1:
        raise BookingError("Check-out must be after check-in.")
    if nights > MAX_NIGHTS:
        raise BookingError(f"For stays longer than {MAX_NIGHTS} nights, please contact us directly.")
    if not str(guest_name).strip() or not str(guest_phone).strip():
        raise BookingError("Name and mobile number are required.")
    if sum(c.isdigit() for c in str(guest_phone)) < 7:
        raise BookingError("Please give a mobile number we can reach you on.")
    try:
        guests = int(guests)
    except (TypeError, ValueError):
        raise BookingError("Number of guests must be a whole number.")
    if guests < 1:
        raise BookingError("Number of guests must be at least 1.")
    room = get_room(conn, int(room_id))
    if room is None or room["status"] != "available":
        raise BookingError("That room isn't open for booking right now.")
    if guests > room["capacity"]:
        raise BookingError(f"{room['name']} fits up to {room['capacity']} guests.")
    if find_overlaps(conn, room["id"], d_in.isoformat(), d_out.isoformat()):
        raise BookingError(f"{room['name']} is already booked for part of those dates. "
                           "Try other dates or another room.")
    total, down = quote(room["rate_per_night"], nights, get_property(conn)["downpayment_pct"])
    return dict(room_id=room["id"], room_name=room["name"], guests=guests, check_in=d_in.isoformat(),
                check_out=d_out.isoformat(), nights=nights, rate_per_night=room["rate_per_night"],
                total_amount=total, downpayment_amount=down)


def create_booking_request(conn: sqlite3.Connection, *, room_id: int, guest_name: str,
                           guest_phone: str, guest_email: str, guests: int,
                           check_in: str, check_out: str, notes: str) -> int:
    """Validate and insert a ``pending_audit`` booking. Raises BookingError on bad input."""
    with transaction(conn):  # validate under the write lock so the overlap check can't race
        v = validate_booking_request(conn, room_id=room_id, guest_name=guest_name, guest_phone=guest_phone,
                                     guests=guests, check_in=check_in, check_out=check_out)
        cur = conn.execute(
            """INSERT INTO bookings (room_id, guest_name, guest_phone, guest_email, guests,
                   check_in, check_out, notes, nights, rate_per_night, total_amount,
                   downpayment_amount, status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'pending_audit')""",
            (v["room_id"], str(guest_name).strip()[:120], str(guest_phone).strip()[:40],
             str(guest_email or "").strip()[:120], v["guests"], v["check_in"], v["check_out"],
             str(notes or "").strip()[:1000], v["nights"], v["rate_per_night"], v["total_amount"],
             v["downpayment_amount"]),
        )
        return cur.lastrowid


def availability(conn: sqlite3.Connection, check_in: str, check_out: str, guests: int | None = None) -> dict:
    """Per-room availability + price for a date range (no guest PII). Raises BookingError on bad dates."""
    try:
        d_in, d_out = date.fromisoformat(str(check_in)), date.fromisoformat(str(check_out))
    except ValueError:
        raise BookingError("Dates must look like YYYY-MM-DD.")
    if d_in < date.today():
        raise BookingError("Check-in date is in the past.")
    nights = (d_out - d_in).days
    if not 1 <= nights <= MAX_NIGHTS:
        raise BookingError(f"Stays must be 1–{MAX_NIGHTS} nights.")
    pct = get_property(conn)["downpayment_pct"]
    out = []
    for r in list_rooms(conn):
        reason = None
        if r["status"] != "available":
            reason = "under maintenance"
        elif guests and guests > r["capacity"]:
            reason = f"fits only {r['capacity']} guests"
        elif find_overlaps(conn, r["id"], d_in.isoformat(), d_out.isoformat()):
            reason = "already booked on some of these nights"
        total, down = quote(r["rate_per_night"], nights, pct)
        out.append(dict(room_id=r["id"], name=r["name"], capacity=r["capacity"], rate_per_night=r["rate_per_night"],
                        total=total, downpayment=down, available=reason is None, reason=reason))
    return dict(check_in=d_in.isoformat(), check_out=d_out.isoformat(), nights=nights, rooms=out)


def parse_booking_ref(ref: str) -> int | None:
    """'SF-DEMO-12', '#12' or '12' -> 12."""
    m = re.search(r"(\d+)\s*$", str(ref or "").strip())
    return int(m.group(1)) if m else None


def phones_match(a: str, b: str) -> bool:
    da, dbb = "".join(c for c in str(a) if c.isdigit()), "".join(c for c in str(b) if c.isdigit())
    return len(da) >= 7 and len(dbb) >= 7 and da[-7:] == dbb[-7:]


# Allowed status changes: action -> (required current status, new status).
TRANSITIONS = {
    "approve": ("pending_audit", "approved"),
    "reject": ("pending_audit", "rejected"),
    "cancel": ("approved", "cancelled"),
    "reopen": ("rejected", "pending_audit"),  # undo a reject; the request goes back to the queue
}


class TransitionError(ValueError):
    """An owner action that isn't allowed from the booking's current state."""


def approve_booking(conn: sqlite3.Connection, booking_id: int) -> list[sqlite3.Row]:
    """Approve a pending request if no approved stay overlaps.

    Returns the conflicting rows (empty on success). Raises LookupError / TransitionError.
    """
    with transaction(conn):
        b = conn.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()
        if b is None:
            raise LookupError("booking not found")
        if b["status"] != "pending_audit":
            raise TransitionError(f"Booking #{booking_id} is {b['status'].replace('_', ' ')}; only pending requests can be approved.")
        if b["check_in"] < date.today().isoformat():
            raise TransitionError(f"Booking #{booking_id} has a check-in date in the past. Reject it or ask the guest for new dates.")
        room = get_room(conn, b["room_id"])
        if room is None or room["status"] == "hidden":
            raise TransitionError(f"The room for booking #{booking_id} is no longer listed.")
        conflicts = find_overlaps(conn, b["room_id"], b["check_in"], b["check_out"],
                                  exclude_booking_id=booking_id)
        if conflicts:
            return conflicts
        conn.execute("UPDATE bookings SET status='approved', decided_at=datetime('now') "
                     "WHERE id=? AND status='pending_audit'", (booking_id,))
        return []


def transition_booking(conn: sqlite3.Connection, booking_id: int, action: str) -> sqlite3.Row:
    """Apply a non-approve action (reject / cancel / reopen) guarded by the current status."""
    if action == "approve":
        raise ValueError("use approve_booking() to approve")
    if action not in TRANSITIONS:
        raise ValueError(f"unknown action: {action}")
    need, new = TRANSITIONS[action]
    with transaction(conn):
        b = conn.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()
        if b is None:
            raise LookupError("booking not found")
        if b["status"] != need:
            raise TransitionError(f"Can't {action} booking #{booking_id}: it is {b['status'].replace('_', ' ')}.")
        conn.execute("UPDATE bookings SET status=?, decided_at=? WHERE id=? AND status=?",
                     (new, None if new == "pending_audit" else _now(), booking_id, need))
    return get_booking(conn, booking_id)


def _now() -> str:
    """UTC timestamp in the same format as SQLite's datetime('now')."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def set_booking_status(conn: sqlite3.Connection, booking_id: int, status: str) -> None:
    """Back-compat wrapper (seed/ops scripts). Prefer transition_booking()."""
    action = {"rejected": "reject", "cancelled": "cancel", "pending_audit": "reopen"}.get(status)
    if action is None:
        raise ValueError("use approve_booking() to approve")
    transition_booking(conn, booking_id, action)


def recent_decisions(conn: sqlite3.Connection, limit: int = 15) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT b.*, r.name AS room_name FROM bookings b JOIN rooms r ON r.id = b.room_id
           WHERE b.status IN ('rejected', 'cancelled')
              OR (b.status = 'approved' AND b.check_out < date('now'))
           ORDER BY COALESCE(b.decided_at, b.created_at) DESC LIMIT ?""", (limit,)).fetchall()


def booking_counts(conn: sqlite3.Connection, room_id: int) -> int:
    return conn.execute("SELECT COUNT(*) FROM bookings WHERE room_id=?", (room_id,)).fetchone()[0]


# --------------------------------------------------------------------------- #
# Rooms
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# Uploaded photos: $DATA_DIR/uploads/<tenant>/<random>.<ext>, served at /media/<file>
# --------------------------------------------------------------------------- #

MEDIA_PREFIX = "/media/"
MAX_UPLOAD_BYTES = 8 * 1024 * 1024
_MEDIA_NAME_RE = re.compile(r"^[a-f0-9]{32}\.(jpg|png|webp|gif)$")
MEDIA_TYPES = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp", "gif": "image/gif"}


def _sniff_image(data: bytes) -> str | None:
    """File type from the bytes themselves (never trust the filename or browser's content-type)."""
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    return None


def is_media_url(url: str) -> bool:
    return bool(url) and url.startswith(MEDIA_PREFIX) and bool(_MEDIA_NAME_RE.match(url[len(MEDIA_PREFIX):]))


def media_path(slug: str, name: str) -> Path | None:
    if not valid_slug(slug) or not _MEDIA_NAME_RE.match(name):
        return None
    return DATA_DIR / "uploads" / slug / name


def save_upload(slug: str, data: bytes) -> str:
    """Store an uploaded image for a tenant and return its /media/ URL. Raises ValueError for bad files."""
    if not data:
        raise ValueError("The photo file is empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError(f"That photo is too big (max {MAX_UPLOAD_BYTES // 1024 // 1024} MB).")
    ext = _sniff_image(data)
    if ext is None:
        raise ValueError("That file isn't a supported photo. Use JPG, PNG, WebP or GIF.")
    folder = DATA_DIR / "uploads" / slug
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{secrets.token_hex(16)}.{ext}"
    tmp = folder / (name + ".part")
    tmp.write_bytes(data)
    tmp.replace(folder / name)  # atomic: never serve a half-written file
    return MEDIA_PREFIX + name


def delete_upload(slug: str, url: str) -> None:
    """Remove a tenant's uploaded photo (no-op for external links or unknown files)."""
    if is_media_url(url):
        path = media_path(slug, url[len(MEDIA_PREFIX):])
        if path:
            path.unlink(missing_ok=True)


def clean_room(*, name: str, rate_per_night, capacity, description: str = "", amenities: str = "",
               image_url: str = "", status: str = "available", sort_order=0) -> dict:
    """Validate owner room input. Raises ValueError with an owner-facing message."""
    name = (name or "").strip()
    if not name:
        raise ValueError("Room name is required.")
    try:
        rate = int(str(rate_per_night).replace(",", "").strip())
    except ValueError:
        raise ValueError("Rate must be a whole number, e.g. 2500.")
    if rate < 0:
        raise ValueError("Rate can't be negative.")
    try:
        cap = int(str(capacity).strip())
    except ValueError:
        raise ValueError("Max guests must be a whole number.")
    if not 1 <= cap <= 50:
        raise ValueError("Max guests must be between 1 and 50.")
    if status not in ROOM_STATUSES:
        raise ValueError("Pick a valid status.")
    image_url = (image_url or "").strip()
    if image_url and not is_media_url(image_url) and not image_url.startswith(("https://", "http://")):
        raise ValueError("Image URL must start with https://")
    try:
        order = int(str(sort_order or 0).strip())
    except ValueError:
        order = 0
    amen = ", ".join(a.strip() for a in (amenities or "").split(",") if a.strip())
    return dict(name=name[:80], rate_per_night=rate, capacity=cap, description=(description or "").strip()[:1000],
                amenities=amen[:300], image_url=image_url[:500], status=status, sort_order=order)


def update_room(conn: sqlite3.Connection, room_id: int, **fields) -> None:
    """Update any subset of room columns (already validated by clean_room or the quick form)."""
    allowed = {"name", "description", "capacity", "rate_per_night", "amenities", "image_url", "status", "sort_order"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if "status" in fields and fields["status"] not in ROOM_STATUSES:
        raise ValueError("bad room status")
    if "rate_per_night" in fields and int(fields["rate_per_night"]) < 0:
        raise ValueError("rate must be >= 0")
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE rooms SET {sets} WHERE id=?", (*fields.values(), room_id))


def delete_room(conn: sqlite3.Connection, room_id: int) -> None:
    """Delete a room with no booking history; otherwise the owner should hide it."""
    with transaction(conn):
        if booking_counts(conn, room_id):
            raise ValueError("This room has bookings on record. Set it to “hidden” instead of deleting it.")
        conn.execute("DELETE FROM rooms WHERE id=?", (room_id,))


# --------------------------------------------------------------------------- #
# Storefront / property settings (owner-editable)
# --------------------------------------------------------------------------- #

PROPERTY_EDITABLE = {
    "name", "tagline", "location", "about", "contact_phone", "contact_email", "check_in_time", "check_out_time",
    "house_rules", "currency", "downpayment_pct", "gcash_name", "gcash_number", "bank_name", "bank_account_name",
    "bank_account_number", "hero_image_url", "facebook_url", "instagram_url", "messenger_url",
    "highlights", "services", "faqs", "gallery", "announcements",
}
PROPERTY_JSON = {"highlights", "services", "faqs", "gallery", "announcements"}
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _url(value: str, label: str) -> str:
    value = (value or "").strip()
    if value and not value.startswith(("https://", "http://")):
        raise ValueError(f"{label} must start with https://")
    return value[:500]


def clean_property(fields: dict) -> dict:
    """Validate owner storefront edits. Only keys present in ``fields`` are cleaned/returned.

    Raises ValueError with an owner-facing message.
    """
    out: dict = {}
    text_limits = {"name": 80, "tagline": 160, "location": 160, "about": 4000, "contact_phone": 40,
                   "house_rules": 2000, "gcash_name": 80, "gcash_number": 40, "bank_name": 80,
                   "bank_account_name": 80, "bank_account_number": 60}
    for k, limit in text_limits.items():
        if k in fields:
            out[k] = str(fields[k] or "").strip()[:limit]
    if "name" in out and not out["name"]:
        raise ValueError("Property name is required.")
    if "contact_email" in fields:
        email = str(fields["contact_email"] or "").strip()
        if email and not _EMAIL_RE.match(email):
            raise ValueError("Contact email doesn't look right.")
        out["contact_email"] = email[:120]
    for k, label in (("check_in_time", "Check-in time"), ("check_out_time", "Check-out time")):
        if k in fields:
            t = str(fields[k] or "").strip()
            if not _TIME_RE.match(t):
                raise ValueError(f"{label} must look like 14:00.")
            out[k] = t
    if "currency" in fields:
        cur = str(fields["currency"] or "").strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", cur):
            raise ValueError("Currency must be a 3-letter code like PHP.")
        out["currency"] = cur
    if "downpayment_pct" in fields:
        try:
            pct = int(str(fields["downpayment_pct"]).strip())
        except ValueError:
            raise ValueError("Downpayment must be a whole number from 0 to 100.")
        if not 0 <= pct <= 100:
            raise ValueError("Downpayment must be between 0 and 100%.")
        out["downpayment_pct"] = pct
    for k, label in (("hero_image_url", "Hero photo URL"), ("facebook_url", "Facebook link"),
                     ("instagram_url", "Instagram link"), ("messenger_url", "Messenger link")):
        if k in fields:
            out[k] = _url(fields[k], label)
    # Lists
    if "highlights" in fields:
        out["highlights"] = [str(h).strip()[:120] for h in fields["highlights"] if str(h).strip()][:12]
    if "announcements" in fields:
        out["announcements"] = [str(a).strip()[:140] for a in fields["announcements"] if str(a).strip()][:10]
    if "services" in fields:
        svc = []
        for s in fields["services"]:
            title = str(s.get("title", "")).strip()[:80]
            if not title:
                continue
            svc.append({"title": title, "description": str(s.get("description", "")).strip()[:400],
                        "price": str(s.get("price", "")).strip()[:60], "icon": str(s.get("icon", "")).strip()[:30] or "sparkles"})
        out["services"] = svc[:20]
    if "faqs" in fields:
        out["faqs"] = [{"q": str(f.get("q", "")).strip()[:200], "a": str(f.get("a", "")).strip()[:1000]}
                       for f in fields["faqs"] if str(f.get("q", "")).strip() and str(f.get("a", "")).strip()][:30]
    if "gallery" in fields:
        gal = []
        for i, g in enumerate(fields["gallery"]):
            url = _url(g.get("url", ""), f"Gallery photo {i + 1} URL")
            caption = str(g.get("caption", "")).strip()[:60]
            if url or caption:
                gal.append({"url": url, "caption": caption})
        out["gallery"] = gal[:12]
    return out


def update_property(conn: sqlite3.Connection, fields: dict) -> None:
    """Write already-cleaned property fields (see clean_property)."""
    fields = {k: v for k, v in fields.items() if k in PROPERTY_EDITABLE}
    if not fields:
        return
    vals = [json.dumps(v, ensure_ascii=False) if k in PROPERTY_JSON else v for k, v in fields.items()]
    conn.execute(f"UPDATE property SET {', '.join(f'{k}=?' for k in fields)} WHERE id=1", vals)


def add_room(conn: sqlite3.Connection, *, name: str, description: str = "", capacity: int = 2,
             rate_per_night: int = 0, amenities: str = "", image_url: str = "",
             status: str = "available", sort_order: int = 0) -> int:
    if status not in ROOM_STATUSES:
        raise ValueError("bad room status")
    cur = conn.execute(
        """INSERT INTO rooms (name, description, capacity, rate_per_night, amenities,
               image_url, status, sort_order) VALUES (?,?,?,?,?,?,?,?)""",
        (name, description, capacity, rate_per_night, amenities, image_url, status, sort_order),
    )
    return cur.lastrowid
