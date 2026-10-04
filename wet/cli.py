"""Command line.

    python -m wet.cli calendars            # phase 1, run this daily
    python -m wet.cli seats --limit 6      # phase 2, focused
    python -m wet.cli report               # what's in the database
    python -m wet.cli probe <url>          # debug a page that won't parse
    python -m wet.cli compact              # convert/shrink the database (VACUUM)
    python -m wet.cli archive              # move old readings to data/archive/
    python -m wet.cli restore --year 2026  # ... and read them back
"""

import argparse
import asyncio
import sys

import os

from . import archive as archive_mod
from . import db
from .browser import Session, Settings
from .collect import collect_calendars, collect_seats, collect_standing, load_shows


async def _run_calendars(args):
    settings = Settings()
    conn = db.connect(args.database)
    shows = load_shows(args.shows)
    if args.only:
        shows = [s for s in shows if s["key"] in args.only]
    print(f"Collecting calendars for {len(shows)} show(s), {args.months} months ahead\n")
    async with Session(settings) as session:
        stats = await collect_calendars(conn, session, shows, months=args.months,
                                        sites_at_once=settings.sites_at_once)
    print(f"\nDone. {stats['ok']} pages ok, {stats['failed']} failed.")


async def _run_seats(args):
    settings = Settings()
    conn = db.connect(args.database)
    shows = load_shows(args.shows)
    if args.only:
        shows = [s for s in shows if s["key"] in args.only]
    print(f"Collecting seat maps for {len(shows)} show(s)\n")
    async with Session(settings) as session:
        stats = await collect_seats(conn, session, shows, limit_per_show=args.limit)
    print(f"\nDone. {stats['ok']} maps ok, {stats['failed']} failed.")


async def _run_standing(args):
    settings = Settings()
    conn = db.connect(args.database)
    shows = [s for s in load_shows(args.shows) if s.get("track_standing")]
    print(f"Reading standing places for {len(shows)} show(s), next {args.days} days\n")
    async with Session(settings) as session:
        stats = await collect_standing(conn, session, shows, days=args.days)
    print(f"\nDone. {stats['ok']} maps ok, {stats['failed']} failed.")


async def _run_probe(args):
    """Load one page and print what a parser would see. Use this when a
    selector stops matching — which will happen, because sites get redesigned."""
    settings = Settings()
    settings.headless = args.headed is False
    async with Session(settings) as session:
        page, html = await session.load(args.url, wait_for=args.wait, settle_ms=2500)
        try:
            print(f"URL           {page.url}")
            print(f"HTML bytes    {len(html):,}")
            counts = await page.evaluate(
                """() => ({
                     svg: document.querySelectorAll('svg').length,
                     circles: document.querySelectorAll('circle').length,
                     labelled: document.querySelectorAll('g[aria-label]').length,
                     ticketLinks: document.querySelectorAll('a[href*="/tickets/"]').length
                   })"""
            )
            for k, v in counts.items():
                print(f"{k:<14}{v:,}")
            labels = await page.eval_on_selector_all(
                "g[aria-label]",
                """els => els.filter(e=>e.querySelector('circle')).slice(0,8)
                            .map(e => e.getAttribute('aria-label') + '  fill=' +
                                 e.querySelector('circle').getAttribute('fill'))""",
            )
            print("\nFirst seat nodes:")
            for l in labels or ["  (none found)"]:
                print(f"  {l}")
            body = await page.inner_text("body")
            prices = sorted({p for p in body.split() if p.startswith("£")})
            print(f"\nPrices in page text: {', '.join(prices[:20]) or '(none)'}")
        finally:
            await page.close()


def _report(args):
    conn = db.connect(args.database)
    q = conn.execute("""
        SELECT s.title, COUNT(DISTINCT p.id) AS perfs,
               COUNT(o.id) AS observations,
               MIN(o.observed_at) AS since,
               ROUND(MIN(o.min_price),2) AS cheapest,
               ROUND(MAX(o.min_price),2) AS dearest
        FROM show s
        LEFT JOIN performance p ON p.show_key = s.key
        LEFT JOIN price_observation o ON o.performance_id = p.id
        GROUP BY s.key ORDER BY s.title""").fetchall()
    if not q:
        print("Nothing collected yet. Run:  python -m wet.cli calendars")
        return
    print(f"{'Show':<32}{'Perfs':>7}{'Obs':>8}{'From':>9}{'To':>9}  Collecting since")
    print("-" * 90)
    for r in q:
        print(f"{(r['title'] or '')[:31]:<32}{r['perfs']:>7}{r['observations']:>8}"
              f"{(r['cheapest'] or 0):>9.2f}{(r['dearest'] or 0):>9.2f}  {r['since'] or '-'}")
    snaps = conn.execute("SELECT COUNT(*) c FROM seat_snapshot").fetchone()["c"]
    states = conn.execute("SELECT COUNT(*) c FROM seat_state").fetchone()["c"]
    print(f"\nSeat snapshots: {snaps:,}   seat state rows: {states:,}")


