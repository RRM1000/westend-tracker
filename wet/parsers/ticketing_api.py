"""Shared parser for the JSON ticketing platform used by several West End groups.

Nimax and Delfont Mackintosh run the same booking software on their own
domains, so one implementation covers both — and probably others we meet later
(sohoplace uses it too). A new group is a four-line subclass.

Endpoints, read live on 23 August 2026:

  month listing   /api/consumer/events/v2/getbymonth/{SERIES}?requestedTime=YYYY/MM/DD&salesChannel=Web
                  -> {"events":[{id, localDate, availabilityColor, lowestPricePoint,
                                 selectSeatsUrl, ...}]}

  one performance /api/consumer/eventinventory/{eventId}?
                  -> {"priceMaps":[{price, priceLevelId, displayName, ...}],
                      "mapSeats":[{key, priceKey, isReserved, ...}]}

TWO THINGS THAT WILL MISLEAD YOU
--------------------------------
1. `lowestPricePoint` in the month listing looks exactly like the "from" price
   you want. It is not. It is a fixed per-show number sitting below anything
   buyable, and on Delfont Mackintosh it is frankly garbage:

       SIX        feed 13.50   real 34.50        Hamilton   feed  0.01   real 500.00
       Hadestown  feed 18.50   real 25.00        Oliver!    feed  0.01   real  30.00
       Producers  feed 13.50   real 25.00        Les Mis    feed  8.10   real  42.50

   Publishing it would put prices on the site that no customer can pay.

2. `priceMaps` lists only the bands that STILL HAVE SEATS. That is a feature,
   not a bug — it means the cheapest entry is the cheapest seat you could
   actually buy at that moment. It also means the number rises as a house
   fills: Hamilton on 1 Sep 2026 was down to 12 seats, all in the £500 band,
   so £500 genuinely was the cheapest available. Do not "correct" this to the
   show's headline price; the whole point is to watch it move.
"""

import asyncio
import json

from .base import Performance, Seat, month_starts, norm_ref

# How many upcoming performances per show to price properly each month. Each
# one is an extra API call, so this is the dial between cost and coverage.
# The near dates are where prices actually move.
PRICE_SAMPLE = 8

# Seconds between those extra calls. Small JSON requests of the sort the site's
# own pages make constantly, but we still don't machine-gun them.
PRICE_GAP = 2.5


def cheapest_from_inventory(inv: dict | None) -> float | None:
    """Cheapest price a customer could actually choose for this performance.

    Returns None when we could not read the inventory — which is different
    from "the show is expensive", so never coerce it to zero.
    """
    if not inv:
        return None
    prices = [p.get("price") for p in (inv.get("priceMaps") or [])
              if isinstance(p.get("price"), (int, float))]
    return float(min(prices)) if prices else None


async def _fetch_json(page, path: str):
    """Fetch from inside the page, so it is same-origin and uses the same
    session the site itself does. Returns None rather than raising — one
    performance we could not price is not a reason to lose the whole month."""
    try:
        txt = await page.evaluate(
            """async p => {
                 const r = await fetch(p, {headers: {Accept: 'application/json'}});
                 if (!r.ok) return null;
                 return await r.text();
               }""", path)
        return json.loads(txt) if txt else None
    except Exception:  # noqa: BLE001
        return None


class TicketingApiParser:
    """Subclass and set BASE, SERIES_KEY and operator."""

    BASE = ""
    SERIES_KEY = ""
    operator = ""

    # ---- calendar -------------------------------------------------------

    @classmethod
    def calendar_urls(cls, show: dict, months: int = 6) -> list[str]:
        series = show[cls.SERIES_KEY]
        return [
            f"{cls.BASE}/api/consumer/events/v2/getbymonth/{series}"
            f"?requestedTime={d.year}%2F{d.month:02d}%2F{d.day:02d}&salesChannel=Web"
            for d in month_starts(months)
        ]

    @staticmethod
    def calendar_wait_selector():
        # The response is raw JSON, not a rendered page, so there is nothing
        # to wait for.
        return None

    @classmethod
    async def parse_calendar(cls, page, show: dict) -> list[Performance] | None:
        """None means we could not read the response at all; an empty list
        means we read it fine and the show simply isn't on that month. The
        difference matters — the first is a fault, the second is normal for a
        show that has not opened yet or has closed."""
        try:
            data = json.loads(await page.evaluate("() => document.body.innerText"))
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(data, dict) or "events" not in data:
            return None

        out: list[Performance] = []
        for e in data.get("events") or []:
            starts, eid = e.get("localDate"), e.get("id")
            if not starts or eid is None:
                continue
            url = e.get("selectSeatsUrl") or ""
            out.append(Performance(
                external_id=str(eid),
                starts_at=starts,
                url=(cls.BASE + url) if url.startswith("/") else (url or cls.BASE),
                min_price=None,      # NOT lowestPricePoint — see module docstring
                availability_band=e.get("availabilityColor"),
            ))

        out.sort(key=lambda p: p.starts_at)

        for i, perf in enumerate(out[:PRICE_SAMPLE]):
            if i:
                await asyncio.sleep(PRICE_GAP)
            inv = await _fetch_json(
                page, f"{cls.BASE}/api/consumer/eventinventory/{perf.external_id}?")
            perf.min_price = cheapest_from_inventory(inv)

        return out

    # ---- seat map -------------------------------------------------------

    @staticmethod
    def seat_wait_selector():
        return None

    @staticmethod
    async def parse_seats(page) -> list[Seat]:
        """Seats come as JSON, so unlike ATG there is no colour or wording to
        interpret.

        Caveat: `mapSeats` lists the seats you can currently choose, so we can
        count what is available but not the size of the house. A snapshot here
        means "seats available", not "seats sold".
        """
        try:
            inv = json.loads(await page.evaluate("() => document.body.innerText"))
        except Exception:  # noqa: BLE001
            return []

        price_by_level = {}
        for pm in inv.get("priceMaps") or []:
            lvl, price = pm.get("priceLevelId"), pm.get("price")
            if lvl is not None and isinstance(price, (int, float)):
                price_by_level[str(lvl)] = float(price)

        seats: list[Seat] = []
        for m in inv.get("mapSeats") or []:
            key = m.get("key")
            if not key:
                continue
            lvl = (m.get("priceKey") or "").split("-")[0]   # "68533--" -> "68533"
            seats.append(Seat(
                seat_ref=norm_ref(*str(key).split("-")),
                available=not m.get("isReserved", False),
                price=price_by_level.get(lvl),
                raw_label=str(key),
            ))
        return seats
