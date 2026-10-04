"""Candidate de-duplication and near-duplicate (re-upload) detection."""

from __future__ import annotations

import pytest

from scoutforge.discovery.dedupe import (
    Deduplicator,
    find_near_duplicates,
    merge_candidates,
    normalize_game_name,
)
from scoutforge.discovery.models import GameCandidate


def candidate(**overrides) -> GameCandidate:
    base = {
        "universe_id": 1,
        "source": "roblox_explore",
        "place_id": 100,
        "name": "Test Game",
        "creator_id": 7,
        "creator_name": "Dev",
        "current_ccu": 1_000,
        "lifetime_visits": 50_000,
    }
    base.update(overrides)
    return GameCandidate(**base)


class TestNormaliseName:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Test Game", "test game"),
            ("TEST GAME", "test game"),
            ("Test  Game", "test game"),
            ("  Padded  ", "padded"),
            ("Cafe Tycoon", "cafe tycoon"),
            ("Café Tycoon", "cafe tycoon"),
            ("Cool-Game_v2!!", "cool game v2"),
            ("", ""),
            (None, ""),
        ],
    )
    def test_normalisation(self, raw: str | None, expected: str) -> None:
        assert normalize_game_name(raw) == expected


class TestMerge:
    def test_same_universe_from_two_sources_merges(self) -> None:
        merged = merge_candidates(
            [
                candidate(universe_id=5, source="roblox_explore"),
                candidate(universe_id=5, source="roblox_search"),
            ]
        )
        assert list(merged) == [5]
        assert merged[5].source_count == 2
        assert len(merged[5].candidates) == 2

    def test_distinct_universes_stay_separate(self) -> None:
        merged = merge_candidates([candidate(universe_id=1), candidate(universe_id=2)])
        assert set(merged) == {1, 2}

    def test_invalid_universe_ids_are_dropped(self) -> None:
        merged = merge_candidates([candidate(universe_id=0), candidate(universe_id=-3)])
        assert merged == {}

    def test_primary_prefers_the_richer_payload(self) -> None:
        merged = merge_candidates(
            [
                candidate(universe_id=5, current_ccu=None, lifetime_visits=None, name=None),
                candidate(universe_id=5, current_ccu=900, source="roblox_search"),
            ]
        )
        assert merged[5].primary.current_ccu == 900

    def test_primary_prefers_earlier_discovery_rank(self) -> None:
        merged = merge_candidates(
            [
                candidate(universe_id=5, discovery_rank=9),
                candidate(universe_id=5, discovery_rank=1),
            ]
        )
        assert merged[5].primary.discovery_rank == 1


class TestNearDuplicates:
    def test_same_name_same_creator_is_a_near_duplicate(self) -> None:
        merged = merge_candidates([candidate(universe_id=1), candidate(universe_id=2)])
        duplicates = find_near_duplicates(merged)
        assert duplicates[1] == [2]
        assert duplicates[2] == [1]

    def test_punctuation_and_case_differences_still_match(self) -> None:
        merged = merge_candidates(
            [
                candidate(universe_id=1, name="Cool Game"),
                candidate(universe_id=2, name="cool-game"),
            ]
        )
        assert 1 in find_near_duplicates(merged)

    def test_same_name_different_creator_is_not_a_duplicate(self) -> None:
        merged = merge_candidates(
            [
                candidate(universe_id=1, creator_id=7),
                candidate(universe_id=2, creator_id=8),
            ]
        )
        assert find_near_duplicates(merged) == {}

    def test_short_names_are_ignored(self) -> None:
        """A 3-character name would collide with half the platform."""
        merged = merge_candidates(
            [candidate(universe_id=1, name="abc"), candidate(universe_id=2, name="abc")]
        )
        assert find_near_duplicates(merged) == {}

    def test_single_game_has_no_duplicates(self) -> None:
        assert find_near_duplicates(merge_candidates([candidate(universe_id=1)])) == {}


class TestDeduplicator:
    def test_add_all_then_universe_ids(self) -> None:
        deduper = Deduplicator()
        deduper.add_all([candidate(universe_id=1), candidate(universe_id=2)])
        deduper.add_all([candidate(universe_id=1)])
        assert sorted(deduper.universe_ids) == [1, 2]
        assert len(deduper) == 2
        assert bool(deduper) is True

    def test_results_expose_sources(self) -> None:
        deduper = Deduplicator()
        deduper.add(candidate(universe_id=1, source="a"))
        deduper.add(candidate(universe_id=1, source="b"))
        assert deduper.results[1].sources == {"a", "b"}

    def test_empty_deduplicator_is_falsy(self) -> None:
        assert bool(Deduplicator()) is False

    def test_near_duplicates_helper_on_the_class(self) -> None:
        deduper = Deduplicator()
        deduper.add(candidate(universe_id=1, name="Same Name"))
        deduper.add(candidate(universe_id=2, name="Same Name"))
        assert deduper.near_duplicates()[1] == [2]