def _history(args):
    """Show the price time series — the thing the whole project exists to build.

    Without a show key you get one line per day collected, which answers
    "is it running, and is the market moving". With --show you get a grid:
    performances down the side, collection dates across, price in the cells.
    """
    conn = db.connect(args.database)

    rows = conn.execute("""
        SELECT p.show_key, s.title, p.starts_at,
               date(o.observed_at) AS day,
               MIN(o.min_price)    AS price
        FROM price_observation o
        JOIN performance p ON p.id = o.performance_id
        JOIN show s        ON s.key = p.show_key
        WHERE o.min_price IS NOT NULL
        GROUP BY p.id, date(o.observed_at)
        ORDER BY p.starts_at, day""").fetchall()

    if not rows:
        print("No prices collected yet. Run:  python -m wet.cli calendars")
        return

    days = sorted({r["day"] for r in rows})

    if not args.show:
        print(f"Collected on {len(days)} day(s).\n\n")
        print(f"{'Date collected':<16}{'Show':<26}{'Perfs':>7}{'Cheapest':>10}{'Dearest':>9}")
        print("-" * 68)
        per = conn.execute("""
            SELECT date(o.observed_at) AS day, s.title,
                   COUNT(DISTINCT p.id) AS perfs,
                   MIN(o.min_price) AS lo, MAX(o.min_price) AS hi
            FROM price_observation o
            JOIN performance p ON p.id = o.performance_id
            JOIN show s        ON s.key = p.show_key
            WHERE o.min_price IS NOT NULL
            GROUP BY day, s.key ORDER BY day, s.title""").fetchall()
        for r in per:
            print(f"{r['day']:<16}{(r['title'] or '')[:25]:<26}{r['perfs']:>7}"
                  f"{r['lo']:>10.2f}{r['hi']:>9.2f}")
        print("\nFor a single show, day by day:")
        print("  python -m wet.cli history --show <key>   (keys: "
              + ", ".join(sorted({r['show_key'] for r in rows})) + ")")
        return

    rows = [r for r in rows if r["show_key"] == args.show]
    if not rows:
        print(f"Nothing collected for show key {args.show!r}.")
        return

    grid: dict[str, dict[str, float]] = {}
    for r in rows:
        grid.setdefault(r["starts_at"], {})[r["day"]] = r["price"]

    title = rows[0]["title"]
    print(f"{title} — cheapest ticket, by day collected")
    print("Blank = not collected that day. A price that falls means it got cheaper.\n\n")

    head = f"{'Performance':<20}" + "".join(f"{d[5:]:>9}" for d in days) + f"{'change':>9}"
    print(head)
    print("-" * len(head))

    for starts in sorted(grid)[: args.limit]:
        cells = grid[starts]
        line = f"{starts.replace('T', ' ')[:16]:<20}"
        for d in days:
            line += f"{cells[d]:>9.2f}" if d in cells else f"{'-':>9}"
        seen = [cells[d] for d in days if d in cells]
        move = (seen[-1] - seen[0]) if len(seen) > 1 else None
        line += f"{move:>+9.2f}" if move else f"{'':>9}"
        print(line)

    if len(grid) > args.limit:
        print(f"\n... {len(grid) - args.limit} more performances "
              f"(use --limit {len(grid)} to see all)")


def _export(args):
    from . import export
    conn = db.connect(args.database)
    n = conn.execute("SELECT COUNT(*) c FROM price_observation").fetchone()["c"]
    if not n:
        print("Nothing collected yet. Run:  python -m wet.cli calendars")
        return
    out = export.write_html(conn, args.out)
    print(f"Wrote {out}")
    print("Open it by double-clicking, or run:  start " + args.out)


def _size_mb(path):
    return os.path.getsize(path) / 1e6 if os.path.exists(path) else 0.0


