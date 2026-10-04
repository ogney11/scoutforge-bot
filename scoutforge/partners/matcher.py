"""Score a game against every partner profile and explain the result.

The matcher answers two questions for a scout:

1. *Who should this go to first?* - a 0-100 compatibility score plus the reasons
   that produced it.
2. *Why did nobody else get it?* - every profile that was skipped records the
   single decisive mismatch, so a partner is never dropped off the radar by an
   opaque filter.

Nothing here contacts anybody; it is a ranking aid.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from scoutforge.partners.keywords import GameSignals, extract_signals, normalise
from scoutforge.partners.profiles import PartnerProfile, ProfileSet, default_profiles

#: Weights for the compatibility score. They are points out of 100 so the
#: partner score reads like the Scout Score and the two can be shown together.
_WEIGHTS = {
    "score": 25.0,
    "ratio": 20.0,
    "engagement": 15.0,
    "monetization": 12.0,
    "size": 12.0,
    "stage": 10.0,
    "originality": 6.0,
}

#: Below this the match is recorded but flagged as weak rather than hidden.
_WEAK_SCORE = 35.0

#: Stage a game can still plausibly grow into, used to soften a stage mismatch.
_ADJACENT_STAGES: dict[str, frozenset[str]] = {
    "early": frozenset({"growing"}),
    "growing": frozenset({"early", "established"}),
    "established": frozenset({"growing"}),
}


@dataclass(slots=True)
class PartnerMatch:
    """One partner's verdict on one game."""

    partner_key: str
    partner_name: str
    score: float
    eligible: bool
    reasons: list[str] = field(default_factory=list)
    mismatches: list[str] = field(default_factory=list)
    revenue_share: str = ""
    deal_terms: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "partner_key": self.partner_key,
            "partner_name": self.partner_name,
            "score": round(self.score, 1),
            "eligible": self.eligible,
            "reasons": list(self.reasons),
            "mismatches": list(self.mismatches),
            "revenue_share": self.revenue_share,
            "deal_terms": dict(self.deal_terms),
        }


@dataclass(slots=True)
class MatchResult:
    """Ranking plus the full field for one game."""

    universe_id: int
    matches: list[PartnerMatch] = field(default_factory=list)
    considered: int = 0
    signals: dict[str, Any] = field(default_factory=dict)

    @property
    def eligible(self) -> list[PartnerMatch]:
        return [m for m in self.matches if m.eligible]

    def best(self) -> PartnerMatch | None:
        return self.eligible[0] if self.eligible else None

    def keys(self) -> list[str]:
        return [m.partner_key for m in self.eligible]

    def to_dict(self) -> dict[str, Any]:
        return {
            "universe_id": self.universe_id,
            "considered": self.considered,
            "matches": [m.to_dict() for m in self.matches],
            "signals": dict(self.signals),
        }


def classify_stage(*, ccu: int | None, visits: int | None, age_days: float | None) -> str:
    """Bucket a game's maturity from live CCU and lifetime visits.

    Age is only a tie-breaker: Roblox games routinely sit at low CCU for years,
    so a small-but-still-thriving title is "growing", not "early".
    """
    ccu_value = ccu or 0
    visits_value = visits or 0
    if ccu_value >= 3_000 or visits_value >= 20_000_000:
        return "established"
    if ccu_value >= 300 or visits_value >= 2_000_000:
        return "growing"
    if ccu_value >= 50 or visits_value >= 200_000:
        return "early" if (age_days or 0) < 540 else "growing"
    return "early"


