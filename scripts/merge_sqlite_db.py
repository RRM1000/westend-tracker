"""Git merge driver for data/westend.db.

Two machines collect into the same SQLite file -- the 05:00 GitHub Actions run,
and occasionally a local run. Git treats the database as an opaque binary and
declares a conflict, and the obvious resolutions are both wrong: "keep mine"
and "keep theirs" each throw away a day's readings that cannot be re-collected.

So this merges the *rows* instead: take one side as the base, then add every
reading the other side has and it doesn't. Nothing is overwritten and nothing
is dropped.

Git invokes it as:  merge_sqlite_db.py %O %A %B
  %O  common ancestor   (unused -- an append-only archive needs no 3-way logic)
  %A  "ours"            (also the OUTPUT file git reads back)
  %B  "theirs"
Exit 0 means merged cleanly.

Readings (table price_reading) are matched across the two files by
(show_key, external_id, first_seen), not by row id: the two databases assigned
their own AUTOINCREMENT ids, so ids do not line up and merging on them would
corrupt the join. A reading both sides have is the same stretch of time seen
from two machines; the one that was confirmed later (greater last_seen) wins,
as it covers the other. A reading only the other side has is added.

A file still in the old one-row-per-sighting layout (written by a clone that
has not pulled the change to readings) is converted first, in place, by
wet.db.connect -- the same conversion the collector applies on opening it.

Overlaps. The two sides can disagree about what happened after a point both
agree on: one machine kept confirming reading R until Tuesday while the other
saw the price change on Monday. Both are real sightings, so rather than let R
cover Monday and Tuesday over the top of the newer reading, R is cut back to
its last sighting before the newer reading began, and its final sighting is
kept as a one-sighting reading of its own (see _resolve_overlaps).

seat_state rows (standing places) are unioned by (performance, seat, time).
"""

import os
import sqlite3
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wet import db  # noqa: E402


