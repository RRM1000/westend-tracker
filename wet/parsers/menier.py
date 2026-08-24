"""Menier Chocolate Factory.

Off-West-End, and on the same JSON ticketing platform as the big commercial
groups — so it costs us nothing beyond a base URL.
"""

from .ticketing_api import TicketingApiParser


class MenierParser(TicketingApiParser):
    operator = "menier"
    BASE = "https://book.menierchocolatefactory.com"
    SERIES_KEY = "menier_series_code"
