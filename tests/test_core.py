"""Tests for the parts that don't need a browser.

The DOM parsing needs a real page, so it's covered by `wet.cli probe` rather
than unit tests. Everything below is pure logic and runs anywhere.
"""

import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wet import db
from wet.collect import load_shows
from wet.parsers.atg import ATGParser, seat_ref_for
from wet import parsers
from wet.parsers.ticketing_api import available_bands, cheapest_from_inventory
from wet.parsers.spektrix import cheapest_public_price, public_prices
from wet.parsers.base import all_money, first_money, norm_ref, parse_uk_datetime


def test_uk_datetime_from_atg_label():
    # Real label text read off atgtickets.com on 23 Aug 2026
    got = parse_uk_datetime("Tuesday August 25th 2026 25 Medium availability 19:30 from £29.50")
    assert got == "2026-08-25T19:30:00", got


def test_uk_datetime_matinee():
    got = parse_uk_datetime("Sunday August 23rd 2026 23 Medium availability 14:30 from £59.50")
    assert got == "2026-08-23T14:30:00", got


def test_uk_datetime_rejects_junk():
    assert parse_uk_datetime("Book now") is None
    assert parse_uk_datetime("") is None


def test_money():
    assert first_money("from £29.50") == 29.50
    assert first_money("no price here") is None
    # Real Wicked price ladder
    text = "£29.50 £39.50 £59.50 £69.50 £89.50 £99.50 £109.50 £139.50 £169.50"
    assert all_money(text) == [29.5, 39.5, 59.5, 69.5, 89.5, 99.5, 109.5, 139.5, 169.5]


def test_seat_ref_normalisation():
    # LW gives four parts, ATG gives two. Both must be stable and comparable.
    assert norm_ref("Stalls", "Left", "S", "18") == "STALLS|LEFT|S|18"
    assert norm_ref("A", "13") == "A|13"
    assert norm_ref(" stalls ", "", "s", "18") == "STALLS|S|18"


def test_atg_seat_refs_do_not_collide_across_sections():
    """The Apollo Victoria has a Row A Seat 13 in the stalls and another in
    the dress circle. Same label, different seat — they must not share a ref."""
    stalls = seat_ref_for("Row A, Seat 13, Sold out", "2315AD33-EDEB-411D-A7DB-03B83FF400A9")
    circle = seat_ref_for("Row A, Seat 13, £59.50, Available", "5B29072B-8D37-45C3-BFC0-46F8308B9523")
    assert stalls != circle, (stalls, circle)
    assert stalls.startswith("A|13|"), stalls
    # the same seat read again tomorrow must produce the same ref, or the
    # delta storage would rewrite the whole house every night
    again = seat_ref_for("Row A, Seat 13, Available", "2315AD33-EDEB-411D-A7DB-03B83FF400A9")
    assert again == stalls


def test_atg_seat_ref_ignores_non_seats():
    assert seat_ref_for("Theater seatmap", None) is None
    assert seat_ref_for("", "GUID") is None


def test_spektrix_price_ignores_restricted_tickets():
    """Real Donmar price list, 1 Sep 2026. The raw minimum is a 15.00 Access
    seat; the cheapest a member of the public can actually buy is the 15.00
    Standing ticket, and if standing were absent it would be 30.00."""
    payload = {"prices": [
        {"amount": 70.0, "ticketType": {"name": "Full Price"}},
        {"amount": 55.0, "ticketType": {"name": "Full Price"}},
        {"amount": 30.0, "ticketType": {"name": "Full Price"}},
        {"amount": 20.0, "ticketType": {"name": "Access"}},
        {"amount": 15.0, "ticketType": {"name": "Access"}},
        {"amount": 20.0, "ticketType": {"name": "35 and under"}},
        {"amount": 15.0, "ticketType": {"name": "Standing"}},
    ]}
    assert cheapest_public_price(payload) == 15.0
    no_standing = {"prices": [r for r in payload["prices"]
                              if r["ticketType"]["name"] != "Standing"]}
    assert cheapest_public_price(no_standing) == 30.0


