"""The two collection passes."""

import json
from datetime import datetime
from pathlib import Path

import yaml

from . import db, parsers
from .browser import Session, Settings


def load_shows(path="config/shows.yaml") -> list[dict]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return raw.get("shows", [])


def _days_to(starts_at: str) -> int | None:
    try:
        return (datetime.fromisoformat(starts_at) - datetime.now()).days
    except (ValueError, TypeError):
        return None


async def collect_calendars(conn, session: Session, shows, months=6, verbose=True):
    """Phase 1. Cheap, fast, and the thing to run every day from now on.

    One page load per show per month. For the whole West End that is roughly
    300 loads a day — about one every five minutes.
    """
    ok = failed = 0
    for show in shows:
        P = parsers.get(show["operator"])
        db.upsert_show(conn, show["key"], show["operator"], show["title"],
                       show.get("venue"), json.dumps(show))
        # Commit now: a show with nothing on sale yet still belongs in the
        # registry, and otherwise it is rolled back with the empty months.
        conn.commit()
        for url in P.calendar_urls(show, months):
            try:
                page, _ = await session.load(
                    url, wait_for=getattr(P, 'calendar_wait_selector', lambda: None)())
            except Exception as e:  # noqa: BLE001
                failed += 1
                if verbose:
                    print(f"  ! {url}\n    {e}")
                continue
            try:
                try:
                    perfs = await P.parse_calendar(page, show)
                except Exception as e:  # noqa: BLE001
                    # A broken parser must not take the whole night's
                    # collection down with it — the other operators are
                    # still fine, and their history is unrecoverable.
                    failed += 1
                    if verbose:
                        print(f"  ! parser error on {show['key']}: "
                              f"{type(e).__name__}: {e}")
                    continue
                if perfs is None:
                    # Could not read the page — this is the silent-empty trap.
                    failed += 1
                    if verbose:
                        print(f"  ! could not read: {url}")
                    continue
                if not perfs:
                    # Read fine, nothing on that month. Normal, not a fault.
                    ok += 1
                    if verbose:
                        print(f"  . {show['key']:<28} nothing on this month")
                    continue
                for p in perfs:
                    pid = db.upsert_performance(conn, show["key"], p.external_id,
                                                p.starts_at, p.url)
                    db.record_price(conn, pid, p.min_price, p.availability_band,
                                    url, days_to_perf=_days_to(p.starts_at))
                conn.commit()
                ok += 1
                if verbose:
                    print(f"  + {show['key']:<28} {url.rsplit('/',1)[-1]:<14} "
                          f"{len(perfs):>3} performances")
            finally:
                await page.close()
    return {"ok": ok, "failed": failed}


async def collect_seats(conn, session: Session, shows, limit_per_show=None, verbose=True):
    """Phase 2. Heavier — a seat map is 600KB–1MB and needs the page to render.

    Run this against a focused set of shows, not everything, until the value
    is proven. Performances must already exist from a calendar pass.
    """
    ok = failed = 0
    for show in shows:
        P = parsers.get(show["operator"])
        rows = conn.execute(
            """SELECT id, external_id, url, starts_at FROM performance
               WHERE show_key=? AND starts_at >= datetime('now')
               ORDER BY starts_at""",
            (show["key"],),
        ).fetchall()
        if limit_per_show:
            rows = rows[:limit_per_show]

        for row in rows:
            url = row["url"]
            if not url or "/performance/" not in url and "/tickets/" not in url:
                continue
            try:
                page, _ = await session.load(url, wait_for=P.seat_wait_selector())
            except Exception as e:  # noqa: BLE001
                failed += 1
                if verbose:
                    print(f"  ! {url}\n    {e}")
                continue
            try:
                seats = await P.parse_seats(page)
                if not seats:
                    failed += 1
                    if verbose:
                        print(f"  ! no seats parsed: {url}")
                    continue
                stats = db.record_seats(conn, row["id"], seats, url)
                conn.commit()
                ok += 1
                if verbose:
                    pct = 100 * (1 - stats["available"] / stats["total"])
                    print(f"  + {show['key']:<28} {row['starts_at']}  "
                          f"{stats['total']:>5} seats  {pct:5.1f}% gone  "
                          f"{stats['changed']:>4} changed")
            finally:
                await page.close()
    return {"ok": ok, "failed": failed}
