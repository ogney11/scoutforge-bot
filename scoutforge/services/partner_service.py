"""Partner matching as an application service.

Responsibilities:

* resolve the *active* profile set (defaults -> JSON file -> database override)
* run the pure matcher for a scored game and persist the verdict
* expose reads for the dashboard, ``/partners`` and ``/match``
* record manual partner outreach (never performed by ScoutForge)

The matcher itself lives in :mod:`scoutforge.partners` and has no I/O, so it can
be reasoned about (and tested) on its own.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from scoutforge.config.logging import get_logger, log_event
from scoutforge.config.settings import PartnerSettings
from scoutforge.database.models import PartnerMatch
from scoutforge.database.repositories import (
    LeadPartnerRepository,
    PartnerMatchRepository,
    SettingsRepository,
)
from scoutforge.enums import PartnerOutreach
from scoutforge.partners.matcher import MatchResult, PartnerMatcher
from scoutforge.partners.profiles import ProfileSet, load_profiles
from scoutforge.scoring.scorer import ScoutScore
from scoutforge.utils import safe_div

log = get_logger(__name__)

#: Database setting key that overrides the profile set wholesale (JSON document).
KEY_PARTNER_PROFILES = "partner_profiles"
#: Database setting key for the reporting threshold.
KEY_PARTNER_MIN_SCORE = "partner.min_score"

#: Rough USD value of one live concurrent player per day, used only to test a
#: partner's ceiling/floor. It is an order-of-magnitude planning number, and the
#: matcher always labels it an *estimate*.
VALUE_PER_CCU_DAY_USD = 40.0


class ProfileLoadError(ValueError):
    """Raised when a stored profile document cannot be parsed."""


class PartnerService:
    """Reads and writes everything partner-related for one session."""

    def __init__(
        self,
        session: AsyncSession,
        settings: PartnerSettings | None = None,
        *,
        profiles: ProfileSet | None = None,
    ) -> None:
        self.session = session
        self.settings = settings or PartnerSettings()
        self.matches = PartnerMatchRepository(session)
        self.leads = LeadPartnerRepository(session)
        self._profiles = profiles

    # ------------------------------------------------------------- profiles
    async def profiles(self, *, override: Any = None) -> ProfileSet:
        """The active profile set.

        Precedence is explicit and documented: an explicit ``override`` (used by
        the dashboard settings page) beats the ``partner_profiles`` database row,
        which beats ``PARTNER_PROFILES_FILE``, which beats the built-in defaults.
        """
        if override is not None:
            return self._load(override, source="override")
        if self._profiles is not None:
            return self._profiles
        stored = await SettingsRepository(self.session).get(KEY_PARTNER_PROFILES)
        if stored is not None:
            return self._load(stored, source="database")
        if self.settings.profiles_file:
            return self._load(self.settings.profiles_file, source="file")
        return load_profiles()

    def _load(self, raw: Any, *, source: str) -> ProfileSet:
        try:
            return load_profiles(raw, risk_keywords=self.settings.risk_keywords)
        except (OSError, ValueError, TypeError) as exc:
            raise ProfileLoadError(f"partner profiles from {source}: {exc}") from exc

    async def save_profiles(self, document: Any, *, updated_by: int | None = None) -> ProfileSet:
        """Validate and persist a profile document, then return what was stored.

        Validation happens before the write, so a bad document can never replace
        a working profile set.
        """
        parsed = self._load(document, source="dashboard")
        if not parsed.profiles:
            raise ProfileLoadError("at least one partner profile is required")
        await SettingsRepository(self.session).set(
            KEY_PARTNER_PROFILES,
            {"profiles": parsed.to_list(), "risk_keywords": list(parsed.risk_keywords)},
            description="Partner profiles edited from the dashboard",
            updated_by=updated_by,
        )
        self._profiles = parsed
        log_event(log, 20, "PARTNER_PROFILES_SAVED", partners=len(parsed.profiles), by=updated_by)
        return parsed

    # -------------------------------------------------------------- matching
    async def match_game(
        self,
        *,
        game: Any,
        score: ScoutScore | None = None,
        score_components: dict[str, Any] | None = None,
        growth: Any = None,
        now: Any = None,
    ) -> MatchResult:
        """Rank the profile set against one game and persist the verdicts."""
        if not self.settings.enabled:
            return MatchResult(universe_id=game.universe_id)

        profiles = await self.profiles()
        scorer_value = score.score if score is not None else game.current_score
        if scorer_value is not None and scorer_value < self.settings.min_game_score:
            log_event(
                log,
                20,
                "PARTNER_MATCH_SKIPPED",
                universe=game.universe_id,
                reason="below_min_game_score",
                score=round(scorer_value, 2),
            )
            return MatchResult(universe_id=game.universe_id)

        components = dict(score_components or (score.component_map() if score else {}))
        engagement = _component_value(components, "engagement")
        monetization = _component_value(components, "monetization")
        originality = _component_value(components, "originality")

        matcher = PartnerMatcher(
            profiles,
            min_score=self.settings.min_score,
            max_matches=self.settings.max_matches_per_game,
        )
        ratio = safe_div(game.current_ccu, game.lifetime_visits)
        result = matcher.match(
            universe_id=game.universe_id,
            name=game.game_name,
            description=game.description,
            ccu=game.current_ccu,
            visits=game.lifetime_visits,
            score=scorer_value,
            ratio=ratio,
            # engagement_score is 0..1 of its own weight; reuse it directly as a
            # 0..1 signal rather than re-deriving likes/visits from stale columns.
            engagement_pct=engagement * 100 if engagement is not None else None,
            monetization_score=(monetization * 100) if monetization is not None else None,
            originality_score=(originality * 100) if originality is not None else None,
            game_value_usd=estimate_value_usd(
                game.current_ccu, ccus_per_day=getattr(growth, "ccus_per_day", None)
            ),
            age_days=_age_days(game, now=now),
        )
        await self.matches.replace_for_game(
            game.universe_id,
            [match.to_dict() for match in result.matches],
            scout_score=scorer_value,
            game_stage=_stage_of(result),
        )
        log_event(
            log,
            20,
            "PARTNER_MATCHED",
            universe=game.universe_id,
            eligible=len(result.eligible),
            considered=result.considered,
        )
        return result

    async def match_payload(self, universe_id: int) -> dict[str, Any]:
        """Stored matches for a game, ready for the dashboard or an embed."""
        rows = await self.matches.list_for_game(universe_id)
        outreach = {row.partner_key: row for row in await self.leads.list_for_game(universe_id)}
        return {
            "universe_id": universe_id,
            "matches": [_match_block(row, outreach.get(row.partner_key)) for row in rows],
        }

    # --------------------------------------------------------------- reads
    async def list_profiles(self) -> list[dict[str, Any]]:
        """Profile set plus how many games each partner currently fits."""
        profiles = await self.profiles()
        counts = await self.matches.counts_by_partner()
        out: list[dict[str, Any]] = []
        for profile in profiles.profiles:
            entry = profile.to_dict()
            entry["matched_games"] = counts.get(profile.key, 0)
            out.append(entry)
        return out

    async def top_games_for(self, partner_key: str, *, limit: int = 25) -> list[dict[str, Any]]:
        rows = await self.matches.top_for_partner(partner_key, limit=limit)
        return [
            {
                "universe_id": row.universe_id,
                "partner": row.partner_name or row.partner_key,
                "score": round(row.score, 1),
                "reasons": list(row.reasons or []),
                "revenue_share": row.revenue_share,
                "matched_at": row.matched_at.isoformat() if row.matched_at else None,
            }
            for row in rows
        ]

    async def pipeline(self, *, limit: int = 100) -> dict[str, Any]:
        rows = await self.leads.pipeline(limit=limit)
        return {
            "by_status": await self.leads.counts_by_status(),
            "rows": [
                {
                    "universe_id": row.universe_id,
                    "partner": row.partner_name or row.partner_key,
                    "partner_key": row.partner_key,
                    "status": row.status,
                    "match_score": row.match_score,
                    "revenue_share_pct": row.revenue_share_pct,
                    "contacted_at": row.contacted_at.isoformat() if row.contacted_at else None,
                    "contact_method": row.contact_method,
                    "contacted_by_name": row.contacted_by_name,
                    "notes": row.notes,
                }
                for row in rows
            ],
        }

    # ------------------------------------------------------------- outreach
    async def shortlist(
        self,
        universe_id: int,
        partner_key: str,
        *,
        partner_name: str = "",
        notes: str | None = None,
    ) -> dict[str, Any]:
        """Mark a lead as shortlisted for a partner. No message is sent."""
        match = await self._find_match(universe_id, partner_key)
        profile = await self._profile(partner_key)
        row = await self.leads.upsert(
            universe_id,
            partner_key,
            partner_name=partner_name or (match.partner_name if match else "") or partner_key,
            match_score=match.score if match else None,
            status=PartnerOutreach.SHORTLISTED,
            revenue_share_pct=profile.revenue_share_min_pct if profile else None,
            deal_terms=match.deal_terms if match else {},
            notes=notes,
        )
        return _lead_partner_block(row)

    async def record_contact(
        self,
        universe_id: int,
        partner_key: str,
        *,
        contact_method: str,
        contact_reference: str | None = None,
        author_id: int | None = None,
        author_name: str | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        """Log that a human reached out. ScoutForge never sends anything itself."""
        match = await self._find_match(universe_id, partner_key)
        row = await self.leads.record_contact(
            universe_id,
            partner_key,
            contact_method=contact_method,
            contact_reference=contact_reference,
            author_id=author_id,
            author_name=author_name,
            partner_name=match.partner_name if match else "",
            notes=notes,
        )
        log_event(
            log,
            20,
            "PARTNER_CONTACT_RECORDED",
            universe=universe_id,
            partner=partner_key,
            method=contact_method,
            by=author_id,
        )
        return _lead_partner_block(row)

    async def set_outreach_status(
        self,
        universe_id: int,
        partner_key: str,
        status: PartnerOutreach | str,
        *,
        notes: str | None = None,
        revenue_share_pct: float | None = None,
    ) -> dict[str, Any]:
        """Move a partner row along the outreach funnel.

        The stored match and profile supply the name, score and share on first
        write, so logging ``INTERESTED`` never loses the negotiated terms.
        """
        match = await self._find_match(universe_id, partner_key)
        profile = await self._profile(partner_key)
        row = await self.leads.upsert(
            universe_id,
            partner_key,
            partner_name=(match.partner_name if match else "") or (profile.name if profile else ""),
            match_score=match.score if match else None,
            status=status,
            revenue_share_pct=(
                revenue_share_pct
                if revenue_share_pct is not None
                else (profile.revenue_share_min_pct if profile else None)
            ),
            deal_terms=match.deal_terms if match else None,
            notes=notes,
        )
        return _lead_partner_block(row)

    async def _find_match(self, universe_id: int, partner_key: str) -> PartnerMatch | None:
        for row in await self.matches.list_for_game(universe_id):
            if row.partner_key == partner_key:
                return row
        return None

    async def _profile(self, partner_key: str) -> Any:
        return (await self.profiles()).get(partner_key)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _component_value(components: dict[str, Any], key: str) -> float | None:
    entry = components.get(key)
    if not isinstance(entry, dict):
        return None
    value = entry.get("value")
    return float(value) if isinstance(value, int | float) else None


def _age_days(game: Any, *, now: Any) -> float | None:
    from scoutforge.utils import ensure_utc, utcnow

    reference = ensure_utc(now) or utcnow()
    created = ensure_utc(getattr(game, "game_created_at", None))
    if created is None:
        return None
    return max(0.0, (reference - created).total_seconds() / 86400.0)


def estimate_value_usd(ccu: int | None, *, ccus_per_day: float | None = None) -> float | None:
    """Very rough planning value for testing a partner's cheque size.

    Deliberately crude and always presented as an estimate: it exists so a
    partner with a $30K or $500K ceiling can be compared against a game without
    pretending to be a valuation model.
    """
    if ccus_per_day is not None and ccus_per_day > 0:
        return float(ccus_per_day) * VALUE_PER_CCU_DAY_USD
    if not ccu or ccu <= 0:
        return None
    return float(ccu) * VALUE_PER_CCU_DAY_USD


def _stage_of(result: MatchResult) -> str | None:
    for match in result.matches:
        if match.eligible and match.deal_terms.get("preferred_stages"):
            return str(match.deal_terms["preferred_stages"][0])
    return None


def _match_block(row: PartnerMatch, outreach: Any) -> dict[str, Any]:
    return {
        "partner_key": row.partner_key,
        "partner": row.partner_name or row.partner_key,
        "partner_name": row.partner_name or row.partner_key,
        "score": round(row.score, 1),
        "eligible": row.eligible,
        "revenue_share": row.revenue_share,
        "reasons": list(row.reasons or []),
        "exclusions": list(row.exclusions or []),
        "deal_terms": dict(row.deal_terms or {}),
        "game_stage": row.game_stage,
        "scout_score": row.scout_score,
        "matched_at": row.matched_at.isoformat() if row.matched_at else None,
        "outreach": _lead_partner_block(outreach) if outreach is not None else None,
    }


def _lead_partner_block(row: Any) -> dict[str, Any]:
    return {
        "partner_key": row.partner_key,
        "partner": row.partner_name or row.partner_key,
        "status": row.status,
        "match_score": row.match_score,
        "revenue_share_pct": row.revenue_share_pct,
        "deal_terms": dict(row.deal_terms or {}),
        "notes": row.notes,
        "contacted_at": row.contacted_at.isoformat() if row.contacted_at else None,
        "contact_method": row.contact_method,
        "contact_reference": row.contact_reference,
        "contacted_by_name": row.contacted_by_name,
        "responded_at": row.responded_at.isoformat() if row.responded_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


__all__ = [
    "KEY_PARTNER_MIN_SCORE",
    "KEY_PARTNER_PROFILES",
    "VALUE_PER_CCU_DAY_USD",
    "PartnerService",
    "ProfileLoadError",
    "estimate_value_usd",
]