def test_spektrix_price_rejects_non_tickets():
    """A Park Theatre instance offered only "Merchandise 5.00". That is not a
    ticket price and must not be published as one — rejected by ticket type
    name, never by being a low number."""
    assert cheapest_public_price({"prices": [
        {"amount": 5.0, "ticketType": {"name": "Merchandise"}}]}) is None
    assert cheapest_public_price({"prices": []}) is None
    assert cheapest_public_price(None) is None
    # A genuinely cheap real ticket must NOT be caught by any price floor.
    assert cheapest_public_price({"prices": [
        {"amount": 0.10, "ticketType": {"name": "Full Price"}}]}) == 0.10


def test_spektrix_price_ignores_worded_concessions():
    """Real Royal Court list, Nov 2026. Genuinely on sale at 10p (confirmed by
    the venue) alongside a duplicate 22.50 "Full Price" row and concessions
    worded so they dodge the obvious keywords. The 10p row is real and must
    survive; only the concessions are dropped."""
    rows = ([{"amount": a, "ticketType": {"name": "Full Price"}}
             for a in (64.5, 49.0, 35.0, 22.5, 0.10, 22.5)] +
            [{"amount": a, "ticketType": {"name": n}}
             for n in ("Over 65s", "Student", "Unwaged ", "25 or Under",
                       "Equity/BECTU/WGGB Members ")
             for a in (59.5, 44.0, 30.0, 17.5)] +
            [{"amount": a, "ticketType": {"name": "Ticket for Access Booker"}}
             for a in (32.25, 24.5, 17.5, 11.25)])
    assert cheapest_public_price({"prices": rows}) == 0.10


def test_price_bands_are_stored_and_read_back():
    """The website needs Premium/Mid/Budget bands, not just the cheapest
    ticket, so every band on sale is kept alongside the minimum."""
    with tempfile.TemporaryDirectory() as tmp:
        conn = db.connect(os.path.join(tmp, "t.db"))
        pid = _seed(conn)
        db.record_price(conn, pid, 34.5, "Good availability", "u",
                        price_bands=[74.5, 34.5, 54.5, 44.5])
        row = conn.execute(
            "SELECT min_price, price_bands_json FROM price_observation "
            "WHERE performance_id=?", (pid,)).fetchone()
        assert row["min_price"] == 34.5
        assert json.loads(row["price_bands_json"]) == [34.5, 44.5, 54.5, 74.5]

        # an operator that publishes no bands must store NULL, not an empty
        # list — "we don't know" and "there are none" are different facts
        db.record_price(conn, pid, 25.0, "Good availability", "u")
        rows = conn.execute(
            "SELECT price_bands_json FROM price_observation "
            "WHERE performance_id=? ORDER BY id", (pid,)).fetchall()
        assert rows[1]["price_bands_json"] is None
        conn.close()


def test_migration_adds_bands_column_to_an_old_database():
    """The existing archive predates this column. Opening it must add the
    column without touching a single collected row."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "old.db")
        old = sqlite3.connect(path)
        old.executescript("""
            CREATE TABLE price_observation (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                performance_id INTEGER NOT NULL,
                observed_at TEXT NOT NULL,
                days_to_perf INTEGER,
                min_price REAL,
                availability_band TEXT,
                on_sale INTEGER NOT NULL DEFAULT 1,
                source_url TEXT);
            INSERT INTO price_observation
                (performance_id, observed_at, min_price, availability_band, on_sale)
            VALUES (1, '2026-08-23T06:00:00+00:00', 29.5, 'Good availability', 1);""")
        old.commit(); old.close()

        conn = db.connect(path)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(price_observation)")}
        assert "price_bands_json" in cols
        row = conn.execute("SELECT min_price, price_bands_json FROM price_observation").fetchone()
        assert row["min_price"] == 29.5           # untouched
        assert row["price_bands_json"] is None    # honest about what we knew then
        conn.close()


def test_tixtrack_bands_come_from_available_price_maps():
    """Real SIX payload shape. priceMaps lists only bands with seats left,
    so this is 'still on sale', not the house's full price list."""
    inv = {"priceMaps": [{"price": 74.5}, {"price": 34.5}, {"price": 54.5},
                         {"price": 34.5}, {"price": None}]}
    assert available_bands(inv) == [34.5, 54.5, 74.5]
    assert available_bands({"priceMaps": []}) is None
    assert available_bands(None) is None


