"""The two collection passes."""

import asyncio
import json
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from . import db, parsers
from .browser import Session, Settings, Unavailable


# Two months in a row that fail even after retries means the show is broken,
# not the network. Each failure costs about three minutes of timeouts.
GIVE_UP_AFTER = 2


def load_shows(path="config/shows.yaml") -> list[dict]:
    """Shows still to collect. An entry with `closed:` stays in the file as a
    record of its key, but is no longer fetched; its history stays in the
    database."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return [s for s in raw.get("shows", []) if not s.get("closed")]


def _days_to(starts_at: str) -> int | None:
    try:
        return (datetime.fromisoformat(starts_at) - datetime.now()).days
    except (ValueError, TypeError):
        return None


async def collect_calendars(conn, session: Session, shows, months=6, verbose=True,
                            sites_at_once=6):
    """Phase 1. Cheap, fast, and the thing to run every day from now on.

    One page load per show per month, roughly 350 loads a day. Shows are
    grouped by the ticketing site they are read from: within a site, pages
    load one at a time with the polite delay between them (Session throttles
    per host); different sites run side by side, sites_at_once at a time. So
    each site sees exactly the traffic it did when everything ran in one
    queue, and the night takes as long as the busiest site, not all of them.
    """
    stats = {"ok": 0, "failed": 0}

    groups: dict[str, list[dict]] = {}
    for show in shows:
        P = parsers.get(show["operator"])
        urls = list(P.calendar_urls(show, months))
        host = urlsplit(urls[0]).hostname if urls else show["operator"]
        groups.setdefault(host or show["operator"], []).append(show)

    gate = asyncio.Semaphore(max(1, sites_at_once))

    async def run_site(site_shows):
        async with gate:
            for show in site_shows:
                await _collect_show_calendar(conn, session, show, months, verbose, stats)

    await asyncio.gather(*(run_site(g) for g in groups.values()))
    return stats


async def _collect_show_calendar(conn, session, show, months, verbose, stats):
    P = parsers.get(show["operator"])
    db.upsert_show(conn, show["key"], show["operator"], show["title"],
                   show.get("venue"), json.dumps(show))
    # Commit now: a show with nothing on sale yet still belongs in the
    # registry, and otherwise it is rolled back with the empty months.
    conn.commit()
    in_a_row = 0
    for url in P.calendar_urls(show, months):
        try:
            page, _ = await session.load(
                url, wait_for=getattr(P, 'calendar_wait_selector', lambda: None)(),
                unavailable=getattr(P, 'calendar_unavailable', None))
        except Unavailable as e:
            # Every other month would get the same answer.
            stats["failed"] += 1
            if verbose:
                print(f"  ! {show['key']:<28} skipped: {e}")
            return
        except Exception as e:  # noqa: BLE001
            stats["failed"] += 1
            in_a_row += 1
            if verbose:
                print(f"  ! {url}\n    {e}")
            if in_a_row >= GIVE_UP_AFTER:
                if verbose:
                    print(f"  ! {show['key']:<28} skipped the remaining months "
                          f"after {in_a_row} failures in a row")
                return
            continue
        in_a_row = 0
        try:
            try:
                perfs = await P.parse_calendar(page, show)
            except Exception as e:  # noqa: BLE001
                # A broken parser must not take the whole night's
                # collection down with it: the other operators are
                # still fine, and their history is unrecoverable.
                stats["failed"] += 1
                if verbose:
                    print(f"  ! parser error on {show['key']}: "
                          f"{type(e).__name__}: {e}")
                continue
            if perfs is None:
                # Could not read the page: this is the silent-empty trap.
                stats["failed"] += 1
                if verbose:
                    print(f"  ! could not read: {url}")
                continue
            if not perfs:
                # Read fine, nothing on that month. Normal, not a fault.
                stats["ok"] += 1
                if verbose:
                    print(f"  . {show['key']:<28} nothing on this month")
                continue
            # No await between here and the commit, so another site's
            # coroutine can never commit half of this page's rows.
            for p in perfs:
                pid = db.upsert_performance(conn, show["key"], p.external_id,
                                            p.starts_at, p.url)
                db.record_price(conn, pid, p.min_price, p.availability_band,
                                url, days_to_perf=_days_to(p.starts_at),
                                price_bands=p.price_bands)
            conn.commit()
            stats["ok"] += 1
            if verbose:
                print(f"  + {show['key']:<28} {url.rsplit('/',1)[-1]:<14} "
                      f"{len(perfs):>3} performances")
        finally:
            await page.close()


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
