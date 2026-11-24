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
from datetime import date
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


def create_booking_request(conn: sqlite3.Connection, *, room_id: int, guest_name: str,
                           guest_phone: str, guest_email: str, guests: int,
                           check_in: str, check_out: str, notes: str) -> int:
    """Validate and insert a ``pending_audit`` booking. Raises BookingError on bad input."""
    try:
        d_in, d_out = date.fromisoformat(check_in), date.fromisoformat(check_out)
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
    if not guest_name.strip() or not guest_phone.strip():
        raise BookingError("Name and mobile number are required.")
    if guests < 1:
        raise BookingError("Number of guests must be at least 1.")

    prop = get_property(conn)
    with transaction(conn):
        room = get_room(conn, room_id)
        if room is None or room["status"] != "available":
            raise BookingError("That room isn't open for booking right now.")
        if guests > room["capacity"]:
            raise BookingError(f"{room['name']} fits up to {room['capacity']} guests.")
        if find_overlaps(conn, room_id, d_in.isoformat(), d_out.isoformat()):
            raise BookingError(f"{room['name']} is already booked for part of those dates. "
                               "Try other dates or another room.")
        total, down = quote(room["rate_per_night"], nights, prop["downpayment_pct"])
        cur = conn.execute(
            """INSERT INTO bookings (room_id, guest_name, guest_phone, guest_email, guests,
                   check_in, check_out, notes, nights, rate_per_night, total_amount,
                   downpayment_amount, status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'pending_audit')""",
            (room_id, guest_name.strip()[:120], guest_phone.strip()[:40], guest_email.strip()[:120],
             guests, d_in.isoformat(), d_out.isoformat(), notes.strip()[:1000], nights,
             room["rate_per_night"], total, down),
        )
        return cur.lastrowid


def approve_booking(conn: sqlite3.Connection, booking_id: int) -> list[sqlite3.Row]:
    """Approve if no approved booking overlaps. Returns the conflicting rows (empty on success)."""
    with transaction(conn):
        b = conn.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()
        if b is None:
            raise LookupError("booking not found")
        if b["status"] == "approved":
            return []
        conflicts = find_overlaps(conn, b["room_id"], b["check_in"], b["check_out"],
                                  exclude_booking_id=booking_id)
        if conflicts:
            return conflicts
        conn.execute("UPDATE bookings SET status='approved', decided_at=datetime('now') WHERE id=?",
                     (booking_id,))
        return []


def set_booking_status(conn: sqlite3.Connection, booking_id: int, status: str) -> None:
    if status not in ("rejected", "cancelled", "pending_audit"):
        raise ValueError("use approve_booking() to approve")
    conn.execute("UPDATE bookings SET status=?, decided_at=datetime('now') WHERE id=?",
                 (status, booking_id))


def update_room(conn: sqlite3.Connection, room_id: int, *, rate_per_night: int, status: str) -> None:
    if status not in ROOM_STATUSES:
        raise ValueError("bad room status")
    if rate_per_night < 0:
        raise ValueError("rate must be >= 0")
    conn.execute("UPDATE rooms SET rate_per_night=?, status=? WHERE id=?",
                 (rate_per_night, status, room_id))


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
