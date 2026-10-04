"""The editable partner profiles ScoutForge matches games against.

Each profile is a plain dataclass so an operator can rewrite the whole set as
JSON (file or database) without touching Python. The six defaults encode the
deal terms currently on the table:

=============  ==========================  ==========================  =================
Partner        Revenue share               Ceiling                     Stage preference
=============  ==========================  ==========================  =================
INDG           1-4%+                        none                        any
InCreates      6%                           none                        growing / stable
Partner #3     ~5%+                         ~$500K                      any
Xolbor         2-5%+                        none                        early
CreatorExch.   5% tiered                    none                        any (>=15%)
Locked.dev     5%                           ~$30K                       early preferred
=============  ==========================  ==========================  =================

Everything is a *hint* for a human decision: ScoutForge ranks and explains, it
never contacts anybody.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from scoutforge.partners.keywords import DEFAULT_RISK_KEYWORDS

#: Stage buckets a game's history is sorted into, from newest to oldest.
DEAL_STAGE_PREFERENCE: dict[str, str] = {
    "early": "early / pre-algorithm (small audience, algorithm has not locked in)",
    "growing": "growing (audience still climbing)",
    "established": "established and stable (audience already proven)",
    "any": "no stage preference",
}

_STAGES = set(DEAL_STAGE_PREFERENCE)


@dataclass(slots=True)
class PartnerProfile:
    """One partner's deal criteria, in ScoutForge's own vocabulary.

    ``exclude_risk`` terms hard-exclude a game from this partner when they show
    up in the title/description (mounts, modded content, DMCA-sensitive copies
    and so on). ``exclude_stages`` drops whole maturity stages instead.
    """

    key: str
    name: str
    revenue_share_min_pct: float = 0.0
    revenue_share_max_pct: float | None = None
    revenue_share_note: str = ""
    max_game_value_usd: float | None = None
    min_game_value_usd: float | None = None
    preferred_stages: tuple[str, ...] = ("any",)
    preferred_ccu_min: int | None = None
    preferred_ccu_max: int | None = None
    preferred_visits_max: int | None = None
    min_engagement_pct: float | None = None
    exclude_risk: tuple[str, ...] = ()
    exclude_stages: tuple[str, ...] = ()
    notes: str = ""
    enabled: bool = True
    extra_exclusions: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["preferred_stages"] = list(self.preferred_stages)
        data["exclude_risk"] = list(self.exclude_risk)
        data["exclude_stages"] = list(self.exclude_stages)
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PartnerProfile:
        """Build a profile from JSON, ignoring unknown keys.

        Unknown keys are dropped rather than raising so a profile file written
        for a newer version still loads on an older deployment.
        """
        known = {f.name for f in fields(cls)}
        kwargs = {key: value for key, value in data.items() if key in known}
        for tuple_field in ("preferred_stages", "exclude_risk", "exclude_stages"):
            if tuple_field in kwargs and kwargs[tuple_field] is not None:
                kwargs[tuple_field] = tuple(
                    str(part).strip().lower() for part in kwargs[tuple_field] if str(part).strip()
                )
        bad_stages = set(kwargs.get("preferred_stages", ())) | set(kwargs.get("exclude_stages", ()))
        unknown = bad_stages - _STAGES
        if unknown:
            raise ValueError(
                f"profile {kwargs.get('key', '<unknown>')!r}: unknown stage(s) {sorted(unknown)}; "
                f"expected {sorted(_STAGES)}"
            )
        return cls(**kwargs)


@dataclass(slots=True)
class ProfileSet:
    """An ordered collection of profiles plus the keywords they share."""

    profiles: tuple[PartnerProfile, ...]
    risk_keywords: tuple[str, ...] = DEFAULT_RISK_KEYWORDS

    def enabled(self) -> tuple[PartnerProfile, ...]:
        return tuple(profile for profile in self.profiles if profile.enabled)

    def get(self, key: str) -> PartnerProfile | None:
        for profile in self.profiles:
            if profile.key == key:
                return profile
        return None

    def to_list(self) -> list[dict[str, Any]]:
        return [profile.to_dict() for profile in self.profiles]


def default_profiles() -> ProfileSet:
    """The built-in profile set, matching the deals currently on the table."""
    shared_risk = ("mount", "modded", "dmca", "ugc", "wyrtower", "copy")
    return ProfileSet(
        profiles=(
            PartnerProfile(
                key="indg",
                name="INDG",
                revenue_share_min_pct=1.0,
                revenue_share_note="1-4%+ depending on the game",
                preferred_stages=("any",),
                exclude_risk=shared_risk,
                extra_exclusions={"ugc": "UGC (user generated content) titles are out of scope"},
                notes="Broad catalogue buyer. No size ceiling, no hard criteria beyond "
                "excluded content types.",
            ),
            PartnerProfile(
                key="increates",
                name="InCreates",
                revenue_share_min_pct=6.0,
                revenue_share_note="flat 6%",
                preferred_stages=("growing", "established"),
                exclude_risk=("mount", "modded", "dmca"),
                notes="Highest share of the set, and deliberately open about criteria: "
                "growing or established-and-stable games both qualify.",
            ),
            PartnerProfile(
                key="partner_3",
                name="Partner #3",
                revenue_share_min_pct=5.0,
                revenue_share_note="~5%+",
                max_game_value_usd=500_000.0,
                preferred_stages=("any",),
                exclude_risk=(*shared_risk, "anime"),
                extra_exclusions={"anime": "anime-styled titles are out of scope"},
                notes="Pays ~5%+ but only up to about $500K of value per game.",
            ),
            PartnerProfile(
                key="xolbor",
                name="Xolbor",
                revenue_share_min_pct=2.0,
                revenue_share_note="2-5%+",
                preferred_stages=("early",),
                preferred_ccu_min=100,
                preferred_visits_max=1_000_000,
                exclude_risk=(*shared_risk, "anime"),
                extra_exclusions={"tower": "tower / temple obby clones"},
                notes="Early-stage buyer. Mainly 100+ CCU games with roughly 1M visits or "
                "fewer; mount, modded, DMCA, UGC, tower and anime titles are declined.",
            ),
            PartnerProfile(
                key="creator_exchange",
                name="CreatorExchange",
                revenue_share_min_pct=5.0,
                revenue_share_note="5% tiered by game performance",
                min_engagement_pct=15.0,
                preferred_stages=("any",),
                exclude_risk=("mount", "modded", "dmca"),
                notes="Tiers the 5% by engagement; games below 15% engagement are out.",
            ),
            PartnerProfile(
                key="locked_dev",
                name="Locked.dev",
                revenue_share_min_pct=5.0,
                revenue_share_note="flat 5%",
                max_game_value_usd=30_000.0,
                min_engagement_pct=20.0,
                preferred_stages=("early",),
                preferred_ccu_min=100,
                preferred_visits_max=1_000_000,
                exclude_risk=("mount", "modded", "dmca"),
                notes="Small cheque (~$30K cap) so it prefers pre-algorithm games; "
                "established titles are still fine when CCU and visits are excellent.",
            ),
        ),
        risk_keywords=DEFAULT_RISK_KEYWORDS,
    )


def load_profiles(
    raw: str | Mapping[str, Any] | Iterable[Mapping[str, Any]] | None = None,
    *,
    risk_keywords: Iterable[str] | None = None,
) -> ProfileSet:
    """Build a :class:`ProfileSet` from JSON text, a mapping or nothing.

    ``None`` returns the defaults. A mapping may be either
    ``{"profiles": [...], "risk_keywords": [...]}`` or a bare list of profiles.
    """
    if raw is None or raw == "":
        return default_profiles()

    if isinstance(raw, str):
        path = Path(raw)
        if path.is_file():
            document: Any = json.loads(path.read_text(encoding="utf-8"))
        else:
            document = json.loads(raw)
    else:
        document = raw

    if isinstance(document, Mapping):
        entries = document.get("profiles", [])
        keywords = document.get("risk_keywords")
    else:
        entries = document
        keywords = None

    profiles = tuple(PartnerProfile.from_dict(entry) for entry in entries)
    resolved_keywords = (
        tuple(str(k).strip().lower() for k in keywords if str(k).strip())
        if keywords
        else (tuple(str(k).strip().lower() for k in risk_keywords) if risk_keywords else None)
    )
    return ProfileSet(
        profiles=profiles,
        risk_keywords=resolved_keywords or default_profiles().risk_keywords,
    )


__all__ = [
    "DEAL_STAGE_PREFERENCE",
    "PartnerProfile",
    "ProfileSet",
    "default_profiles",
    "load_profiles",
]