class PartnerMatcher:
    """Applies a :class:`~scoutforge.partners.profiles.ProfileSet` to a game."""

    def __init__(
        self,
        profiles: ProfileSet | None = None,
        *,
        min_score: float = _WEAK_SCORE,
        max_matches: int = 6,
    ) -> None:
        self.profiles = profiles or default_profiles()
        self.min_score = min_score
        self.max_matches = max(1, max_matches)

    # ------------------------------------------------------------------ api
    def match(
        self,
        *,
        universe_id: int,
        name: str | None,
        description: str | None = None,
        ccu: int | None = None,
        visits: int | None = None,
        score: float | None = None,
        ratio: float | None = None,
        engagement_pct: float | None = None,
        monetization_score: float | None = None,
        originality_score: float | None = None,
        game_value_usd: float | None = None,
        age_days: float | None = None,
        signals: GameSignals | None = None,
    ) -> MatchResult:
        """Rank every enabled profile for one game."""
        enabled = self.profiles.enabled()
        signals = signals or extract_signals(
            name,
            description,
            risk_keywords=self.profiles.risk_keywords,
            profile_keywords=_exclusion_terms(enabled),
        )
        stage = classify_stage(ccu=ccu, visits=visits, age_days=age_days)
        context = _Context(
            name=name or "the game",
            ccu=ccu,
            visits=visits,
            score=score,
            ratio=ratio,
            engagement_pct=engagement_pct,
            monetization_score=monetization_score,
            originality_score=originality_score,
            game_value_usd=game_value_usd,
            stage=stage,
            signals=signals,
        )

        matches = [self._evaluate(profile, context) for profile in enabled]
        matches.sort(key=lambda m: (m.eligible, m.score), reverse=True)
        ranked = matches[: self.max_matches]
        return MatchResult(
            universe_id=universe_id,
            matches=ranked,
            considered=len(matches),
            signals=signals.to_dict(),
        )

    # ------------------------------------------------------------- internals
    def _evaluate(self, profile: PartnerProfile, ctx: _Context) -> PartnerMatch:
        reasons: list[str] = []
        mismatches: list[str] = []
        points = 0.0

        exclusions = self._exclusions(profile, ctx.signals)
        for label in exclusions:
            mismatches.append(f"excluded: {label}")

        stage_mismatch = self._stage_mismatch(profile, ctx)
        if stage_mismatch:
            mismatches.append(stage_mismatch)

        hard = self._hard_mismatches(profile, ctx)
        mismatches.extend(hard)

        if exclusions or stage_mismatch or hard:
            # Still report the score so the dashboard can show *how* close it was,
            # but never call the game eligible for this partner.
            points = self._score(profile, ctx, reasons)
            return PartnerMatch(
                partner_key=profile.key,
                partner_name=profile.name,
                score=round(points, 1),
                eligible=False,
                reasons=reasons,
                mismatches=_dedupe(mismatches),
                revenue_share=profile.revenue_share_note or _share_text(profile),
                deal_terms=_deal_terms(profile),
            )

        points = self._score(profile, ctx, reasons)
        reasons.append(f"no exclusions found ({_exclusion_summary(profile)})")
        return PartnerMatch(
            partner_key=profile.key,
            partner_name=profile.name,
            score=round(points, 1),
            eligible=points >= self.min_score,
            reasons=_dedupe(reasons),
            mismatches=[],
            revenue_share=profile.revenue_share_note or _share_text(profile),
            deal_terms=_deal_terms(profile),
        )

    def _exclusions(self, profile: PartnerProfile, signals: GameSignals) -> list[str]:
        haystack = signals.risk_keywords + signals.originality_keywords + signals.profile_keywords
        labels: list[str] = []
        covered: set[str] = set()

        for term in profile.exclude_risk:
            hit = _matching_keyword(term, haystack)
            if not hit:
                continue
            covered.add(normalise(term))
            # A profile that spells the rule out (``anime`` -> "anime-styled titles
            # are out of scope") is more useful than the generic vocabulary label.
            label = profile.extra_exclusions.get(term) or signals.risk_label_for(hit)
            labels.append(f"{hit} content ({label})")

        for term, label in profile.extra_exclusions.items():
            if normalise(term) in covered:
                continue
            if _matching_keyword(term, haystack):
                labels.append(label)
        return _dedupe(labels)

    def _stage_mismatch(self, profile: PartnerProfile, ctx: _Context) -> str | None:
        if "any" in profile.preferred_stages or ctx.stage in profile.preferred_stages:
            return None
        wanted = ", ".join(profile.preferred_stages)
        return f"stage mismatch: game is {ctx.stage}, {profile.name} prefers {wanted}"

    def _hard_mismatches(self, profile: PartnerProfile, ctx: _Context) -> list[str]:
        out: list[str] = []
        ceiling = profile.max_game_value_usd
        if ceiling is not None and ctx.game_value_usd is not None and ctx.game_value_usd > ceiling:
            out.append(
                f"estimated value ${_usd(ctx.game_value_usd)} is above the ${_usd(ceiling)} ceiling"
            )
        floor = profile.min_game_value_usd
        if floor is not None and ctx.game_value_usd is not None and ctx.game_value_usd < floor:
            out.append(
                f"estimated value ${_usd(ctx.game_value_usd)} is below the ${_usd(floor)} floor"
            )
        if profile.min_engagement_pct is not None:
            if ctx.engagement_pct is None:
                out.append(f"needs engagement >= {profile.min_engagement_pct:g}%, which is unknown")
            elif ctx.engagement_pct < profile.min_engagement_pct:
                out.append(
                    f"engagement {ctx.engagement_pct:.1f}% is below the "
                    f"{profile.min_engagement_pct:g}% requirement"
                )
        return out

    def _score(self, profile: PartnerProfile, ctx: _Context, reasons: list[str]) -> float:
        total = 0.0

        value = _band(ctx.score, 0, 100)
        total += value * _WEIGHTS["score"]
        reasons.append(f"Scout Score {_fmt(ctx.score)} / 100 ({_band_label(value)})")

        ratio = ctx.ratio or 0.0
        ratio_pct = ratio * 100
        ratio_value = _band(ratio_pct, 0, 5) / 100
        total += ratio_value * _WEIGHTS["ratio"]
        reasons.append(f"CCU/visits ratio {_fmt_pct(ratio)} ({_band_label(ratio_value * 100)})")

        engagement = ctx.engagement_pct or 0.0
        engagement_value = _band(engagement, 0, 40) / 100
        total += engagement_value * _WEIGHTS["engagement"]
        reasons.append(f"engagement {_fmt_pct(engagement)} ({_band_label(engagement_value * 100)})")

        monetization = ctx.monetization_score or 0.0
        monetization_value = _band(monetization, 0, 100) / 100
        total += monetization_value * _WEIGHTS["monetization"]
        reasons.append(
            f"monetisation signal {monetization:.0f}/100"
            f" ({len(ctx.signals.monetization_keywords)} keyword hits)"
        )

        size_value, size_reason = self._size_value(profile, ctx)
        total += size_value * _WEIGHTS["size"]
        reasons.append(size_reason)

        stage_value, stage_reason = self._stage_value(profile, ctx)
        total += stage_value * _WEIGHTS["stage"]
        reasons.append(stage_reason)

        originality = ctx.originality_score or 0.0
        originality_value = _band(originality, 0, 100) / 100
        total += originality_value * _WEIGHTS["originality"]
        if ctx.signals.originality_keywords:
            reasons.append("genre-clone hints: " + ", ".join(ctx.signals.originality_keywords[:3]))

        return max(0.0, min(100.0, total))

    def _size_value(self, profile: PartnerProfile, ctx: _Context) -> tuple[float, str]:
        if ctx.ccu is None:
            return 0.5, "CCU unknown, assumed average"
        if profile.preferred_ccu_min is not None and ctx.ccu < profile.preferred_ccu_min:
            return (
                0.2,
                f"{ctx.ccu:,} CCU is below the {profile.preferred_ccu_min:,} CCU floor",
            )
        if profile.preferred_ccu_max is not None and ctx.ccu > profile.preferred_ccu_max:
            return (
                0.4,
                f"{ctx.ccu:,} CCU is above the {profile.preferred_ccu_max:,} CCU ceiling",
            )
        if (
            profile.preferred_visits_max is not None
            and (ctx.visits or 0) > profile.preferred_visits_max
        ):
            return (
                0.3,
                f"{(ctx.visits or 0):,} lifetime visits is above the "
                f"{profile.preferred_visits_max:,} preference",
            )
        if profile.preferred_ccu_min is not None or profile.preferred_visits_max is not None:
            return (
                1.0,
                f"{ctx.ccu:,} CCU / {(ctx.visits or 0):,} visits sits inside "
                f"{profile.name}'s preferred size",
            )
        return 1.0, f"{ctx.ccu:,} CCU, no size ceiling"

    def _stage_value(self, profile: PartnerProfile, ctx: _Context) -> tuple[float, str]:
        if "any" in profile.preferred_stages:
            return 1.0, f"stage {ctx.stage} accepted ({profile.name} has no stage preference)"
        if ctx.stage in profile.preferred_stages:
            return 1.0, f"stage {ctx.stage} matches {profile.name}'s preference"
        # Adjacent stage: still viable, just not first choice.
        adjacent = _ADJACENT_STAGES.get(ctx.stage, frozenset())
        if adjacent & set(profile.preferred_stages):
            return 0.5, f"stage {ctx.stage} is one step from {profile.name}'s preference"
        return 0.15, f"stage {ctx.stage} is a poor fit for {profile.name}"


