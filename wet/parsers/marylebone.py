"""Marylebone Theatre — on the shared TixTrack platform.

Price levels carry the face value in their name ("Band E £27.75") and add a
£2.25 booking fee on top, so the default `price` field matches the headline.
"""

from .ticketing_api import TicketingApiParser


class MaryleboneParser(TicketingApiParser):
    operator = "marylebone"
    BASE = "https://tickets.marylebonetheatre.com"
    SERIES_KEY = "marylebone_series_code"
