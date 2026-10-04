"""Text signals extracted from a game, shared by filtering, scoring and matching.

Keeping the extraction in one place means the Scout Score and the partner
matcher agree on what "looks like a mount simulator" means, so a game cannot be
penalised by one and recommended by the other.

Matching is substring based on purpose: a fast, explainable heuristic beats a
model here, and a scout can read exactly which word triggered what.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

#: Content partners refuse: cloning, ripped assets, re-uploads, exploit-driven
#: titles. Kept as whole phrases first, then single tokens.
DEFAULT_RISK_KEYWORDS: tuple[str, ...] = (
    "copy",
    "copied",
    "clone",
    "cloned",
    "ripped",
    "leaked assets",
    "free assets",
    "asset pack",
    "mount",
    "wyrtower",
    "modded",
    "mod",
    "mod menu",
    "mod apk",
    "uncopied",
    "uncapped",
    "leveling fast",
    "dmca",
    "takedown",
    "noclip",
    "esp hack",
    "aimbot",
    "exploit",
    "exploits",
)

#: Phrases that mark a game as already carrying heavy monetisation pressure.
MONETIZATION_KEYWORDS: tuple[str, ...] = (
    "gamepass",
    "game pass",
    "vip",
    "premium",
    "premium members",
    "donate",
    "robux",
    "tycoon",
    "simulator",
    "simulator tycoon",
    "spend money",
    "cash",
    "gems",
    "coins",
    "diamonds",
    "credits",
    "shop",
    "store",
    "pass",
    "booster",
    "speed boost",
    "2x",
    "10x",
    "mega money",
    "in-game purchase",
    "microtransaction",
    "limited time",
    "limited-time",
    "event currency",
)

#: Tokens hinting the game is a generic reskin of an existing genre formula.
ORIGINALITY_KEYWORDS: tuple[str, ...] = (
    "obby",
    "tower",
    "temple",
    "simulator",
    "clicker",
    "grinder",
    "obby",
    "escape",
    "doors",
    "bedwars",
    "survival",
    "classic",
    "old",
    "copy",
)

#: Keywords are matched on a normalised string, so collapse punctuation and
#: collapse whitespace rather than building a regex per keyword.
_NON_WORD = re.compile(r"[^a-z0-9+]+")


def normalise(text: str | None) -> str:
    """Lower-case, strip punctuation and collapse whitespace."""
    if not text:
        return ""
    return _NON_WORD.sub(" ", text.lower()).strip()


@dataclass(slots=True)
class GameSignals:
    """Everything the matcher wants to know about one game, extracted once.

    ``risk_keywords`` and ``monetization_keywords`` are kept verbatim (not
    counts) so a match explanation can quote the offending word back to the
    scout.
    """

    text: str = ""
    risk_keywords: list[str] = field(default_factory=list)
    monetization_keywords: list[str] = field(default_factory=list)
    originality_keywords: list[str] = field(default_factory=list)
    profile_keywords: list[str] = field(default_factory=list)

    @property
    def has_risk(self) -> bool:
        return bool(self.risk_keywords)

    @property
    def monetization_hint_count(self) -> int:
        return len(self.monetization_keywords)

    @property
    def originality_hint_count(self) -> int:
        return len(self.originality_keywords)

    def risk_label(self) -> str | None:
        """Human label for the single most disqualifying signal found."""
        if not self.risk_keywords:
            return None
        return self.risk_label_for(self.risk_keywords[0])

    def risk_label_for(self, keyword: str) -> str:
        """Human label for one specific keyword, falling back to the word itself."""
        return _RISK_LABELS.get(keyword, keyword)

    def to_dict(self) -> dict[str, Any]:
        return {
            "risk_keywords": list(self.risk_keywords),
            "monetization_keywords": list(self.monetization_keywords),
            "originality_keywords": list(self.originality_keywords),
            "profile_keywords": list(self.profile_keywords),
        }


_RISK_LABELS: dict[str, str] = {
    "mount": "mount simulator / mount-only content",
    "wyrtower": "WYRTower re-upload",
    "modded": "modded content",
    "mod": "mod content",
    "mod menu": "mod menu",
    "mod apk": "modded APK",
    "copy": "possible copy / clone",
    "copied": "possible copy / clone",
    "clone": "possible copy / clone",
    "cloned": "possible copy / clone",
    "uncopied": "uncopied assets",
    "uncapped": "uncapped / modded content",
    "dmca": "DMCA-sensitive",
    "ripped": "ripped assets",
    "leaked assets": "leaked assets",
    "free assets": "free assets",
    "asset pack": "asset pack",
    "leveling fast": "fast leveling",
    "exploit": "exploit content",
    "exploits": "exploit content",
    "esp hack": "cheat content",
    "aimbot": "cheat content",
    "noclip": "cheat content",
    "takedown": "DMCA-sensitive",
}


def find_keywords(text: str, keywords: tuple[str, ...] | list[str]) -> list[str]:
    """Return the keywords present in ``text``, longest first (no duplicates).

    Ordering matters: a title like ``Uncopied Mount Simulator`` should report
    ``uncopied`` rather than a substring of itself, so multi-word keywords are
    checked before single tokens and each keyword is only reported once.
    """
    haystack = f" {normalise(text)} "
    found: list[str] = []
    for keyword in sorted(
        {k.strip().lower() for k in keywords if k and k.strip()}, key=len, reverse=True
    ):
        if f" {normalise(keyword)} " in haystack and keyword not in found:
            found.append(keyword)
    return found


def extract_signals(
    name: str | None,
    description: str | None = None,
    *,
    risk_keywords: tuple[str, ...] | list[str] | None = None,
    profile_keywords: tuple[str, ...] | list[str] | None = None,
) -> GameSignals:
    """Extract risk / monetisation / originality signals from a game's text.

    ``profile_keywords`` are the exclusion terms the *active partner profiles*
    declare (``ugc``, ``anime``, ...). They are scanned separately from the
    global risk list so a partner can exclude a genre ScoutForge has no opinion
    about, and so the term is still detectable if it was only configured in the
    profile document.
    """
    text = " ".join(part for part in (name, description) if part)
    risky = list(risk_keywords) if risk_keywords is not None else list(DEFAULT_RISK_KEYWORDS)
    return GameSignals(
        text=text,
        risk_keywords=find_keywords(text, risky),
        monetization_keywords=find_keywords(text, MONETIZATION_KEYWORDS),
        originality_keywords=find_keywords(text, ORIGINALITY_KEYWORDS),
        profile_keywords=find_keywords(text, profile_keywords or ()),
    )


__all__ = [
    "DEFAULT_RISK_KEYWORDS",
    "MONETIZATION_KEYWORDS",
    "ORIGINALITY_KEYWORDS",
    "GameSignals",
    "extract_signals",
    "find_keywords",
    "normalise",
]
