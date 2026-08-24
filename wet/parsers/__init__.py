from .atg import ATGParser
from .charingcross import CharingCrossParser
from .delfont import DelfontParser
from .lw import LWParser
from .menier import MenierParser
from .nederlander import NederlanderParser
from .nimax import NimaxParser
from .shaftesbury import ShaftesburyParser
from .spektrix import SpektrixParser
from .sohoplace import SohoPlaceParser

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
    "spektrix": SpektrixParser,
}


def get(operator: str):
    try:
        return REGISTRY[operator]
    except KeyError:
        raise ValueError(f"no parser for operator {operator!r}; have {list(REGISTRY)}")
