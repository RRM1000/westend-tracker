"""ATG Tickets.

Covers the ATG estate — Apollo Victoria, Lyceum, Savoy, Piccadilly, Phoenix,
Harold Pinter, Duke of York's, Fortune, Ambassadors, Playhouse and the rest.

Structures below were read off live pages on 23 August 2026:

  calendar   https://www.atgtickets.com/shows/{show}/{venue}/calendar/{YYYY-MM-DD}
  seat map   https://www.atgtickets.com/shows/{show}/{venue}/tickets/{GUID}

  performance link text:
      "Tuesday August 25th 2026 25 Medium availability 19:30 from £29.50"
  seat node:
      <g aria-label="Row A, Seat 13, Sold out"><circle fill="#d8d8d8"/></g>

ATG states availability in words on the seat node, which means we do not have
to infer anything from colour. It also publishes its own demand rating per
performance ("Good availability" / "Medium availability") — a free signal that
ATG computes and we would otherwise have to derive.
"""

import re
from urllib.parse import urlsplit

from .base import Performance, Seat, all_money, first_money, month_starts, norm_ref, parse_uk_datetime

BASE = "https://www.atgtickets.com"

# Words ATG uses on a seat node to mean "you cannot buy this".
UNAVAILABLE_WORDS = ("sold out", "unavailable", "not available", "reserved")

SEAT_LABEL = re.compile(r"Row\s+([A-Za-z0-9]+),\s*Seat\s+([A-Za-z0-9]+)", re.I)


def seat_ref_for(label: str, seat_id: str | None) -> str | None:
    """Build a stable, unique reference for one seat.

    ATG's label is only "Row A, Seat 13" — it does not say which part of the
    house. The Apollo Victoria has a Row A Seat 13 in the stalls AND in the
    dress circle, so row+seat alone collides for roughly 900 of its 2,328
    seats, which would scramble the per-seat history.

    ATG gives every seat its own id in data-seat-id. It was verified stable
    across page loads on 23 Aug 2026, so we append it. Row and seat stay in
    the reference so it is still readable and greppable.
    """
    m = SEAT_LABEL.search(label or "")
    if not m:
        return None
    return norm_ref(m.group(1), m.group(2), (seat_id or "").strip())


class ATGParser:
    operator = "atg"

    # ---- calendar -------------------------------------------------------

    @staticmethod
    def calendar_urls(show: dict, months: int = 6) -> list[str]:
        s, v = show["atg_show_slug"], show["atg_venue_slug"]
        return [f"{BASE}/shows/{s}/{v}/calendar/{d.isoformat()}" for d in month_starts(months)]

    @staticmethod
    def calendar_wait_selector() -> str:
        # The calendar is rendered client-side too. Without this we get the
        # shell, parse zero performances, and record nothing — silently.
        return 'a[href*="/tickets/"]'

    @staticmethod
    def calendar_unavailable(final_url: str) -> str | None:
        """Why this calendar can't be read, or None if it can.

        Seen 23 Sep 2026: a closed show's calendar redirects to its landing
        page (Abigail's Party) or the venue's what's-on page (Arcadia, after
        the Duke of York's became the Tom Stoppard Theatre), and a busy show
        can be held in a Queue-it waiting room (Paddington). None of these
        ever render ticket links, so waiting and retrying only burns time.
        A month past the booking window is different: it redirects to
        another /calendar/ month, which parses fine.
        """
        parts = urlsplit(final_url)
        if (parts.hostname or "").startswith("queue."):
            return "held in a Queue-it waiting room"
        if "/calendar/" not in parts.path:
            return "calendar redirected away (closed or moved?)"
        return None

    @staticmethod
    async def parse_calendar(page, show: dict) -> list[Performance]:
        rows = await page.eval_on_selector_all(
            'a[href*="/tickets/"]',
            """els => els.map(e => ({
                 href: e.getAttribute('href'),
                 text: (e.innerText || e.textContent || '').replace(/\\s+/g, ' ').trim()
               }))""",
        )
        # No ticket links at all means we got the shell, not the calendar.
        if not rows:
            return None

        out, seen = [], set()
        for r in rows:
            href, text = r.get("href") or "", r.get("text") or ""
            guid = href.rstrip("/").rsplit("/", 1)[-1]
            if not guid or guid in seen:
                continue
            starts = parse_uk_datetime(text)
            if not starts:
                continue
            seen.add(guid)
            band = None
            m = re.search(r"(Good|Medium|Low|Limited|Very limited)\s+availability", text, re.I)
            if m:
                band = m.group(0)
            out.append(Performance(
                external_id=guid,
                starts_at=starts,
                url=href if href.startswith("http") else BASE + href,
                min_price=first_money(text),
                availability_band=band,
            ))
        return out

    # ---- seat map -------------------------------------------------------

    @staticmethod
    def seat_wait_selector() -> str:
        # The map is rendered client-side. Waiting on this is what stops us
        # capturing the 3KB empty shell the server returns.
        return "g[aria-label*='Seat'] circle"

    @staticmethod
    async def parse_seats(page) -> list[Seat]:
        nodes = await page.eval_on_selector_all(
            "g[aria-label]",
            """els => els.filter(e => e.querySelector('circle'))
                        .map(e => ({
                          label: e.getAttribute('aria-label'),
                          seatId: e.getAttribute('data-seat-id'),
                          fill: e.querySelector('circle').getAttribute('fill')
                        }))""",
        )
        body = await page.inner_text("body")
        prices = all_money(body)

        seats: list[Seat] = []
        fill_counts: dict[str, int] = {}
        for n in nodes:
            label = n.get("label") or ""
            if not SEAT_LABEL.search(label):
                continue
            fill = (n.get("fill") or "").lower()
            fill_counts[fill] = fill_counts.get(fill, 0) + 1

        # The single most common fill on a seat map is always the unavailable
        # colour, by a wide margin. We use that only as a fallback for houses
        # where ATG omits the words.
        dominant = max(fill_counts, key=fill_counts.get) if fill_counts else None

        for n in nodes:
            label = n.get("label") or ""
            m = SEAT_LABEL.search(label)
            if not m:
                continue
            fill = (n.get("fill") or "").lower()
            lower = label.lower()

            if any(w in lower for w in UNAVAILABLE_WORDS):
                available = False
            elif "£" in label:
                available = True
            else:
                available = fill != dominant

            price = first_money(label)
            if available and price is None and len(prices) == 1:
                price = prices[0]

            seats.append(Seat(
                seat_ref=seat_ref_for(label, n.get("seatId")),
                available=available,
                price=price if available else None,
                raw_label=label,
            ))
        return seats