@dataclass(slots=True)
class _Context:
    """Read-only bundle of the facts a match is computed from."""

    name: str
    ccu: int | None
    visits: int | None
    score: float | None
    ratio: float | None
    engagement_pct: float | None
    monetization_score: float | None
    originality_score: float | None
    game_value_usd: float | None
    stage: str
    signals: GameSignals


def _exclusion_terms(profiles: Sequence[PartnerProfile]) -> list[str]:
    """Every term the profiles exclude, so all of them can be detected.

    A profile may exclude a genre ScoutForge has no global opinion about
    (``ugc``, ``anime``). Scanning the union of profile terms keeps those
    exclusions live without polluting the shared risk vocabulary.
    """
    terms: list[str] = []
    for profile in profiles:
        terms.extend(profile.exclude_risk)
        terms.extend(profile.extra_exclusions)
    return _dedupe(terms)


def _matching_keyword(term: str, keywords: list[str]) -> str | None:
    """Find the keyword a profile exclusion refers to.

    ``"modded"`` should catch a detected ``"mod apk"``, so a hit counts when the
    terms share a word boundary in either direction.
    """
    needle = normalise(term)
    if not needle:
        return None
    for keyword in keywords:
        hay = normalise(keyword)
        if not hay:
            continue
        if hay == needle or needle in hay or hay in needle:
            return keyword
    return None


