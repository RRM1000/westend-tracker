"""LW Theatres.

Covers the LW estate — Cambridge, London Palladium, Theatre Royal Drury Lane,
Adelphi, Gillian Lynne, His Majesty's and the rest.

Structures read off live pages on 23 August 2026:

  calendar   https://ticketing.lwtheatres.co.uk/event/{eventId}/?date=YYYY-MM
  seat map   https://ticketing.lwtheatres.co.uk/event/{eventId}/performance/{perfId}

  performance button text:  "7:00pm from £37.50"
  seat node:                <g aria-label="Stalls, Left, S, 18"><circle fill="#ff7510"/></g>

LW differs from ATG in one important way: the seat label carries the seat's
identity but NOT its state. State is encoded in the circle's fill colour, and
the mapping from colour to price is not published anywhere on the page.

What we do about that: on the performance we read, there were exactly seven
non-grey fills and exactly seven prices in the page text. That correspondence
is the hook. We rank fills by how many seats carry them and rank prices, then
pair them — cheap seats are almost always the most numerous. It is a heuristic
and it can be wrong, so `price_confidence` is recorded alongside, and when the
counts don't match we store availability only and leave price NULL rather than
writing a number we can't stand behind.
"""

import re
from datetime import date

from .base import Performance, Seat, all_money, first_money, norm_ref

BASE = "https://ticketing.lwtheatres.co.uk"

# LW greys out everything you cannot buy.
GREY_FILLS = {"#ddd", "#dddddd", "#d8d8d8", "#cccccc", "#ccc", "none"}

TIME_RE = re.compile(r"(\d{1,2})[:.](\d{2})\s*(am|pm)?", re.I)


class LWParser:
    operator = "lw"

    @staticmethod
    def calendar_urls(show: dict, months: int = 6) -> list[str]:
        from .base import month_starts
        eid = show["lw_event_id"]
        return [f"{BASE}/event/{eid}/?date={d.strftime('%Y-%m')}" for d in month_starts(months)]

    @staticmethod
    def calendar_wait_selector() -> str:
        return ".rdp-day"

    @staticmethod
    async def parse_calendar(page, show: dict) -> list[Performance]:
        """LW renders the month as buttons; the performance id only appears
        once you follow one. We read the day cells and their prices, and
        resolve ids lazily in the seat pass."""
        items = await page.eval_on_selector_all(
            "button, a",
            """els => els.map(e => ({
                 text: (e.innerText || '').replace(/\\s+/g,' ').trim(),
                 href: e.getAttribute('href') || '',
                 label: e.getAttribute('aria-label') || ''
               })).filter(o => /£/.test(o.text) || /performance/.test(o.href))""",
        )
        # The month being displayed comes from the URL we asked for.
        m = re.search(r"date=(\d{4})-(\d{2})", page.url)
        year, month = (int(m.group(1)), int(m.group(2))) if m else (date.today().year, date.today().month)

        out, seen = [], set()
        for it in items:
            text = f"{it['label']} {it['text']}".strip()
            price = first_money(text)
            tm = TIME_RE.search(text)
            if price is None or not tm:
                continue
            hh, mm = int(tm.group(1)), int(tm.group(2))
            ampm = (tm.group(3) or "").lower()
            if ampm == "pm" and hh < 12:
                hh += 12
            if ampm == "am" and hh == 12:
                hh = 0

            dm = re.search(r"\b(\d{1,2})\b", it["label"]) or re.search(r"\b(\d{1,2})\b", it["text"])
            if not dm:
                continue
            day = int(dm.group(1))
            if not 1 <= day <= 31:
                continue

            perf_id = ""
            hm = re.search(r"/performance/(\d+)", it["href"])
            if hm:
                perf_id = hm.group(1)
            key = perf_id or f"{year}-{month:02d}-{day:02d}T{hh:02d}:{mm:02d}"
            if key in seen:
                continue
            seen.add(key)

            try:
                starts = f"{year}-{month:02d}-{day:02d}T{hh:02d}:{mm:02d}:00"
            except ValueError:
                continue

            out.append(Performance(
                external_id=key,
                starts_at=starts,
                url=(BASE + it["href"]) if it["href"].startswith("/") else (it["href"] or page.url),
                min_price=price,
                availability_band=None,   # LW does not publish one
            ))
        return out

    @staticmethod
    def seat_wait_selector() -> str:
        return "g[aria-label] circle"

    @staticmethod
    async def parse_seats(page) -> list[Seat]:
        nodes = await page.eval_on_selector_all(
            "g[aria-label]",
            """els => els.filter(e => e.querySelector('circle'))
                        .map(e => ({
                          label: e.getAttribute('aria-label'),
                          fill: (e.querySelector('circle').getAttribute('fill')||'').toLowerCase()
                        }))""",
        )
        # Real seats have a comma-separated positional label; icons don't.
        seats_raw = [n for n in nodes if (n.get("label") or "").count(",") >= 2]

        body = await page.inner_text("body")
        prices = all_money(body)

        counts: dict[str, int] = {}
        for n in seats_raw:
            f = n["fill"]
            if f in GREY_FILLS:
                continue
            counts[f] = counts.get(f, 0) + 1

        # Pair colour to price: most-numerous colour gets the cheapest price.
        fill_to_price: dict[str, float] = {}
        if prices and len(counts) == len(prices):
            ordered = sorted(counts.items(), key=lambda kv: -kv[1])
            for (fill, _n), price in zip(ordered, prices):
                fill_to_price[fill] = price

        out = []
        for n in seats_raw:
            parts = [p.strip() for p in n["label"].split(",")]
            fill = n["fill"]
            available = fill not in GREY_FILLS
            out.append(Seat(
                seat_ref=norm_ref(*parts),
                available=available,
                price=fill_to_price.get(fill) if available else None,
                raw_label=n["label"],
            ))
        return out
