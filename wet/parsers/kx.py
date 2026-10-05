"""The Troubadour theatres: Wembley Park (High School Musical) and Canary Wharf
(The Hunger Games On Stage).

Both sell through KX Tickets, which is the same TixTrack/Nliven JSON platform
as Nimax and Delfont Mackintosh (see ticketing_api.py for the logic and its
two traps). Wembley's shows sit on KX's own domain; The Hunger Games has its
own white-label domain on the same software and the same API paths.

Two differences from the other operators, both found by comparing the API with
what the seat map shows a customer:

  * The seat map headlines the fee-inclusive figure. A Band A seat with
    `price` 72.50 plus £2.50 of facility fee and Troubadour Trust levy is
    sold as £75 (`displayRetailPrice`); the price filter reads
    £25 / £35 / £50 / £75 / £125 / £212.50. So PRICE_FIELD is
    displayRetailPrice here, unlike Nimax and Delfont where the headline
    is the face value.

  * The Hunger Games lists VIP "Premium VIP" levels with
    requirePackagePurchase (a seat bundled with extras, £212.50 on the map
    under "Package"). They cannot be bought as a seat on their own, so they
    are left out of the bands.
"""

from .ticketing_api import TicketingApiParser


class KXParser(TicketingApiParser):
    operator = "kx"
    BASE = "https://ticketing.kxtickets.com"
    SERIES_KEY = "kx_series_code"
    PRICE_FIELD = "displayRetailPrice"
    EXCLUDE_PACKAGES = True


class HungerGamesParser(TicketingApiParser):
    operator = "hungergames"
    BASE = "https://tickets.thehungergamesonstage.com"
    SERIES_KEY = "hungergames_series_code"
    PRICE_FIELD = "displayRetailPrice"
    EXCLUDE_PACKAGES = True
