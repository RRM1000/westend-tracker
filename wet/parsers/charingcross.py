"""Charing Cross Theatre — on the shared TixTrack platform."""

from .ticketing_api import TicketingApiParser


class CharingCrossParser(TicketingApiParser):
    operator = "charingcross"
    BASE = "https://charingcrosstheatre.tixtrack.com"
    SERIES_KEY = "charingcross_series_code"
