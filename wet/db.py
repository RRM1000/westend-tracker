"""SQLite storage.

Design notes
------------
Observations are append-only. We never update a price in place, because the
whole asset is the time series — knowing that a seat cost £59.50 on the 3rd
and £45.00 on the 11th is the entire point, and an UPDATE would destroy it.

Seat states are stored as deltas. A full snapshot of a 2,300-seat house is
2,300 rows; storing that daily for 40 shows would be ~30m rows a year for
very little extra information, since most seats don't change on most days.
So we write a seat row only when its state differs from the last one we saw,
plus one aggregate row per snapshot that is always written.
"""

import sqlite3
from pathlib import Path
from datetime import datetime, timezone

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS show (
    key           TEXT PRIMARY KEY,
    operator      TEXT NOT NULL,
    title         TEXT NOT NULL,
    venue         TEXT,
    config_json   TEXT,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS performance (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    show_key      TEXT NOT NULL REFERENCES show(key),
    external_id   TEXT NOT NULL,          -- operator's own performance id
    starts_at     TEXT NOT NULL,          -- ISO local datetime of the performance
    url           TEXT,
    UNIQUE(show_key, external_id)
);
CREATE INDEX IF NOT EXISTS ix_perf_show_start ON performance(show_key, starts_at);

-- One row per (performance, time we looked). This is the core time series.
CREATE TABLE IF NOT EXISTS price_observation (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    performance_id    INTEGER NOT NULL REFERENCES performance(id),
    observed_at       TEXT NOT NULL,      -- UTC ISO
    days_to_perf      INTEGER,            -- convenience: booking lead time
    min_price         REAL,               -- the "from £X" figure
    availability_band TEXT,               -- operator's own words, e.g. "Good availability"
    on_sale           INTEGER NOT NULL DEFAULT 1,
    source_url        TEXT
);
CREATE INDEX IF NOT EXISTS ix_obs_perf ON price_observation(performance_id, observed_at);

-- One row per seat-map capture, always written.
CREATE TABLE IF NOT EXISTS seat_snapshot (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    performance_id    INTEGER NOT NULL REFERENCES performance(id),
    observed_at       TEXT NOT NULL,
    seats_total       INTEGER,
    seats_available   INTEGER,
    price_bands_json  TEXT,               -- {"29.50": 85, "39.50": 133, ...}
    est_gross_low     REAL,               -- unavailable seats x cheapest band
    est_gross_high    REAL,               -- unavailable seats x dearest band
    source_url        TEXT
);
CREATE INDEX IF NOT EXISTS ix_snap_perf ON seat_snapshot(performance_id, observed_at);

-- One row per seat, written only when its state CHANGES.
CREATE TABLE IF NOT EXISTS seat_state (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    performance_id    INTEGER NOT NULL REFERENCES performance(id),
    seat_ref          TEXT NOT NULL,      -- normalised, e.g. "STALLS|LEFT|S|18"
    observed_at       TEXT NOT NULL,
    available         INTEGER NOT NULL,   -- 1 available, 0 gone
    price             REAL                -- NULL when unavailable
);
CREATE INDEX IF NOT EXISTS ix_seat_perf_ref ON seat_state(performance_id, seat_ref, observed_at);

CREATE TABLE IF NOT EXISTS run_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    task          TEXT NOT NULL,
    pages_ok      INTEGER DEFAULT 0,
    pages_failed  INTEGER DEFAULT 0,
    notes         TEXT
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path="data/westend.db") -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def upsert_show(conn, key, operator, title, venue, config_json):
    now = utcnow()
    conn.execute(
        """INSERT INTO show(key, operator, title, venue, config_json, first_seen, last_seen)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(key) DO UPDATE SET title=excluded.title,
                                          venue=excluded.venue,
                                          config_json=excluded.config_json,
                                          last_seen=excluded.last_seen""",
        (key, operator, title, venue, config_json, now, now),
    )


def upsert_performance(conn, show_key, external_id, starts_at, url) -> int:
    conn.execute(
        """INSERT INTO performance(show_key, external_id, starts_at, url)
           VALUES (?,?,?,?)
           ON CONFLICT(show_key, external_id) DO UPDATE SET starts_at=excluded.starts_at,
                                                            url=excluded.url""",
        (show_key, external_id, starts_at, url),
    )
    row = conn.execute(
        "SELECT id FROM performance WHERE show_key=? AND external_id=?",
        (show_key, external_id),
    ).fetchone()
    return row["id"]


def record_price(conn, performance_id, min_price, availability_band, source_url,
                 days_to_perf=None, on_sale=True):
    conn.execute(
        """INSERT INTO price_observation
           (performance_id, observed_at, days_to_perf, min_price,
            availability_band, on_sale, source_url)
           VALUES (?,?,?,?,?,?,?)""",
        (performance_id, utcnow(), days_to_perf, min_price,
         availability_band, 1 if on_sale else 0, source_url),
    )


def record_seats(conn, performance_id, seats, source_url):
    """seats: list of dicts {seat_ref, available: bool, price: float|None}"""
    now = utcnow()
    total = len(seats)
    avail = sum(1 for s in seats if s["available"])

    bands = {}
    for s in seats:
        if s["available"] and s["price"] is not None:
            bands[f"{s['price']:.2f}"] = bands.get(f"{s['price']:.2f}", 0) + 1

    prices = [s["price"] for s in seats if s["price"] is not None]
    gone = total - avail
    low = round(gone * min(prices), 2) if prices else None
    high = round(gone * max(prices), 2) if prices else None

    import json
    conn.execute(
        """INSERT INTO seat_snapshot
           (performance_id, observed_at, seats_total, seats_available,
            price_bands_json, est_gross_low, est_gross_high, source_url)
           VALUES (?,?,?,?,?,?,?,?)""",
        (performance_id, now, total, avail, json.dumps(bands), low, high, source_url),
    )

    # Delta write: only seats whose state differs from the last we recorded.
    last = {
        r["seat_ref"]: (r["available"], r["price"])
        for r in conn.execute(
            """SELECT seat_ref, available, price FROM seat_state s1
               WHERE performance_id=?
                 AND observed_at=(SELECT MAX(observed_at) FROM seat_state s2
                                  WHERE s2.performance_id=s1.performance_id
                                    AND s2.seat_ref=s1.seat_ref)""",
            (performance_id,),
        )
    }
    changed = 0
    for s in seats:
        prev = last.get(s["seat_ref"])
        cur = (1 if s["available"] else 0, s["price"])
        if prev is None or prev != cur:
            conn.execute(
                """INSERT INTO seat_state(performance_id, seat_ref, observed_at, available, price)
                   VALUES (?,?,?,?,?)""",
                (performance_id, s["seat_ref"], now, cur[0], cur[1]),
            )
            changed += 1
    return {"total": total, "available": avail, "changed": changed}