def _vacuum(conn, path):
    """Checkpoint the WAL into the main file and rewrite it compactly, so the
    one file git commits is complete and as small as it can be."""
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("VACUUM")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def _compact(args):
    """Opening the database converts an old-layout file to readings (db.connect
    does it, after checking the result reproduces every sighting). This makes
    sure that has happened and then VACUUMs."""
    before = _size_mb(args.database)
    conn = db.connect(args.database)
    n = conn.execute("SELECT COUNT(*) c FROM price_reading").fetchone()["c"]
    _vacuum(conn, args.database)
    conn.close()
    print(f"{args.database}: {n:,} readings. {before:.1f} MB -> {_size_mb(args.database):.1f} MB")


def _archive(args):
    conn = db.connect(args.database)
    moved = archive_mod.archive_old_readings(
        conn, directory=args.dir, older_than_days=args.older_than_days, dry_run=args.dry_run)
    if not moved:
        print(f"Nothing to archive: no readings for performances older than "
              f"{args.older_than_days} days.")
    for year, n in moved.items():
        print(f"{'Would move' if args.dry_run else 'Moved'} {n:,} readings to "
              f"{archive_mod.archive_path(args.dir, year)}")
    if moved and not args.dry_run:
        before = _size_mb(args.database)
        _vacuum(conn, args.database)
        print(f"{args.database}: {before:.1f} MB -> {_size_mb(args.database):.1f} MB")
    conn.close()


def _restore(args):
    conn = db.connect(args.database)
    n = archive_mod.restore(conn, directory=args.dir, year=args.year, show=args.show)
    print(f"Restored {n:,} readings into {args.database}")
    conn.close()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="wet", description="West End ticket data collector")
    ap.add_argument("--database", default="data/westend.db")
    ap.add_argument("--shows", default="config/shows.yaml")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("calendars", help="collect from-prices and availability (cheap, daily)")
    c.add_argument("--months", type=int, default=6)
    c.add_argument("--only", nargs="*", help="limit to these show keys")

    s = sub.add_parser("seats", help="collect per-seat maps (heavier)")
    s.add_argument("--limit", type=int, default=6, help="performances per show")
    s.add_argument("--only", nargs="*")

    st = sub.add_parser("standing", help="record standing places for shows marked track_standing")
    st.add_argument("--days", type=int, default=8, help="performances this many days ahead")

    p = sub.add_parser("probe", help="inspect one page to fix a broken selector")
    p.add_argument("url")
    p.add_argument("--wait", default=None, help="CSS selector to wait for")
    p.add_argument("--headed", action="store_true", help="show the browser")

    sub.add_parser("report", help="summarise what has been collected")

    e = sub.add_parser("export", help="write a visual HTML report you can open in a browser")
    e.add_argument("--out", default="data/report.html", help="file to write")

    h = sub.add_parser("history", help="show prices day by day (the time series)")
    h.add_argument("--show", help="show key, for a per-performance grid")
    h.add_argument("--limit", type=int, default=25, help="performances to show")

    sub.add_parser("compact", help="convert an old-layout database to readings and VACUUM it")

    a = sub.add_parser("archive", help="move readings of long-past performances to data/archive/")
    a.add_argument("--older-than-days", type=int, default=archive_mod.DEFAULT_OLDER_THAN_DAYS,
                   help="performances that started more than this many days ago (default %(default)s)")
    a.add_argument("--dir", default=archive_mod.DEFAULT_DIR)
    a.add_argument("--dry-run", action="store_true", help="say what would move, change nothing")

    r = sub.add_parser("restore", help="read archived readings back into a database")
    r.add_argument("--year", type=int, help="only this year's file (default: all)")
    r.add_argument("--show", help="only this show key")
    r.add_argument("--dir", default=archive_mod.DEFAULT_DIR)

    args = ap.parse_args(argv)
    if args.cmd == "compact":
        _compact(args)
    elif args.cmd == "archive":
        _archive(args)
    elif args.cmd == "restore":
        _restore(args)
    elif args.cmd == "calendars":
        asyncio.run(_run_calendars(args))
    elif args.cmd == "seats":
        asyncio.run(_run_seats(args))
    elif args.cmd == "standing":
        asyncio.run(_run_standing(args))
    elif args.cmd == "probe":
        asyncio.run(_run_probe(args))
    elif args.cmd == "report":
        _report(args)
    elif args.cmd == "history":
        _history(args)
    elif args.cmd == "export":
        _export(args)


if __name__ == "__main__":
    sys.exit(main())
