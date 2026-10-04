"""SQLite storage.

Design notes
------------
Prices are stored as *readings*, one row per stretch of time over which a
performance showed the same price and availability (table price_reading, with
first_seen / last_seen). The nightly run does not add a row for every
performance every night -- that grew the database by ~1.2 MB a night, and
GitHub refuses files over 100 MB. It adds a row only when the from-price, the
price bands, the availability or the on-sale flag differs from the performance's
latest reading; otherwise it moves that reading's last_seen forward. A repeated
identical reading carries no information beyond its date range, so nothing is
lost, and every change is still kept with the moment it was first seen.
Readings are never updated except to extend last_seen: overwriting a price
would destroy the history -- knowing that a seat cost 59.50 on the 3rd and
45.00 on the 11th is the entire point.

Readers that want one row per observation (the website importers, the report)
query the view price_observation, which expands every reading back into one
row per day it was seen. See PRICE_OBSERVATION_VIEW.

Seat states are stored as deltas. A full snapshot of a 2,300-seat house is
2,300 rows; storing that daily for 40 shows would be ~30m rows a year for
very little extra information, since most seats don't change on most days.
So we write a seat row only when its state differs from the last one we saw,
plus one aggregate row per snapshot that is always written.

Readings for performances played long ago are moved out to yearly compressed
files by wet.archive (data/archive/); this file only ever holds recent history.
"""

import json
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

-- The core time series: one row per stretch of time over which a performance
-- showed the same price and availability. first_seen/last_seen are the first
-- and last times we looked and saw exactly this reading (UTC ISO); a new row
-- is only started when something changed, or when we missed a night (see
-- record_price). days_to_perf is kept for the first and last sighting only;
-- the days in between are recomputed by the price_observation view.
CREATE TABLE IF NOT EXISTS price_reading (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    performance_id     INTEGER NOT NULL REFERENCES performance(id),
    first_seen         TEXT NOT NULL,
    last_seen          TEXT NOT NULL,
    first_days_to_perf INTEGER,           -- booking lead time at first_seen
    last_days_to_perf  INTEGER,           -- ... and at last_seen
    day_times          TEXT NOT NULL DEFAULT '',
                                          -- time of day (seconds after midnight UTC, 5 digits
                                          -- each) of the last sighting on every day from
                                          -- first_seen's date up to the day before last_seen's
    min_price          REAL,              -- the "from X" figure
    price_bands_json   TEXT,              -- every band still on sale, e.g. "[34.5,44.5,74.5]"
                                          -- NULL where the operator does not publish bands
    availability_band  TEXT,              -- operator's own words, e.g. "Good availability"
    on_sale            INTEGER NOT NULL DEFAULT 1,
    source_url         TEXT               -- the calendar page of the first sighting
);
CREATE INDEX IF NOT EXISTS ix_reading_perf ON price_reading(performance_id, first_seen);

-- 0..1100, so the price_observation view can expand a reading into its days
-- with a plain join (a recursive CTE there makes SQLite scan it per reading).
CREATE TABLE IF NOT EXISTS day_offset (i INTEGER PRIMARY KEY);

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