def test_spektrix_bands_drop_restricted_types_only():
    """Bands for the website must exclude access/concession rates but keep
    genuinely cheap public tickets."""
    payload = {"prices": [
        {"amount": 70.0, "ticketType": {"name": "Full Price"}},
        {"amount": 30.0, "ticketType": {"name": "Full Price"}},
        {"amount": 15.0, "ticketType": {"name": "Standing"}},
        {"amount": 20.0, "ticketType": {"name": "Access"}},
        {"amount": 17.5, "ticketType": {"name": "Over 65s"}},
    ]}
    assert public_prices(payload) == [15.0, 30.0, 70.0]


def _seed(conn):
    db.upsert_show(conn, "wicked-apollo-victoria", "atg", "Wicked", "Apollo Victoria", "{}")
    return db.upsert_performance(conn, "wicked-apollo-victoria", "GUID-1",
                                 "2026-08-25T19:30:00", "https://example.test/x")


def test_seat_delta_only_writes_changes():
    with tempfile.TemporaryDirectory() as tmp:
        conn = db.connect(os.path.join(tmp, "t.db"))
        pid = _seed(conn)

        seats = [{"seat_ref": f"A|{i}", "available": True, "price": 29.50} for i in range(100)]
        first = db.record_seats(conn, pid, seats, "u")
        assert first["changed"] == 100          # everything is new

        again = db.record_seats(conn, pid, seats, "u")
        assert again["changed"] == 0            # nothing moved, nothing written

        seats[0]["available"] = False
        seats[0]["price"] = None
        third = db.record_seats(conn, pid, seats, "u")
        assert third["changed"] == 1            # exactly the seat that sold

        rows = conn.execute("SELECT COUNT(*) c FROM seat_state WHERE performance_id=?",
                            (pid,)).fetchone()["c"]
        assert rows == 101
        conn.close()


def test_snapshot_maths_matches_the_live_reading():
    """Reproduces the Wicked figures actually read on 23 Aug 2026:
    2,328 seats, 1,655 gone, 673 available."""
    with tempfile.TemporaryDirectory() as tmp:
        conn = db.connect(os.path.join(tmp, "t.db"))
        pid = _seed(conn)
        seats = ([{"seat_ref": f"S|{i}", "available": False, "price": None} for i in range(1655)]
                 + [{"seat_ref": f"A|{i}", "available": True, "price": 29.50} for i in range(673)])
        stats = db.record_seats(conn, pid, seats, "u")
        assert stats["total"] == 2328
        assert stats["available"] == 673

        snap = conn.execute("SELECT * FROM seat_snapshot WHERE performance_id=?",
                            (pid,)).fetchone()
        assert snap["seats_total"] == 2328
        assert round(100 * (1 - 673 / 2328), 1) == 71.1
        conn.close()


def test_price_changes_are_kept_in_order():
    """A changed price is never written over: both stay, oldest first, each with
    the moment it was first seen."""
    with tempfile.TemporaryDirectory() as tmp:
        conn = db.connect(os.path.join(tmp, "t.db"))
        pid = _seed(conn)
        db.record_price(conn, pid, 59.50, "Medium availability", "u", now="2026-09-01T05:00:00+00:00")
        db.record_price(conn, pid, 45.00, "Good availability", "u", now="2026-09-02T05:00:00+00:00")
        rows = conn.execute(
            "SELECT min_price, first_seen FROM price_reading WHERE performance_id=? ORDER BY first_seen",
            (pid,)).fetchall()
        assert [(r["min_price"], r["first_seen"][:10]) for r in rows] == \
            [(59.50, "2026-09-01"), (45.00, "2026-09-02")]
        conn.close()


NIGHTS = ["2026-09-01T05:00:10+00:00", "2026-09-02T09:30:00+00:00", "2026-09-03T04:05:00+00:00",
          "2026-09-04T10:50:00+00:00", "2026-09-05T06:00:00+00:00"]


def _days_to(starts_at, seen):
    run = datetime.fromisoformat(seen).replace(tzinfo=None)
    return (datetime.fromisoformat(starts_at) - run).days


