"""Email-alert parser registry.

Each parser knows how to recognise alert emails from one Swiss apartment
portal and extract listing URLs + a context window for the apartment_pipeline
to normalize and score.
"""

from __future__ import annotations

from .base import BaseEmailParser, ParseError, ParsedListing
from .flatfox import FlatfoxParser
from .homegate import HomegateParser
from .immoscout24 import ImmoScout24Parser
from .newhome import NewhomeParser
from .wgzimmer import WGZimmerParser

REGISTERED_PARSERS: tuple[BaseEmailParser, ...] = (
    WGZimmerParser(),
    HomegateParser(),
    NewhomeParser(),
    ImmoScout24Parser(),
    FlatfoxParser(),
)


def parser_for_sender(from_header: str) -> BaseEmailParser | None:
    for parser in REGISTERED_PARSERS:
        if parser.matches_sender(from_header):
            return parser
    return None


__all__ = [
    "BaseEmailParser",
    "ParseError",
    "ParsedListing",
    "REGISTERED_PARSERS",
    "parser_for_sender",
]
