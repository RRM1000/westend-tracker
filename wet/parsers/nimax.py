"""Nimax Theatres — Apollo, Lyric, Garrick, Vaudeville, Duchess, Palace.

Nimax runs the shared JSON ticketing platform, so everything of substance
lives in ticketing_api.py — including the two traps in that data. Read the
docstring there before changing anything here.
"""

from .ticketing_api import TicketingApiParser, cheapest_from_inventory  # noqa: F401


class NimaxParser(TicketingApiParser):
    operator = "nimax"
    BASE = "https://ticketing.nimaxtheatres.com"
    SERIES_KEY = "nimax_series_code"