def _band(value: float | None, low: float, high: float) -> float:
    if value is None:
        return 0.0
    if high <= low:
        return 0.0
    return max(0.0, min(1.0, (value - low) / (high - low)))


def _band_label(value: float) -> str:
    if value >= 0.85:
        return "top tier"
    if value >= 0.6:
        return "strong"
    if value >= 0.35:
        return "moderate"
    return "weak"


def _share_text(profile: PartnerProfile) -> str:
    if profile.revenue_share_min_pct and profile.revenue_share_max_pct:
        return f"{profile.revenue_share_min_pct:g}-{profile.revenue_share_max_pct:g}%"
    if profile.revenue_share_min_pct:
        return f"{profile.revenue_share_min_pct:g}%"
    return ""


def _deal_terms(profile: PartnerProfile) -> dict[str, Any]:
    return {
        "revenue_share_min_pct": profile.revenue_share_min_pct,
        "revenue_share_max_pct": profile.revenue_share_max_pct,
        "max_game_value_usd": profile.max_game_value_usd,
        "min_game_value_usd": profile.min_game_value_usd,
        "preferred_stages": list(profile.preferred_stages),
        "min_engagement_pct": profile.min_engagement_pct,
        "notes": profile.notes,
    }


def _exclusion_summary(profile: PartnerProfile) -> str:
    terms = list(profile.exclude_risk) + list(profile.extra_exclusions)
    return ", ".join(terms) if terms else "no content exclusions configured"


def _usd(value: float) -> str:
    return f"{value:,.0f}"


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0f}"


def _fmt_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.2f}%"


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


def match_game(
    *,
    universe_id: int,
    name: str | None,
    profiles: ProfileSet | None = None,
    **kwargs: Any,
) -> MatchResult:
    """Convenience wrapper around :class:`PartnerMatcher` for one-off calls."""
    return PartnerMatcher(profiles).match(universe_id=universe_id, name=name, **kwargs)


__all__ = [
    "MatchResult",
    "PartnerMatch",
    "PartnerMatcher",
    "classify_stage",
    "match_game",
]
