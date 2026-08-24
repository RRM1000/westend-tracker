"""Tests for the parts that don't need a browser.

The DOM parsing needs a real page, so it's covered by `wet.cli probe` rather
than unit tests. Everything below is pure logic and runs anywhere.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wet import db
from wet.parsers.atg import seat_ref_for
from wet.parsers.spektrix import cheapest_public_price
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


def test_price_observations_are_append_only():
    with tempfile.TemporaryDirectory() as tmp:
        conn = db.connect(os.path.join(tmp, "t.db"))
        pid = _seed(conn)
        db.record_price(conn, pid, 59.50, "Medium availability", "u")
        db.record_price(conn, pid, 45.00, "Good availability", "u")
        rows = conn.execute(
            "SELECT min_price FROM price_observation WHERE performance_id=? ORDER BY id",
            (pid,)).fetchall()
        assert [r["min_price"] for r in rows] == [59.50, 45.00]
        conn.close()


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