def test_unchanged_reading_extends_instead_of_inserting():
    """The point of the whole change: the same price and availability seen on
    five nights is ONE row, with last_seen moved forward, not five."""
    with tempfile.TemporaryDirectory() as tmp:
        conn = db.connect(os.path.join(tmp, "t.db"))
        pid = _seed(conn)
        starts = "2026-09-20T19:30:00"
        conn.execute("UPDATE performance SET starts_at=? WHERE id=?", (starts, pid))
        made = [db.record_price(conn, pid, 29.5, "Good availability", "u", price_bands=[29.5, 49.5],
                                days_to_perf=_days_to(starts, t), now=t) for t in NIGHTS]
        assert made == [True, False, False, False, False]
        rows = conn.execute("SELECT * FROM price_reading").fetchall()
        assert len(rows) == 1
        assert rows[0]["first_seen"] == NIGHTS[0] and rows[0]["last_seen"] == NIGHTS[-1]
        # the compatibility view still shows each night, at the time of the
        # sighting, with the days_to_perf the collector worked out then
        view = conn.execute("SELECT observed_at, days_to_perf FROM price_observation "
                            "ORDER BY observed_at").fetchall()
        assert [(r["observed_at"], r["days_to_perf"]) for r in view] == \
            [(t, _days_to(starts, t)) for t in NIGHTS]
        # a change starts a new reading; the old one stops where it was
        db.record_price(conn, pid, 35.0, "Medium availability", "u", now="2026-09-06T06:00:00+00:00")
        rows = conn.execute("SELECT min_price, last_seen FROM price_reading ORDER BY id").fetchall()
        assert [(r["min_price"], r["last_seen"]) for r in rows] == \
            [(29.5, NIGHTS[-1]), (35.0, "2026-09-06T06:00:00+00:00")]
        conn.close()


def test_a_missed_night_is_not_papered_over():
    """If the run failed on the 2nd and 3rd, a price seen again on the 4th is a
    new reading: the view must not claim we looked on days we did not."""
    with tempfile.TemporaryDirectory() as tmp:
        conn = db.connect(os.path.join(tmp, "t.db"))
        pid = _seed(conn)
        for t in (NIGHTS[0], NIGHTS[3], NIGHTS[4]):
            db.record_price(conn, pid, 29.5, "Good availability", "u", now=t)
        assert conn.execute("SELECT COUNT(*) FROM price_reading").fetchone()[0] == 2
        days = [r[0][:10] for r in conn.execute("SELECT observed_at FROM price_observation ORDER BY observed_at")]
        assert days == ["2026-09-01", "2026-09-04", "2026-09-05"], days
        conn.close()


