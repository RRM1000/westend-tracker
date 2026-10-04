"""Yearly compressed archive of old price readings.

The database only needs recent history: the website's readers look at the
next few months of performances and the last couple of months of readings.
Readings for performances played long ago are moved out of data/westend.db
into one gzipped CSV per year of the performance,

    data/archive/readings-2026.csv.gz

which is committed to git beside the database. Nothing is thrown away: every
column of price_reading comes with the show and performance it belonged to,
identified by the operator's own keys (show_key, external_id), not by this
database's row ids, so a file can be restored into any copy of the database.

    python -m wet.cli archive                       # move readings older than 180 days
    python -m wet.cli archive --older-than-days 90  # ... or a different cutoff
    python -m wet.cli archive --dry-run             # say what would move
    python -m wet.cli restore --year 2026 --database data/scratch.db

In Python:

    from wet import archive
    for row in archive.read_archive("data/archive/readings-2026.csv.gz"):
        ...   # a dict per reading: show_key, external_id, starts_at, first_seen, ...

What counts as old is the performance's start, not the reading's date: a
reading made in March for a performance in December is not old until that
performance is long past. Performance rows (and seat_state, which is tiny and
already stored as changes) stay in the database, so show and performance keys
remain resolvable; only the readings move.

The files are written deterministically (sorted rows, no timestamp in the gzip
header), so running the archive again with nothing new to move leaves them
byte-for-byte unchanged and git sees no change.
"""

import csv
import gzip
import io
from datetime import date, timedelta
from pathlib import Path

from . import db

COLUMNS = [
    "show_key", "external_id", "starts_at",
    "first_seen", "last_seen", "first_days_to_perf", "last_days_to_perf", "day_times",
    "min_price", "price_bands_json", "availability_band", "on_sale", "source_url",
]
DEFAULT_DIR = "data/archive"
DEFAULT_OLDER_THAN_DAYS = 180


def archive_path(directory, year) -> Path:
    return Path(directory) / f"readings-{year}.csv.gz"


def read_archive(path):
    """Yield one dict per archived reading, values as the strings in the file
    ('' meaning NULL). Use restore() to put them back into a database."""
    with gzip.open(path, "rt", encoding="utf-8", newline="") as f:
        yield from csv.DictReader(f)


def _write_archive(path: Path, rows) -> None:
    """Rows sorted by (show, performance, first_seen); gzip header carries no
    file name or time, so identical content is identical bytes."""
    buf = io.StringIO(newline="")
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(COLUMNS)
    for r in sorted(rows, key=lambda r: (r["show_key"], r["external_id"], r["first_seen"])):
        w.writerow([r[c] for c in COLUMNS])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=9, mtime=0) as gz:
            gz.write(buf.getvalue().encode("utf-8"))
    tmp.replace(path)


def _text(v):
    return "" if v is None else str(v)


def archive_old_readings(conn, directory=DEFAULT_DIR, older_than_days=DEFAULT_OLDER_THAN_DAYS,
                         dry_run=False, today=None) -> dict:
    """Move readings of performances that started more than older_than_days ago
    into the yearly files. Returns {year: readings moved} (empty when there is
    nothing old enough). The database rows are deleted only after the file
    holding them has been written and read back with the right row count."""
    cutoff = _cutoff(today or db.utcnow()[:10], older_than_days)
    rows = conn.execute(
        """SELECT r.id, s.show_key, s.external_id, s.starts_at,
                  r.first_seen, r.last_seen, r.first_days_to_perf, r.last_days_to_perf,
                  r.day_times, r.min_price, r.price_bands_json, r.availability_band,
                  r.on_sale, r.source_url
             FROM price_reading r JOIN performance s ON s.id = r.performance_id
            WHERE s.starts_at < ?""", (cutoff,)).fetchall()
    by_year: dict[str, list] = {}
    for r in rows:
        by_year.setdefault(r["starts_at"][:4], []).append(r)
    moved = {y: len(v) for y, v in sorted(by_year.items())}
    if dry_run or not rows:
        return moved

    for year, year_rows in by_year.items():
        path = archive_path(directory, year)
        merged = {}
        if path.exists():
            for old in read_archive(path):
                merged[(old["show_key"], old["external_id"], old["first_seen"])] = old
        for r in year_rows:
            row = {c: _text(r[c]) for c in COLUMNS}
            merged[(row["show_key"], row["external_id"], row["first_seen"])] = row
        _write_archive(path, merged.values())
        if sum(1 for _ in read_archive(path)) != len(merged):
            raise RuntimeError(f"{path}: read back a different number of rows than written; "
                               "database left untouched")
    ids = [(r["id"],) for r in rows]
    conn.executemany("DELETE FROM price_reading WHERE id=?", ids)
    conn.commit()
    return moved


def _cutoff(iso_date: str, days: int) -> str:
    """The start of the day `days` before iso_date, in the form performance.starts_at uses."""
    return (date.fromisoformat(iso_date[:10]) - timedelta(days=days)).isoformat() + "T00:00:00"


def restore(conn, directory=DEFAULT_DIR, year=None, show=None) -> int:
    """Put archived readings back into the database (skipping any already
    there). Creates the show and performance if the database lacks them.
    Returns the number of readings inserted. Restore into a scratch copy
    (--database data/scratch.db) unless you mean to: the nightly archive will
    move them out of the live database again."""
    paths = sorted(Path(directory).glob("readings-*.csv.gz"))
    if year is not None:
        paths = [p for p in paths if p.name == archive_path(".", year).name]
    inserted = 0
    for path in paths:
        for r in read_archive(path):
            if show and r["show_key"] != show:
                continue
            conn.execute(
                """INSERT OR IGNORE INTO show(key, operator, title, first_seen, last_seen)
                   VALUES (?, '', ?, ?, ?)""",
                (r["show_key"], r["show_key"], r["first_seen"], r["last_seen"]))
            conn.execute(
                """INSERT OR IGNORE INTO performance(show_key, external_id, starts_at)
                   VALUES (?,?,?)""", (r["show_key"], r["external_id"], r["starts_at"]))
            pid = conn.execute(
                "SELECT id FROM performance WHERE show_key=? AND external_id=?",
                (r["show_key"], r["external_id"])).fetchone()["id"]
            if conn.execute("SELECT 1 FROM price_reading WHERE performance_id=? AND first_seen=?",
                            (pid, r["first_seen"])).fetchone():
                continue
            num = lambda v, cast: cast(v) if v != "" else None
            conn.execute(
                """INSERT INTO price_reading
                   (performance_id, first_seen, last_seen, first_days_to_perf, last_days_to_perf,
                    day_times, min_price, price_bands_json, availability_band, on_sale, source_url)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (pid, r["first_seen"], r["last_seen"],
                 num(r["first_days_to_perf"], int), num(r["last_days_to_perf"], int),
                 r["day_times"], num(r["min_price"], float),
                 r["price_bands_json"] or None, r["availability_band"] or None,
                 int(r["on_sale"] or 1), r["source_url"] or None))
            inserted += 1
    conn.commit()
    return inserted
