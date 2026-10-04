"""Unit tests for the pure partner matcher.

The matcher is the piece a scout argues with most ("why did it skip this?"), so
the tests here pin the two promises it makes: every profile is either eligible
with reasons or declined with the decisive mismatch, and exclusions declared by
a *profile* are actually detectable even when the shared vocabulary has never
heard of the genre.
"""

from __future__ import annotations

from typing import Any

from scoutforge.partners import (
    PartnerMatcher,
    classify_stage,
    default_profiles,
    extract_signals,
    load_profiles,
)
from scoutforge.partners.profiles import PartnerProfile


def _match(name: str, description: str = "", **overrides: Any) -> Any:
    defaults: dict[str, Any] = {
        "universe_id": 1,
        "name": name,
        "description": description or None,
        "ccu": 600,
        "visits": 1_000_000,
        "score": 75.0,
        "ratio": 0.0006,
        "engagement_pct": 8.0,
        "monetization_score": 40.0,
        "originality_score": 60.0,
        "game_value_usd": 24_000.0,
        "age_days": 120.0,
    }
    defaults.update(overrides)
    return PartnerMatcher(default_profiles()).match(**defaults)


class TestProfileDefinedExclusions:
    def test_a_genre_only_a_profile_knows_about_still_excludes(self) -> None:
        """``ugc`` is in no shared keyword list; the profile's own term must work."""
        signals = extract_signals(
            "Mega UGC Village",
            "build your dream town with friends",
            profile_keywords=["ugc", "anime"],
        )
        assert "ugc" in signals.profile_keywords

    def test_profile_only_terms_produce_an_exclusion(self) -> None:
        result = _match("Mega UGC Village", "a user generated content tycoon")
        declined = {m.partner_key: m for m in result.matches if not m.eligible}
        assert "ugc" in " ".join(declined["indg"].mismatches).lower()

    def test_anime_is_excluded_for_the_profile_that_declares_it(self) -> None:
        result = _match("Anime Fighters X", "ultimate anime battlegrounds")
        declined = {m.partner_key: m for m in result.matches if not m.eligible}
        assert any("anime" in reason.lower() for reason in declined["xolbor"].mismatches)

    def test_a_custom_label_is_used_when_the_profile_supplies_one(self) -> None:
        result = _match("Anime Fighters X", "ultimate anime battlegrounds")
        partner_3 = next(m for m in result.matches if m.partner_key == "partner_3")
        assert any("anime-styled titles" in reason for reason in partner_3.mismatches)

    def test_ordinary_games_are_not_excluded_by_those_terms(self) -> None:
        result = _match("Original RPG", "a handcrafted story adventure")
        assert "anime" not in " ".join(r for m in result.matches for r in m.mismatches)

    def test_a_shared_risk_term_still_uses_its_own_label(self) -> None:
        result = _match("Mount Simulator", "mount only, free assets")
        indg = next(m for m in result.matches if m.partner_key == "indg")
        assert not indg.eligible
        assert any("mount simulator" in reason for reason in indg.mismatches)


class TestVerdictShape:
    def test_every_profile_is_either_eligible_or_explained(self) -> None:
        result = _match("Steady Grown Game")
        assert result.considered == len(default_profiles().profiles)
        for match in result.matches:
            assert match.reasons, f"{match.partner_key} gave no reasons"
            if not match.eligible:
                assert match.mismatches, f"{match.partner_key} was skipped silently"

    def test_eligible_matches_come_first_and_are_capped(self) -> None:
        result = _match("Steady Grown Game")
        flags = [m.eligible for m in result.matches]
        assert flags == sorted(flags, reverse=True)
        assert len(result.matches) <= PartnerMatcher(default_profiles()).max_matches

    def test_scores_stay_in_range_and_carry_the_share(self) -> None:
        for match in _match("Steady Grown Game").matches:
            assert 0.0 <= match.score <= 100.0
            assert match.revenue_share

    def test_disabled_profiles_are_not_considered(self) -> None:
        profiles = load_profiles(
            {
                "profiles": [
                    {"key": "off", "name": "Off", "enabled": False},
                    {"key": "on", "name": "On"},
                ]
            }
        )
        result = PartnerMatcher(profiles).match(universe_id=2, name="Anything", score=80.0)
        assert {m.partner_key for m in result.matches} == {"on"}


class TestProfileSet:
    def test_defaults_load_six_profiles(self) -> None:
        assert len(default_profiles().profiles) == 6

    def test_a_profile_document_round_trips_through_to_dict(self) -> None:
        document = load_profiles(
            {"profiles": [{"key": "solo", "name": "Solo", "max_game_value_usd": 50_000}]}
        )
        payload = {"profiles": document.to_list()}
        again = load_profiles(payload)
        assert again.get("solo").max_game_value_usd == 50_000


class TestClassifyStage:
    def test_high_ccu_is_established(self) -> None:
        assert classify_stage(ccu=4_000, visits=100, age_days=10) == "established"

    def test_mid_size_is_growing(self) -> None:
        assert classify_stage(ccu=500, visits=100, age_days=10) == "growing"

    def test_a_small_old_game_is_not_still_early(self) -> None:
        """Roblox games sit at low CCU for years; age alone must not say 'early'."""
        assert classify_stage(ccu=60, visits=300_000, age_days=1_500) == "growing"

    def test_a_small_new_game_is_early(self) -> None:
        assert classify_stage(ccu=60, visits=300_000, age_days=30) == "early"


class TestProfileValidation:
    def test_a_profile_needs_a_key(self) -> None:
        import pytest

        with pytest.raises(TypeError):
            PartnerProfile(name="Nameless")  # type: ignore[call-arg]

    def test_an_unknown_stage_is_rejected_with_the_legal_set(self) -> None:
        import pytest

        with pytest.raises(ValueError, match="unknown stage"):
            PartnerProfile.from_dict({"key": "k", "name": "K", "preferred_stages": ["huge"]})