def _old_layout_db(path, sightings):
    """A database as the collector wrote it before readings: one price_observation
    row per sighting. sightings: (performance 'A' or 'B', observed_at, price,
    bands json, availability, on_sale)."""
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE show (key TEXT PRIMARY KEY, operator TEXT NOT NULL, title TEXT NOT NULL,
                           venue TEXT, config_json TEXT, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL);
        CREATE TABLE performance (id INTEGER PRIMARY KEY AUTOINCREMENT, show_key TEXT NOT NULL,
                           external_id TEXT NOT NULL, starts_at TEXT NOT NULL, url TEXT,
                           UNIQUE(show_key, external_id));
        CREATE TABLE price_observation (id INTEGER PRIMARY KEY AUTOINCREMENT,
                           performance_id INTEGER NOT NULL, observed_at TEXT NOT NULL, days_to_perf INTEGER,
                           min_price REAL, price_bands_json TEXT, availability_band TEXT,
                           on_sale INTEGER NOT NULL DEFAULT 1, source_url TEXT);
        CREATE INDEX ix_obs_perf ON price_observation(performance_id, observed_at);
        INSERT INTO show VALUES ('s', 'atg', 'S', 'V', '{}', '2026-09-01', '2026-09-01');
        INSERT INTO performance(show_key, external_id, starts_at) VALUES
            ('s', 'A', '2026-09-20T14:30:00'), ('s', 'B', '2026-09-20T19:30:00');""")
    ids = {"A": 1, "B": 2}
    starts = {"A": "2026-09-20T14:30:00", "B": "2026-09-20T19:30:00"}
    for ext, at, price, bands, band, on_sale in sightings:
        old.execute("""INSERT INTO price_observation (performance_id, observed_at, days_to_perf, min_price,
                       price_bands_json, availability_band, on_sale, source_url) VALUES (?,?,?,?,?,?,?,?)""",
                    (ids[ext], at, _days_to(starts[ext], at), price, bands, band, on_sale, "u"))
    old.commit()
    old.close()


# Sightings with the awkward cases: a price that changes, the same price seen
# twice in one morning (a performance on two calendar pages), an afternoon run,
# a missed night (A on the 5th), and a performance sold out (no price) and back.
SIGHTINGS = [
    ("A", "2026-09-01T05:00:10+00:00", 29.5, "[29.5, 49.5]", "Good availability", 1),
    ("B", "2026-09-01T05:00:11+00:00", 39.5, None, "Good availability", 1),
    ("A", "2026-09-01T05:09:00+00:00", 29.5, "[29.5, 49.5]", "Good availability", 1),
    ("A", "2026-09-02T09:30:00+00:00", 29.5, "[29.5, 49.5]", "Good availability", 1),
    ("B", "2026-09-02T09:30:01+00:00", 39.5, None, "Good availability", 1),
    ("A", "2026-09-02T15:10:00+00:00", 29.5, "[29.5, 49.5]", "Good availability", 1),
    ("A", "2026-09-03T04:05:00+00:00", 29.5, "[29.5, 49.5]", "Good availability", 1),
    ("B", "2026-09-03T04:05:01+00:00", None, None, "Low availability", 1),
    ("A", "2026-09-04T10:50:00+00:00", 34.5, "[34.5, 49.5]", "Medium availability", 1),
    ("B", "2026-09-04T10:50:01+00:00", None, None, "Low availability", 1),
    ("B", "2026-09-05T06:00:01+00:00", None, None, "Low availability", 1),
    ("A", "2026-09-06T06:00:00+00:00", 34.5, "[34.5, 49.5]", "Medium availability", 1),
    ("B", "2026-09-06T06:00:01+00:00", 39.5, None, "Good availability", 1),
]


def _per_day_last(rows):
    """{(performance, UTC day): (observed_at, days_to_perf, values)} for the LAST
    sighting of each day: what every reader of price_observation ends up with,
    because they all take the latest of a day (or the latest overall)."""
    out = {}
    for pid, at, days, price, bands, band, on_sale in sorted(rows, key=lambda r: (r[0], r[1])):
        out[(pid, at[:10])] = (at, days, (price, bands, band, on_sale))
    return out


def test_conversion_reproduces_every_day_of_the_old_table():
    """Converting an old-layout database must give back, through price_observation,
    the same last sighting for every performance on every day -- same time of day,
    same days_to_perf, same price/bands/availability -- and nothing for a day nobody
    looked (A on the 5th)."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "old.db")
        _old_layout_db(path, SIGHTINGS)
        q = ("SELECT performance_id, observed_at, days_to_perf, min_price, price_bands_json, "
             "availability_band, on_sale FROM price_observation")
        raw = sqlite3.connect(path)
        before = _per_day_last([tuple(r) for r in raw.execute(q)])
        raw.close()

        conn = db.connect(path)                      # converts
        kind = conn.execute("SELECT type FROM sqlite_master WHERE name='price_observation'").fetchone()[0]
        assert kind == "view"
        after = _per_day_last([tuple(r) for r in conn.execute(q)])
        assert after == before
        assert (1, "2026-09-05") not in after
        # fewer rows stored than sightings made
        assert conn.execute("SELECT COUNT(*) FROM price_reading").fetchone()[0] < len(SIGHTINGS)
        # converting again changes nothing
        assert db.upgrade_to_readings(conn) is None
        conn.close()


