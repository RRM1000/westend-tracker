"""@sohoplace, Soho Place.

Same JSON ticketing platform as Nimax and Delfont Mackintosh, on its own
domain. All the logic — and the two traps in that data — live in
ticketing_api.py.
"""

from .ticketing_api import TicketingApiParser


class SohoPlaceParser(TicketingApiParser):
    operator = "sohoplace"
    BASE = "https://ticketing.sohoplace.org"
    SERIES_KEY = "sohoplace_series_code"
