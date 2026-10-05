"""Come Alive! The Greatest Showman Circus Spectacular — its own TixTrack domain.

The levels are named by what the customer pays ("£50", "£70", "VIP - £200")
and `price` is that figure less a booking fee (48.08 for "£50"), so the
headline is `displayRetailPrice`.
"""

from .ticketing_api import TicketingApiParser


class ComeAliveParser(TicketingApiParser):
    operator = "comealive"
    BASE = "https://tickets.comealiveshow.com"
    SERIES_KEY = "comealive_series_code"
    PRICE_FIELD = "displayRetailPrice"
