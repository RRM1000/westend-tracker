"""Nederlander — Dominion Theatre and Aldwych Theatre.

Same JSON ticketing platform (TixTrack) as Nimax, Delfont, sohoplace and
Menier. See ticketing_api.py for the logic and its two traps.
"""

from .ticketing_api import TicketingApiParser


class NederlanderParser(TicketingApiParser):
    operator = "nederlander"
    BASE = "https://ticketing.nederlander.co.uk"
    SERIES_KEY = "nederlander_series_code"