def test_nightly_writes_agree_with_conversion():
    """Writing the sightings one at a time with record_price must leave the
    same readings as converting the same sightings written the old way."""
    with tempfile.TemporaryDirectory() as tmp:
        old_path, new_path = os.path.join(tmp, "old.db"), os.path.join(tmp, "new.db")
        _old_layout_db(old_path, SIGHTINGS)
        converted = db.connect(old_path)

        fresh = db.connect(new_path)
        db.upsert_show(fresh, "s", "atg", "S", "V", "{}")
        starts = {"A": "2026-09-20T14:30:00", "B": "2026-09-20T19:30:00"}
        ids = {ext: db.upsert_performance(fresh, "s", ext, starts[ext], None) for ext in starts}
        for ext, at, price, bands, band, on_sale in SIGHTINGS:
            db.record_price(fresh, ids[ext], price, band, "u", days_to_perf=_days_to(starts[ext], at),
                            on_sale=bool(on_sale), price_bands=json.loads(bands) if bands else None, now=at)
        cols = ("performance_id, first_seen, last_seen, first_days_to_perf, last_days_to_perf, day_times, "
                "min_price, price_bands_json, availability_band, on_sale")
        a = [tuple(r) for r in converted.execute(f"SELECT {cols} FROM price_reading ORDER BY 1, 2")]
        b = [tuple(r) for r in fresh.execute(f"SELECT {cols} FROM price_reading ORDER BY 1, 2")]
        assert a == b, (a, b)
        converted.close(); fresh.close()


def test_archive_moves_old_readings_and_restores_them():
    from wet import archive
    with tempfile.TemporaryDirectory() as tmp:
        conn = db.connect(os.path.join(tmp, "t.db"))
        db.upsert_show(conn, "s", "atg", "S", "V", "{}")
        old_a = db.upsert_performance(conn, "s", "old-a", "2026-01-10T19:30:00", None)
        old_b = db.upsert_performance(conn, "s", "old-b", "2027-01-05T19:30:00", None)   # another year
        new = db.upsert_performance(conn, "s", "new", "2026-12-20T19:30:00", None)
        for pid in (old_a, old_b, new):
            for t, price in (("2026-01-01T05:00:00+00:00", 29.5), ("2026-01-02T05:00:00+00:00", 29.5),
                             ("2026-01-03T05:00:00+00:00", 35.0)):
                db.record_price(conn, pid, price, "Good availability", "u", price_bands=[price, 80.0],
                                days_to_perf=9, now=t)
        total = conn.execute("SELECT COUNT(*) FROM price_reading").fetchone()[0]       # 6: two per performance
        adir = os.path.join(tmp, "archive")

        # a dry run reports and changes nothing
        assert archive.archive_old_readings(conn, adir, 180, dry_run=True, today="2026-12-31") == {"2026": 2}
        assert conn.execute("SELECT COUNT(*) FROM price_reading").fetchone()[0] == total
        assert not os.path.exists(adir)

        # only old-a started more than 180 days before the end of 2026
        assert archive.archive_old_readings(conn, adir, 180, today="2026-12-31") == {"2026": 2}
        assert conn.execute("SELECT COUNT(*) FROM price_reading").fetchone()[0] == total - 2
        f = archive.archive_path(adir, 2026)
        assert os.path.exists(f) and not os.path.exists(archive.archive_path(adir, 2027))

        # later more has aged out: the 2026 file gains the December performance, a 2027
        # file appears, and a run with nothing new to move leaves the bytes alone
        assert archive.archive_old_readings(conn, adir, 180, today="2028-01-01") == {"2026": 2, "2027": 2}
        assert len(list(archive.read_archive(f))) == 4
        first = open(f, "rb").read()
        assert archive.archive_old_readings(conn, adir, 180, today="2028-01-01") == {}
        assert open(f, "rb").read() == first

        # restoring into a fresh database brings back identical readings
        other = db.connect(os.path.join(tmp, "other.db"))
        assert archive.restore(other, adir) == 6
        assert archive.restore(other, adir) == 0                      # already there
        got = [tuple(r) for r in other.execute(
            """SELECT p.external_id, r.first_seen, r.last_seen, r.min_price, r.price_bands_json,
                      r.first_days_to_perf, r.availability_band, r.on_sale
                 FROM price_reading r JOIN performance p ON p.id=r.performance_id
                ORDER BY 1, 2""")]
        assert got[:2] == [
            ("new", "2026-01-01T05:00:00+00:00", "2026-01-02T05:00:00+00:00", 29.5, "[29.5, 80.0]", 9,
             "Good availability", 1),
            ("new", "2026-01-03T05:00:00+00:00", "2026-01-03T05:00:00+00:00", 35.0, "[35.0, 80.0]", 9,
             "Good availability", 1)], got
        assert len(got) == 6
        conn.close(); other.close()


