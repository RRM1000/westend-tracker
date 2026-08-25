"""Shared helpers and the parser contract.

Every operator parser implements the same four things:

    calendar_urls(show, months)  -> list[str]
    parse_calendar(page, show)   -> list[Performance]
    seat_wait_selector()         -> str        (what proves the map has rendered)
    parse_seats(page)            -> list[Seat]

Keeping the contract this small is deliberate — adding Nimax or Delfont later
should be a single new file, not a refactor.
"""

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

MONEY = re.compile(r"£\s?(\d{1,4}(?:\.\d{2})?)")


@dataclass
class Performance:
    external_id: str
    starts_at: str                    # ISO local, e.g. "2026-08-25T19:30:00"
    url: str
    min_price: float | None = None
    availability_band: str | None = None
    # Every price still on sale for this performance, cheapest first. None
    # where the operator doesn't publish a band list (ATG's calendar, LW).
    price_bands: list[float] | None = None


@dataclass
class Seat:
    seat_ref: str                     # normalised "SECTION|BLOCK|ROW|NUMBER"
    available: bool
    price: float | None = None
    raw_label: str = ""


def first_money(text: str) -> float | None:
    m = MONEY.search(text or "")
    return float(m.group(1)) if m else None


def all_money(text: str) -> list[float]:
    return sorted({float(x) for x in MONEY.findall(text or "")})


def month_starts(n: int, start: date | None = None) -> list[date]:
    """First day of each of the next n months, including the current one."""
    d = (start or date.today()).replace(day=1)
    out = []
    for _ in range(n):
        out.append(d)
        d = (d + timedelta(days=32)).replace(day=1)
    return out


def norm_ref(*parts) -> str:
    clean = [re.sub(r"\s+", " ", str(p or "").strip().upper()) for p in parts]
    return "|".join(p for p in clean if p)


ORDINAL = re.compile(r"(\d{1,2})(st|nd|rd|th)")


def parse_uk_datetime(text: str, fallback_year: int | None = None) -> str | None:
    """Parse strings like 'Tuesday August 25th 2026 ... 19:30' into ISO.

    Deliberately tolerant — these labels are written for screen readers, not
    for us, and their exact wording changes without warning.
    """
    if not text:
        return None
    t = ORDINAL.sub(r"\1", text)
    tm = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", t)
    dm = re.search(r"\b(\d{1,2})\s+([A-Za-z]{3,9})\s+(\d{4})\b", t) or \
         re.search(r"\b([A-Za-z]{3,9})\s+(\d{1,2})\s+(\d{4})\b", t)
    if not dm:
        return None
    groups = dm.groups()
    try:
        if groups[0].isdigit():
            day, mon, year = int(groups[0]), groups[1], int(groups[2])
        else:
            mon, day, year = groups[0], int(groups[1]), int(groups[2])
        month = datetime.strptime(mon[:3], "%b").month
    except (ValueError, IndexError):
        return None
    hh, mm = (int(tm.group(1)), int(tm.group(2))) if tm else (0, 0)
    return datetime(year, month, day, hh, mm).isoformat(timespec="seconds")
