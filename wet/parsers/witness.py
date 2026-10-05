"""Witness for the Prosecution (London County Hall) — its own TixTrack domain.

No booking fee is added on top: `price` and `displayRetailPrice` agree.
"""

from .ticketing_api import TicketingApiParser


class WitnessParser(TicketingApiParser):
    operator = "witness"
    BASE = "https://ticketing.witnesscountyhall.com"
    SERIES_KEY = "witness_series_code"
