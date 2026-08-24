"""Shaftesbury Theatre — independently owned, on the shared TixTrack platform."""

from .ticketing_api import TicketingApiParser


class ShaftesburyParser(TicketingApiParser):
    operator = "shaftesbury"
    BASE = "https://tixtrack.shaftesburytheatre.com"
    SERIES_KEY = "shaftesbury_series_code"