# One row per observation, rebuilt from price_reading, so everything written
# against the old one-row-per-sighting table (the website's importers, the
# report, `wet.cli history`) keeps working unchanged. Each reading becomes:
#   - a row at first_seen, with the days_to_perf stored then;
#   - a row at last_seen, with the days_to_perf stored then (when last_seen is
#     later than first_seen);
#   - one row for the last sighting of every day from first_seen's date up to
#     the day before last_seen's (a reading only ever spans consecutive days,
#     so each of those days really had a sighting), at the time of day kept in
#     day_times, with days_to_perf worked out from it. On the first day that
#     row is left out when it is first_seen itself.
# So "latest reading" (ORDER BY observed_at DESC) is the reading's last_seen,
# and "seen in the last three days" (observed_at >= ...) means last_seen, not
# first_seen. id is unique and stable per row (reading id x 4096 + k).
#
# Two performance notes. The days between come from a join with day_offset,
# and SQLite will not push a join through a compound view: a query that JOINS
# price_observation reads the whole view (about 0.5 s), while correlated
# lookups ("this performance's latest reading") are pushed down and cost
# nothing. New code that only needs readings should read price_reading.
PRICE_OBSERVATION_VIEW = """
CREATE VIEW price_observation AS
SELECT r.id * 4096                    AS id,
       r.performance_id               AS performance_id,
       r.first_seen                   AS observed_at,
       r.first_days_to_perf           AS days_to_perf,
       r.min_price                    AS min_price,
       r.price_bands_json             AS price_bands_json,
       r.availability_band            AS availability_band,
       r.on_sale                      AS on_sale,
       r.source_url                   AS source_url
  FROM price_reading r
UNION ALL
SELECT r.id * 4096 + 4095,
       r.performance_id, r.last_seen, r.last_days_to_perf, r.min_price,
       r.price_bands_json, r.availability_band, r.on_sale, r.source_url
  FROM price_reading r
 WHERE r.last_seen > r.first_seen
UNION ALL
SELECT m.rid * 4096 + m.i, m.performance_id,
       m.day || 'T' || time(m.tod, 'unixepoch') || '+00:00',
       -- floor((starts_at - observed_at) / 1 day), in whole seconds so a
       -- floating-point julianday difference can't tip an exact day over
       (m.s - ((m.s % 86400) + 86400) % 86400) / 86400,
       m.min_price, m.price_bands_json, m.availability_band, m.on_sale, m.source_url
  FROM (
    SELECT r.id AS rid, n.i AS i, r.performance_id AS performance_id,
           date(r.first_seen, '+' || n.i || ' days') AS day,
           CAST(substr(r.day_times, 5 * n.i + 1, 5) AS INTEGER) AS tod,
           CAST(strftime('%s', p.starts_at) AS INTEGER)
             - CAST(strftime('%s', date(r.first_seen, '+' || n.i || ' days')) AS INTEGER)
             - CAST(substr(r.day_times, 5 * n.i + 1, 5) AS INTEGER) AS s,
           r.min_price AS min_price, r.price_bands_json AS price_bands_json,
           r.availability_band AS availability_band, r.on_sale AS on_sale,
           r.source_url AS source_url,
           CAST(strftime('%s', r.first_seen) AS INTEGER) % 86400 AS first_tod
      FROM price_reading r
      JOIN day_offset n
        ON n.i < CAST(julianday(substr(r.last_seen, 1, 10))
                      - julianday(substr(r.first_seen, 1, 10)) AS INTEGER)
      JOIN performance p ON p.id = r.performance_id
  ) m
 WHERE NOT (m.i = 0 AND m.tod = m.first_tod)
"""

# A reading never spans more than this many days (day_offset holds 0..1100).
MAX_SPAN_DAYS = 1000


def _object_type(conn, name):
    row = conn.execute("SELECT type FROM sqlite_master WHERE name=?", (name,)).fetchone()
    return row[0] if row else None


def _ensure_day_offsets(conn) -> None:
    if conn.execute("SELECT COUNT(*) FROM day_offset").fetchone()[0] < 1101:
        conn.execute("""INSERT OR IGNORE INTO day_offset(i)
                        WITH RECURSIVE n(i) AS (SELECT 0 UNION ALL SELECT i + 1 FROM n WHERE i < 1100)
                        SELECT i FROM n""")
        conn.commit()


