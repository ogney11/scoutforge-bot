"""Configurable partner matching.

A *partner* is a publisher or marketplace ScoutForge can route a game to.
:mod:`scoutforge.partners.profiles` holds the editable deal profiles and
:mod:`scoutforge.partners.matcher` turns a scored game plus its signals into a
ranked, explained list of partners - including why a partner was *not* a fit,
so nothing is silently dropped.

Matching is a ranking aid for humans. ScoutForge never contacts anyone on its
own; a scout reads the reasons and decides.
"""

from scoutforge.partners.keywords import (
    DEFAULT_RISK_KEYWORDS,
    GameSignals,
    extract_signals,
)
from scoutforge.partners.matcher import (
    MatchResult,
    PartnerMatch,
    PartnerMatcher,
    classify_stage,
    match_game,
)
from scoutforge.partners.profiles import (
    DEAL_STAGE_PREFERENCE,
    PartnerProfile,
    ProfileSet,
    default_profiles,
    load_profiles,
)

__all__ = [
    "DEAL_STAGE_PREFERENCE",
    "DEFAULT_RISK_KEYWORDS",
    "GameSignals",
    "MatchResult",
    "PartnerMatch",
    "PartnerMatcher",
    "PartnerProfile",
    "ProfileSet",
    "classify_stage",
    "default_profiles",
    "extract_signals",
    "load_profiles",
    "match_game",
]