def _merge_module():
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
    import merge_sqlite_db
    return merge_sqlite_db


def test_merge_driver_unions_two_collectors():
    """The 05:00 CI run wrote more nights while a local clone was converted: the
    merge keeps both sides' readings, extends a reading the other side confirmed
    later, and accepts an other side still in the old layout."""
    merge_sqlite_db = _merge_module()
    with tempfile.TemporaryDirectory() as tmp:
        ours_p, theirs_p = os.path.join(tmp, "ours.db"), os.path.join(tmp, "theirs.db")
        _old_layout_db(theirs_p, SIGHTINGS)               # theirs: old layout, all the sightings
        ours = db.connect(ours_p)                          # ours: new layout, A's first nights only
        db.upsert_show(ours, "s", "atg", "S", "V", "{}")
        a = db.upsert_performance(ours, "s", "A", "2026-09-20T14:30:00", None)
        for ext, at, price, bands, band, on_sale in SIGHTINGS:
            if ext == "A" and at < "2026-09-04":
                db.record_price(ours, a, price, band, "u", price_bands=json.loads(bands),
                                days_to_perf=_days_to("2026-09-20T14:30:00", at), now=at)
        ours.commit()
        ours.close()

        added, skipped = merge_sqlite_db.merge(ours_p, theirs_p)
        assert skipped == 0 and added > 0
        merged = db.connect(ours_p)
        want = db.connect(theirs_p)                        # already converted by the merge
        cols = "p.external_id, r.first_seen, r.last_seen, r.min_price, r.availability_band"
        q = f"SELECT {cols} FROM price_reading r JOIN performance p ON p.id=r.performance_id ORDER BY 1, 2"
        assert [tuple(r) for r in merged.execute(q)] == [tuple(r) for r in want.execute(q)]
        merged.close(); want.close()


def test_merge_driver_cuts_back_a_reading_a_newer_one_overlaps():
    """One side kept confirming 29.50 to the 4th; the other saw it become 34.50
    on the 3rd. Both are real: the 29.50 reading is cut back to its last sighting
    before the 3rd, and its later sightings survive as a reading of their own."""
    merge_sqlite_db = _merge_module()

    def side(path, seen):
        c = db.connect(path)
        db.upsert_show(c, "s", "atg", "S", "V", "{}")
        pid = db.upsert_performance(c, "s", "A", "2026-09-20T14:30:00", None)
        for at, price in seen:
            db.record_price(c, pid, price, "Good availability", "u", days_to_perf=15, now=at)
        c.commit()
        c.close()

    with tempfile.TemporaryDirectory() as tmp:
        ours_p, theirs_p = os.path.join(tmp, "o.db"), os.path.join(tmp, "t.db")
        side(ours_p, [("2026-09-01T05:00:00+00:00", 29.5), ("2026-09-02T05:00:00+00:00", 29.5),
                      ("2026-09-03T05:00:00+00:00", 29.5), ("2026-09-04T05:00:00+00:00", 29.5)])
        side(theirs_p, [("2026-09-01T05:00:00+00:00", 29.5), ("2026-09-02T05:00:00+00:00", 29.5),
                        ("2026-09-03T06:00:00+00:00", 34.5)])
        merge_sqlite_db.merge(ours_p, theirs_p)
        c = db.connect(ours_p)
        rows = [(r["min_price"], r["first_seen"][:13], r["last_seen"][:13]) for r in c.execute(
            "SELECT * FROM price_reading ORDER BY first_seen, id")]
        assert rows == [(29.5, "2026-09-01T05", "2026-09-03T05"),
                        (34.5, "2026-09-03T06", "2026-09-03T06"),
                        (29.5, "2026-09-04T05", "2026-09-04T05")], rows
        # and the view shows the sightings in order, none of them twice
        seen = [(r["observed_at"][:13], r["min_price"]) for r in c.execute(
            "SELECT observed_at, min_price FROM price_observation ORDER BY observed_at")]
        assert seen == [("2026-09-01T05", 29.5), ("2026-09-02T05", 29.5), ("2026-09-03T05", 29.5),
                        ("2026-09-03T06", 34.5), ("2026-09-04T05", 29.5)], seen
        c.close()