def _ensure_view(conn) -> None:
    """(Re)create the price_observation view if it is missing or out of date.
    Compared against the stored SQL so an unchanged database file isn't
    rewritten just by opening it."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='view' AND name='price_observation'").fetchone()
    if row and row[0].strip() == PRICE_OBSERVATION_VIEW.strip():
        return
    if _object_type(conn, "price_observation") == "table":
        return          # not yet converted; upgrade_to_readings() does it
    conn.execute("DROP VIEW IF EXISTS price_observation")
    conn.execute(PRICE_OBSERVATION_VIEW)
    conn.commit()


def _date_gap_days(earlier: str, later: str) -> int:
    """Whole calendar days (UTC dates) from one ISO timestamp to another."""
    return (datetime.fromisoformat(later[:10]) - datetime.fromisoformat(earlier[:10])).days


def _second_of_day(iso: str) -> int:
    """Seconds after midnight UTC of an ISO timestamp as utcnow() writes it."""
    return int(iso[11:13]) * 3600 + int(iso[14:16]) * 60 + int(iso[17:19])


def upgrade_to_readings(conn, vacuum=True) -> dict | None:
    """Convert a database in the old one-row-per-sighting layout (a TABLE called
    price_observation) to price_reading + the compatibility view. Returns
    {rows_before, readings_after} or None when there was nothing to do.

    Consecutive sightings of one performance are folded into one reading while
    price, bands, availability and on-sale flag are identical and the sightings
    fall on the same or consecutive UTC days. A missed night starts a new
    reading with the same values, so the view never invents a day nobody
    looked. What a reading keeps of its sightings: the first, the last, and
    the time of the last sighting of every day before the last. Not kept: which
    of a performance's calendar pages (source_url) a repeat came from, and
    earlier sightings on a day that had several (identical ones, by
    construction), and the first sighting of the last day.

    All in one transaction: either the whole table is converted and dropped, or
    nothing changes. The old table is only dropped after the view built from
    the new rows has been checked against it.
    """
    if _object_type(conn, "price_observation") != "table":
        return None
    have = {r[1] for r in conn.execute("PRAGMA table_info(price_observation)")}
    if "price_bands_json" not in have:
        conn.execute("ALTER TABLE price_observation ADD COLUMN price_bands_json TEXT")
    conn.commit()
    _ensure_day_offsets(conn)

    before = conn.execute("SELECT COUNT(*) FROM price_observation").fetchone()[0]
    conn.execute("BEGIN")
    try:
        # A previous run that was interrupted before the drop leaves readings
        # behind; start from clean rather than doubling them.
        conn.execute("DELETE FROM price_reading")
        cur = conn.execute(
            """SELECT performance_id, observed_at, days_to_perf, min_price, price_bands_json,
                      availability_band, on_sale, source_url
                 FROM price_observation ORDER BY performance_id, observed_at, id""")
        # open reading: [perf, first, last, first_days, last_days, price, bands, band,
        #                on_sale, url, {day: second_of_day of that day's last sighting}]
        open_row = None
        out = []

        def close(row):
            first_day, last_day = row[1][:10], row[2][:10]
            days = sorted(d for d in row[10] if first_day <= d < last_day)
            out.append((row[0], row[1], row[2], row[3], row[4],
                        "".join(f"{row[10][d]:05d}" for d in days),
                        row[5], row[6], row[7], row[8], row[9]))

        for r in cur:
            perf, at, days, price, bands, band, on_sale, url = tuple(r)
            same = (open_row is not None and open_row[0] == perf
                    and (open_row[5], open_row[6], open_row[7], open_row[8]) == (price, bands, band, on_sale)
                    and _date_gap_days(open_row[2], at) <= 1
                    and _date_gap_days(open_row[1], at) < MAX_SPAN_DAYS)
            if same:
                open_row[2], open_row[4] = at, days
                open_row[10][at[:10]] = _second_of_day(at)
                continue
            if open_row is not None:
                close(open_row)
            open_row = [perf, at, at, days, days, price, bands, band, on_sale, url,
                        {at[:10]: _second_of_day(at)}]
        if open_row is not None:
            close(open_row)
        conn.executemany(
            """INSERT INTO price_reading
               (performance_id, first_seen, last_seen, first_days_to_perf, last_days_to_perf,
                day_times, min_price, price_bands_json, availability_band, on_sale, source_url)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""", out)

        # The check: every original sighting's day must still be covered by a
        # reading with identical values, and no reading may cover a day with
        # no sighting. Compared as sets of (performance, UTC day, values).
        cols = ("performance_id, substr(observed_at,1,10), min_price, price_bands_json, "
                "availability_band, on_sale")
        want = set(map(tuple, conn.execute(f"SELECT {cols} FROM price_observation")))
        conn.execute("DROP VIEW IF EXISTS temp.price_observation_check")
        conn.execute(PRICE_OBSERVATION_VIEW.replace(
            "CREATE VIEW price_observation AS", "CREATE TEMP VIEW price_observation_check AS"))
        got = set(map(tuple, conn.execute(f"SELECT {cols} FROM price_observation_check")))
        conn.execute("DROP VIEW temp.price_observation_check")
        if want != got:
            raise RuntimeError(
                f"compaction check failed: {len(want - got)} sightings lost, "
                f"{len(got - want)} invented -- database left unchanged")

        conn.execute("DROP TABLE price_observation")
        conn.execute(PRICE_OBSERVATION_VIEW)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    if vacuum:
        conn.execute("VACUUM")
    return {"rows_before": before, "readings_after": len(out)}


def _migrate(conn) -> None:
    """Bring an older database up to the current layout.

    Deliberately additive until the one-off conversion to readings: the
    collected history is the whole asset, so a migration may only ADD, never
    drop or rewrite a column. An old row with no band data is being honest
    about what we knew that day. The conversion to readings drops the old
    table only after proving the new one reproduces every sighting.
    """
    _ensure_day_offsets(conn)
    if _object_type(conn, "price_observation") == "table":
        upgrade_to_readings(conn)
    _ensure_view(conn)


def connect(path="data/westend.db") -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
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
                 days_to_perf=None, on_sale=True, price_bands=None, now=None):
    """Record what we just saw for one performance. Returns True when this
    started a new reading, False when it only confirmed the current one.

    A reading is the from-price, the price bands, the availability band and
    the on-sale flag. If all four equal the performance's latest reading AND
    that reading was last confirmed today or yesterday (UTC), nothing new is
    stored: the reading's last_seen simply moves to now. Otherwise a new
    reading starts at now. The "today or yesterday" rule means a night we
    missed (a failed run) starts a fresh reading with the same values rather
    than stretching the old one across a day nobody looked.

    When last_seen moves on to a new day, the time of the last sighting on the
    day it leaves is appended to day_times (see PRICE_OBSERVATION_VIEW).

    price_bands: every price still on sale for this performance, or None
    where the operator does not publish them. Stored as a JSON list so the
    website can build Premium/Mid-range/Budget bands without re-fetching.

    Note what this is NOT: it is the bands with seats remaining, not the
    house's full price list. On a nearly-sold-out performance the cheap
    bands vanish, so anything published from this must be qualified by
    availability_band or it will quote a sold-out show's premium-only
    prices as if they were normal.
    """
    now = now or utcnow()
    bands_json = None
    if price_bands:
        bands_json = json.dumps(sorted(float(b) for b in price_bands))
    on_sale = 1 if on_sale else 0

    last = conn.execute(
        """SELECT id, first_seen, last_seen, min_price, price_bands_json, availability_band, on_sale
             FROM price_reading WHERE performance_id=?
            ORDER BY first_seen DESC, id DESC LIMIT 1""", (performance_id,)).fetchone()
    if (last is not None
            and (last["min_price"], last["price_bands_json"],
                 last["availability_band"], last["on_sale"])
            == (min_price, bands_json, availability_band, on_sale)
            and _date_gap_days(last["last_seen"], now) <= 1
            and _date_gap_days(last["first_seen"], now) < MAX_SPAN_DAYS):
        if now > last["last_seen"]:
            leaving = last["last_seen"]
            if now[:10] > leaving[:10]:
                conn.execute(
                    "UPDATE price_reading SET last_seen=?, last_days_to_perf=?, "
                    "day_times = day_times || ? WHERE id=?",
                    (now, days_to_perf, f"{_second_of_day(leaving):05d}", last["id"]))
            else:
                conn.execute("UPDATE price_reading SET last_seen=?, last_days_to_perf=? WHERE id=?",
                             (now, days_to_perf, last["id"]))
        return False
    conn.execute(
        """INSERT INTO price_reading
           (performance_id, first_seen, last_seen, first_days_to_perf, last_days_to_perf,
            min_price, price_bands_json, availability_band, on_sale, source_url)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (performance_id, now, now, days_to_perf, days_to_perf,
         min_price, bands_json, availability_band, on_sale, source_url),
    )
    return True


def record_seat_states(conn, performance_id, seats, now=None):
    """Delta-write seat_state only: a row for each seat whose availability
    or price differs from the last reading. No seat_snapshot row — for the
    standing places, where a snapshot of a 29-place row would sit among
    whole-house snapshots and mean something different.

    seats: objects or dicts with seat_ref, available, price."""
    now = now or utcnow()
    get = lambda s, k: s[k] if isinstance(s, dict) else getattr(s, k)
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
        cur = (1 if get(s, "available") else 0, get(s, "price"))
        if last.get(get(s, "seat_ref")) != cur:
            conn.execute(
                """INSERT INTO seat_state(performance_id, seat_ref, observed_at, available, price)
                   VALUES (?,?,?,?,?)""",
                (performance_id, get(s, "seat_ref"), now, cur[0], cur[1]),
            )
            changed += 1
    return changed


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
