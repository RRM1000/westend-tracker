"""Spektrix — the platform behind much of subsidised and off-West-End London.

Covers Bridge, Criterion, Royal Court, Barbican, Donmar, Park, Southwark
Playhouse and Soho Theatre, among many others. Each venue is a "client" with
its own slug; the API is the same for all of them and needs no key at all:

  events     https://system.spektrix.com/{client}/api/v3/events
  instances  https://system.spektrix.com/{client}/api/v3/instances?startFrom=YYYY-MM-DD
  seat plans https://system.spektrix.com/{client}/api/v3/plans

  prices     https://system.spektrix.com/{client}/api/v3/instances/{id}/price-list

WHICH PRICE COUNTS
------------------
The price-list mixes things a member of the public cannot simply walk up and
buy with things they can. A Donmar performance on 1 Sep 2026 offered:

    Full Price  70.00 / 55.00 / 30.00      Standing        15.00
    Access      20.00 / 20.00 / 15.00      35 and under    20.00

Reporting the raw minimum would advertise £15 Access seats as the cheapest
ticket, which is wrong and unfair to the venue. We therefore drop the ticket
types that are restricted to particular people — access, companion, student,
under-30s, schools, groups — and keep the ones anyone can buy, including
genuinely cheap standing tickets. Merchandise and programmes are dropped too:
one Park Theatre instance offered nothing but "Merchandise £5.00", which is
not a ticket price at all.

Some venues really do sell very cheap "Full Price" tickets — Royal Court has
listed 10p seats alongside £64.50 ones for the same performance, confirmed
by the venue. Do not add a minimum price cutoff to "protect" against this;
it would silently discard a genuine and rather important number. The only
things filtered out are restricted ticket types and non-ticket line items,
by name — never by how low the price is.

UNLIKE THE COMMERCIAL SITES
---------------------------
Spektrix lists every price band whether or not seats remain in it, so this is
"cheapest band on the price list", not "cheapest seat still for sale". The
TixTrack venues report the latter. Do not compare the two as if they were the
same measurement.

ONE MORE THING
--------------
A Spektrix "event" is anything sellable, so a venue's event list is full of
backstage tours, writing workshops, interval drinks pre-orders and test
records. Pin the exact event ids you want in config rather than sweeping
them all up.
"""

import asyncio
import json
import re
from datetime import date, timedelta

from .base import Performance, Seat

# Ticket types not on general sale, or not tickets at all. Kept deliberately
# broad: every venue words its concessions differently, and a concession that
# slips through is published as though anyone could buy it.
RESTRICTED = re.compile(
    r"access|companion|carer|student|school|group|senior|child|concession|"
    r"under\s*\d|\d+\s*(?:and|or)\s*under|over\s*\d|unwaged|equity|bectu|wggb|"
    r"union|standby|staff|industry|press|complimentary|comp|patron|friend|"
    r"member|merchandise|programme|donation|gift|voucher|subscription",
    re.I,
)

# How many upcoming performances per show to price each run, and the pause
# between those extra calls.
PRICE_SAMPLE = 8
PRICE_GAP = 2.0

def cheapest_public_price(payload: dict | None) -> float | None:
    """Cheapest price an ordinary member of the public could pay.

    Returns None when nothing on the list qualifies — which is different from
    "this show is free", so never coerce it to zero.
    """
    if not payload:
        return None
    amounts = []
    for row in payload.get("prices") or []:
        amt = row.get("amount")
        name = ((row.get("ticketType") or {}).get("name") or "")
        if not isinstance(amt, (int, float)) or amt <= 0:
            continue
        if RESTRICTED.search(name):
            continue
        amounts.append(float(amt))
    return min(amounts) if amounts else None


class SpektrixParser:
    operator = "spektrix"
    BASE = "https://system.spektrix.com"

    @classmethod
    def calendar_urls(cls, show: dict, months: int = 6) -> list[str]:
        """One request covers the whole window, rather than one per month."""
        client = show["spektrix_client"]
        start = date.today()
        end = start + timedelta(days=31 * months)
        return [f"{cls.BASE}/{client}/api/v3/instances"
                f"?startFrom={start.isoformat()}&startTo={end.isoformat()}"]

    @staticmethod
    def calendar_wait_selector():
        return None

    @staticmethod
    async def parse_calendar(page, show: dict) -> list[Performance] | None:
        try:
            data = json.loads(await page.evaluate("() => document.body.innerText"))
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(data, list):
            return None

        want = show["spektrix_event_id"]
        out: list[Performance] = []
        for i in data:
            if (i.get("event") or {}).get("id") != want:
                continue
            if i.get("cancelled"):
                continue
            starts = i.get("start")
            iid = i.get("id")
            if not starts or not iid:
                continue
            out.append(Performance(
                external_id=str(iid),
                starts_at=starts,
                url=f"https://system.spektrix.com/{show['spektrix_client']}"
                    f"/api/v3/instances/{iid}",
                min_price=None,          # filled in below for the nearest few
                availability_band="On sale" if i.get("isOnSale") else "Not on sale",
            ))
        out.sort(key=lambda p: p.starts_at)

        client = show["spektrix_client"]
        for n, perf in enumerate(out[:PRICE_SAMPLE]):
            if n:
                await asyncio.sleep(PRICE_GAP)
            try:
                txt = await page.evaluate(
                    """async u => {
                         const r = await fetch(u, {headers: {Accept: 'application/json'}});
                         return r.ok ? await r.text() : null;
                       }""",
                    f"https://system.spektrix.com/{client}/api/v3"
                    f"/instances/{perf.external_id}/price-list")
                perf.min_price = cheapest_public_price(json.loads(txt) if txt else None)
            except Exception:  # noqa: BLE001
                perf.min_price = None

        return out

    @staticmethod
    def seat_wait_selector():
        return None

    @staticmethod
    async def parse_seats(page) -> list[Seat]:
        # The plans endpoint describes the house but not what is still for
        # sale, so there is no honest seat snapshot to take here yet.
        return []
