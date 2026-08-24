"""Delfont Mackintosh Theatres.

Prince Edward, Prince of Wales, Sondheim, Novello, Noel Coward, Gielgud,
Wyndham's and Victoria Palace.

Same JSON ticketing platform as Nimax, on their own domain, so the work is
all in ticketing_api.py. Note that Delfont's `lowestPricePoint` values are
even more obviously junk than Nimax's (£0.01 for Hamilton), which is why we
never use that field.
"""

from .ticketing_api import TicketingApiParser


class DelfontParser(TicketingApiParser):
    operator = "dm"
    BASE = "https://buytickets.delfontmackintosh.co.uk"
    SERIES_KEY = "dm_series_code"
