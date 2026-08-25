"""Git merge driver for data/westend.db.

Two machines collect into the same SQLite file — the 05:00 GitHub Actions run,
and occasionally a local run. Git treats the database as an opaque binary and
declares a conflict, and the obvious resolutions are both wrong: "keep mine"
and "keep theirs" each throw away a day's readings that cannot be re-collected.

So this merges the *rows* instead: take one side as the base, then insert
every observation the other side has and it doesn't. Nothing is overwritten
and nothing is dropped.

Git invokes it as:  merge_sqlite_db.py %O %A %B
  %O  common ancestor   (unused — an append-only archive needs no 3-way logic)
  %A  "ours"            (also the OUTPUT file git reads back)
  %B  "theirs"
Exit 0 means merged cleanly.

Matching across the two files is by (show_key, external_id, observed_at), not
by row id: the two databases assigned their own AUTOINCREMENT ids, so ids do
not line up and merging on them would corrupt the join.
"""

import sqlite3
import sys


def _table_columns(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def merge(ours_path: str, theirs_path: str) -> tuple[int, int]:
    ours = sqlite3.connect(ours_path)
    ours.row_factory = sqlite3.Row
    theirs = sqlite3.connect(theirs_path)
    theirs.row_factory = sqlite3.Row

    # Make sure "ours" has every column "theirs" does, so a database written
    # before a migration can still absorb rows written after one.
    ours_cols = _table_columns(ours, "price_observation")
    theirs_cols = _table_columns(theirs, "price_observation")
    for missing in theirs_cols - ours_cols:
        ours.execute(f"ALTER TABLE price_observation ADD COLUMN {missing}")
    ours.commit()
    ours_cols = _table_columns(ours, "price_observation")

    # Shows and performances first — a reading is useless without its parent.
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
               for r in ours.execute(
                   "SELECT id, show_key, external_id FROM performance")}
    seen = {(r["performance_id"], r["observed_at"])
            for r in ours.execute(
                "SELECT performance_id, observed_at FROM price_observation")}

    shared = [c for c in
              ("days_to_perf", "min_price", "price_bands_json",
               "availability_band", "on_sale", "source_url")
              if c in ours_cols and c in theirs_cols]

    inserted = skipped = 0
    for r in theirs.execute("""SELECT o.*, p.show_key, p.external_id
                               FROM price_observation o
                               JOIN performance p ON p.id = o.performance_id"""):
        tid = perf_id.get((r["show_key"], r["external_id"]))
        if tid is None:
            skipped += 1
            continue
        if (tid, r["observed_at"]) in seen:
            continue
        cols = ["performance_id", "observed_at"] + shared
        vals = [tid, r["observed_at"]] + [r[c] for c in shared]
        ours.execute(
            f"INSERT INTO price_observation ({','.join(cols)}) "
            f"VALUES ({','.join('?' * len(cols))})", vals)
        seen.add((tid, r["observed_at"]))
        inserted += 1

    ours.commit()
    ours.close()
    theirs.close()
    return inserted, skipped


def main(argv):
    if len(argv) < 4:
        print("usage: merge_sqlite_db.py <ancestor> <ours/output> <theirs>",
              file=sys.stderr)
        return 2
    _ancestor, ours, theirs = argv[1], argv[2], argv[3]
    try:
        inserted, skipped = merge(ours, theirs)
    except Exception as e:  # noqa: BLE001
        # Fail loudly. A silent failure here would resolve the conflict by
        # quietly keeping one side, which is the exact data loss this exists
        # to prevent.
        print(f"sqlite merge FAILED: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print(f"sqlite merge: added {inserted} readings from the other side"
          + (f" ({skipped} had no matching performance)" if skipped else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