def _sightings(r):
    """Every sighting time a reading still knows about, oldest first: its
    first, the last of each day before the last day, and its last."""
    out = [r["first_seen"]]
    day = r["first_seen"][:10]
    times = r["day_times"] or ""
    for k in range(len(times) // 5):
        d = (datetime.fromisoformat(day) + timedelta(days=k)).date().isoformat()
        secs = int(times[5 * k:5 * k + 5])
        out.append(f"{d}T{secs // 3600:02d}:{secs % 3600 // 60:02d}:{secs % 60:02d}+00:00")
    out.append(r["last_seen"])
    return sorted(set(out))


def _days_to(starts_at, seen):
    run = datetime.fromisoformat(seen).replace(tzinfo=None)
    return (datetime.fromisoformat(starts_at) - run).days


def _resolve_overlaps(conn) -> int:
    """Make each performance's readings run strictly one after another."""
    fixed = 0
    perfs = [r[0] for r in conn.execute("SELECT DISTINCT performance_id FROM price_reading")]
    for pid in perfs:
        starts = conn.execute("SELECT starts_at FROM performance WHERE id=?", (pid,)).fetchone()[0]
        rows = conn.execute("SELECT * FROM price_reading WHERE performance_id=? "
                            "ORDER BY first_seen, id", (pid,)).fetchall()
        for a, b in zip(rows, rows[1:]):
            if a["last_seen"] < b["first_seen"]:
                continue
            known = _sightings(a)
            kept = [t for t in known if t < b["first_seen"]]
            tail = [t for t in known if t >= b["first_seen"]]
            new_last = kept[-1]
            first_day = a["first_seen"][:10]
            day_times = "".join(
                f"{db._second_of_day(t):05d}"
                for t in _last_per_day(kept) if first_day <= t[:10] < new_last[:10])
            conn.execute(
                "UPDATE price_reading SET last_seen=?, last_days_to_perf=?, day_times=? WHERE id=?",
                (new_last, _days_to(starts, new_last), day_times, a["id"]))
            # the later sightings of the cut-back reading survive as a point
            conn.execute(
                """INSERT INTO price_reading
                   (performance_id, first_seen, last_seen, first_days_to_perf, last_days_to_perf,
                    day_times, min_price, price_bands_json, availability_band, on_sale, source_url)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (pid, tail[-1], tail[-1], _days_to(starts, tail[-1]), _days_to(starts, tail[-1]), "",
                 a["min_price"], a["price_bands_json"], a["availability_band"],
                 a["on_sale"], a["source_url"]))
            fixed += 1
    return fixed


def _last_per_day(times):
    by_day = {}
    for t in sorted(times):
        by_day[t[:10]] = t
    return [by_day[d] for d in sorted(by_day)]


def merge(ours_path: str, theirs_path: str) -> tuple[int, int]:
    """Merge theirs into ours (written in place). Returns (readings added or
    extended, readings whose performance was unknown and were skipped)."""
    ours = db.connect(ours_path)
    theirs = db.connect(theirs_path)

    # Shows and performances first -- a reading is useless without its parent.
    for r in theirs.execute("SELECT * FROM show"):
        ours.execute(
            """INSERT INTO show(key, operator, title, venue, config_json,
                                first_seen, last_seen)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(key) DO NOTHING""",
            (r["key"], r["operator"], r["title"], r["venue"], r["config_json"],
             r["first_seen"], r["last_seen"]))
    ours.commit()

    for r in theirs.execute("SELECT * FROM performance"):
        ours.execute(
            """INSERT INTO performance(show_key, external_id, starts_at, url)
               VALUES (?,?,?,?)
               ON CONFLICT(show_key, external_id) DO NOTHING""",
            (r["show_key"], r["external_id"], r["starts_at"], r["url"]))
    ours.commit()

    perf_id = {(r["show_key"], r["external_id"]): r["id"]
               for r in ours.execute("SELECT id, show_key, external_id FROM performance")}
    have = {(r["performance_id"], r["first_seen"]): (r["id"], r["last_seen"])
            for r in ours.execute("SELECT id, performance_id, first_seen, last_seen FROM price_reading")}

    added = skipped = 0
    for r in theirs.execute("""SELECT x.*, p.show_key, p.external_id
                               FROM price_reading x
                               JOIN performance p ON p.id = x.performance_id"""):
        pid = perf_id.get((r["show_key"], r["external_id"]))
        if pid is None:
            skipped += 1
            continue
        mine = have.get((pid, r["first_seen"]))
        if mine is not None:
            if r["last_seen"] > mine[1]:
                ours.execute(
                    "UPDATE price_reading SET last_seen=?, last_days_to_perf=?, day_times=? WHERE id=?",
                    (r["last_seen"], r["last_days_to_perf"], r["day_times"], mine[0]))
                added += 1
            continue
        ours.execute(
            """INSERT INTO price_reading
               (performance_id, first_seen, last_seen, first_days_to_perf, last_days_to_perf,
                day_times, min_price, price_bands_json, availability_band, on_sale, source_url)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (pid, r["first_seen"], r["last_seen"], r["first_days_to_perf"],
             r["last_days_to_perf"], r["day_times"], r["min_price"], r["price_bands_json"],
             r["availability_band"], r["on_sale"], r["source_url"]))
        added += 1
    ours.commit()

    # Standing places: a seat's state at a moment, matched like readings are.
    have_seats = {(r["performance_id"], r["seat_ref"], r["observed_at"])
                  for r in ours.execute("SELECT performance_id, seat_ref, observed_at FROM seat_state")}
    for r in theirs.execute("""SELECT s.*, p.show_key, p.external_id
                               FROM seat_state s JOIN performance p ON p.id = s.performance_id"""):
        pid = perf_id.get((r["show_key"], r["external_id"]))
        if pid is not None and (pid, r["seat_ref"], r["observed_at"]) not in have_seats:
            ours.execute(
                """INSERT INTO seat_state(performance_id, seat_ref, observed_at, available, price)
                   VALUES (?,?,?,?,?)""",
                (pid, r["seat_ref"], r["observed_at"], r["available"], r["price"]))
    ours.commit()

    overlaps = _resolve_overlaps(ours)
    if overlaps:
        print(f"sqlite merge: {overlaps} reading(s) overlapped a newer one and were cut back")
    ours.commit()

    # Git reads the single file back: no WAL left behind.
    ours.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    ours.execute("PRAGMA journal_mode=DELETE")
    ours.close()
    theirs.close()
    return added, skipped


def main(argv):
    if len(argv) < 4:
        print("usage: merge_sqlite_db.py <ancestor> <ours/output> <theirs>",
              file=sys.stderr)
        return 2
    _ancestor, ours, theirs = argv[1], argv[2], argv[3]
    try:
        added, skipped = merge(ours, theirs)
    except Exception as e:  # noqa: BLE001
        # Fail loudly. A silent failure here would resolve the conflict by
        # quietly keeping one side, which is the exact data loss this exists
        # to prevent.
        print(f"sqlite merge FAILED: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print(f"sqlite merge: added or extended {added} readings from the other side"
          + (f" ({skipped} had no matching performance)" if skipped else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
