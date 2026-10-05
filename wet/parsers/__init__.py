from .atg import ATGParser
from .charingcross import CharingCrossParser
from .comealive import ComeAliveParser
from .delfont import DelfontParser
from .kx import HungerGamesParser, KXParser
from .lw import LWParser
from .marylebone import MaryleboneParser
from .menier import MenierParser
from .nederlander import NederlanderParser
from .nimax import NimaxParser
from .shaftesbury import ShaftesburyParser
from .spektrix import SpektrixParser
from .sohoplace import SohoPlaceParser
from .witness import WitnessParser

REGISTRY = {
    "atg": ATGParser,
    "lw": LWParser,
    "nimax": NimaxParser,
    "dm": DelfontParser,
    "sohoplace": SohoPlaceParser,
    "menier": MenierParser,
    "nederlander": NederlanderParser,
    "shaftesbury": ShaftesburyParser,
    "charingcross": CharingCrossParser,
    "kx": KXParser,
    "hungergames": HungerGamesParser,
    "marylebone": MaryleboneParser,
    "comealive": ComeAliveParser,
    "witness": WitnessParser,
    "spektrix": SpektrixParser,
}


def get(operator: str):
    try:
        return REGISTRY[operator]
    except KeyError:
        raise ValueError(f"no parser for operator {operator!r}; have {list(REGISTRY)}")