def test_atg_calendar_unavailable():
    # Final URLs seen on 23 Sep 2026.
    check = ATGParser.calendar_unavailable
    assert check("https://www.atgtickets.com/shows/wicked/apollo-victoria-theatre"
                 "/calendar/2027-05-30") is None
    assert "closed" in check("https://www.atgtickets.com/shows/abigails-party"
                             "/harold-pinter-theatre/")
    assert "closed" in check("https://www.atgtickets.com/venues/tom-stoppard-theatre"
                             "/whats-on/")
    assert "Queue-it" in check("https://queue.atgtickets.com/?c=atgtickets&e=paddingtonsav")


def test_closed_shows_are_not_collected():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "shows.yaml")
        with open(path, "w", encoding="utf-8") as f:
            f.write("shows:\n"
                    "  - key: running\n    operator: atg\n"
                    "  - key: gone\n    closed: 2026-09-12\n    operator: atg\n")
        assert [s["key"] for s in load_shows(path)] == ["running"]


def test_tixtrack_retail_price_and_package_exclusion():
    """Real Hunger Games On Stage shape: `price` is 72.5 but the seat map
    sells that Band A seat at £75 (face + £2.50 fees), and the VIP level can
    only be bought as a package. Other operators keep the default behaviour."""
    inv = {"priceMaps": [
        {"price": 22.5, "displayRetailPrice": 25.0, "requirePackagePurchase": False},
        {"price": 72.5, "displayRetailPrice": 75.0, "requirePackagePurchase": False},
        {"price": 72.5, "displayRetailPrice": 75.0, "requirePackagePurchase": False},
        {"price": 210.0, "displayRetailPrice": 212.5, "requirePackagePurchase": True},
    ]}
    assert available_bands(inv) == [22.5, 72.5, 210.0]                  # old behaviour
    assert available_bands(inv, "displayRetailPrice", True) == [25.0, 75.0]
    assert cheapest_from_inventory(inv, "displayRetailPrice", True) == 25.0
    assert cheapest_from_inventory(inv) == 22.5
    # nothing but packages on sale: no price, not zero
    only_pkg = {"priceMaps": [{"price": 210.0, "requirePackagePurchase": True}]}
    assert cheapest_from_inventory(only_pkg, "price", True) is None
    assert available_bands(only_pkg, "price", True) is None
    # a level missing the chosen field is skipped, not read as zero
    assert available_bands({"priceMaps": [{"price": 30.0}]}, "displayRetailPrice") is None


def test_troubadour_parsers_read_the_headline_price():
    assert parsers.get("hungergames").PRICE_FIELD == "displayRetailPrice"
    assert parsers.get("hungergames").EXCLUDE_PACKAGES is True
    assert parsers.get("kx").PRICE_FIELD == "displayRetailPrice"
    assert parsers.get("comealive").PRICE_FIELD == "displayRetailPrice"
    # Everyone collected before keeps the face-value default, so history is comparable.
    for op in ("nimax", "dm", "nederlander", "shaftesbury", "charingcross", "menier",
               "sohoplace", "marylebone", "witness"):
        assert parsers.get(op).PRICE_FIELD == "price", op
        assert parsers.get(op).EXCLUDE_PACKAGES is False, op


def test_every_configured_show_can_be_collected():
    """A typo'd operator or a missing series code would otherwise fail on the
    night, after the unit tests passed. Keys must be unique too."""
    shows = load_shows()
    keys = [s["key"] for s in shows]
    assert len(keys) == len(set(keys)), "duplicate show key"
    for s in shows:
        P = parsers.get(s["operator"])          # raises on an unknown operator
        series = getattr(P, "SERIES_KEY", None)
        if series:
            assert s.get(series), f"{s['key']}: missing {series}"
            urls = P.calendar_urls(s, 1)
            assert urls and urls[0].startswith(P.BASE), s["key"]


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except AssertionError as e:
                fails += 1
                print(f"  FAIL  {name}: {e}")
            except Exception as e:  # noqa: BLE001
                fails += 1
                print(f"  ERROR {name}: {type(e).__name__}: {e}")
    print(f"\n{'All tests passed.' if not fails else str(fails) + ' failure(s).'}")
    raise SystemExit(1 if fails else 0)
