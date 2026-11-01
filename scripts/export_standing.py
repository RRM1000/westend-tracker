"""Standing places, for the London Theatre Geek admin's Standing tickets tab.

    python scripts/export_standing.py                         # data/standing.json
    python scripts/export_standing.py --hourly-log run.txt    # add to data/standing-hourly.csv

The tab reads both files from this public repo, so no database passwords are
needed here. The daily collection writes standing.json from seat_state; the
hourly job only appends to standing-hourly.csv. Keeping them apart means the two
jobs never change the same file, so their commits can't conflict.
"""
import argparse
import csv
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB = "data/westend.db"
OUT = Path("data/standing.json")
HOURLY = Path("data/standing-hourly.csv")
DAYS = 45

# "  + rent-tom-stoppard   2026-10-28T19:30:00  standing 10/14 available at £20  (14 changed)"
LINE = re.compile(r"^\s*\+\s+(\S+)\s+(\S+)\s+standing\s+(\d+)/(\d+) available(?: at \D?([\d.]+))?")


def append_hourly(log_path: str) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = []
    for line in Path(log_path).read_text(encoding="utf8", errors="replace").splitlines():
        m = LINE.match(line)
        if m:
            key, starts, on_sale, places, price = m.groups()
            rows.append([key, starts, now, on_sale, places, price or ""])
    new = not HOURLY.exists()
    with HOURLY.open("a", newline="", encoding="utf8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["show", "starts_at", "read_at", "on_sale", "places", "price"])
        w.writerows(rows)
    print(f"{len(rows)} reading(s) added to {HOURLY}")


def export() -> None:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    since = (datetime.now(timezone.utc) - timedelta(days=DAYS)).strftime("%Y-%m-%d")
    # seat_state stores changes, so each seat's latest row is its current state.
    rows = conn.execute(
        """SELECT p.show_key, p.starts_at, s.seat_ref, s.observed_at, s.available, s.price
           FROM seat_state s JOIN performance p ON p.id = s.performance_id
           WHERE lower(s.seat_ref) LIKE '%stand%' AND p.starts_at >= ?
           ORDER BY s.observed_at""",
        (since,),
    ).fetchall()
    shows: dict = {}
    for r in rows:
        show = shows.setdefault(r["show_key"], {})
        perf = show.setdefault(r["starts_at"], {"seats": {}, "first_on_sale": None, "max_on_sale": 0, "price": None})
        perf["seats"][r["seat_ref"]] = r["available"]
        on_sale = sum(1 for v in perf["seats"].values() if v)
        if on_sale and perf["first_on_sale"] is None:
            perf["first_on_sale"] = r["observed_at"]
        perf["max_on_sale"] = max(perf["max_on_sale"], on_sale)
        if r["available"] and r["price"]:
            perf["price"] = r["price"]
    out = {}
    for key, perfs in shows.items():
        with_places = [
            {"starts_at": s, "max_on_sale": p["max_on_sale"], "places": len(p["seats"]),
             "price": p["price"], "first_on_sale": p["first_on_sale"]}
            for s, p in sorted(perfs.items()) if p["max_on_sale"]
        ]
        out[key] = {
            "performances": len(perfs),
            "places": max(len(p["seats"]) for p in perfs.values()),
            "with_places": with_places,
        }
    OUT.write_text(json.dumps(
        {"generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "days": DAYS, "shows": out},
        indent=1), encoding="utf8")
    print(f"{OUT}: {len(out)} shows")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--hourly-log")
    args = ap.parse_args()
    if args.hourly_log:
        append_hourly(args.hourly_log)
    else:
        export()
