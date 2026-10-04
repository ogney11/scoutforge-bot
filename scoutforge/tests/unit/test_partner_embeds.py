"""Unit tests for the partner-facing embeds.

Partner data reaches Discord from two places - the HOT/EXTREME alert payload and
``/partners`` / ``/match`` - so these tests pin the two rules that matter: an
alert only ever names *eligible* partners, and ``/match`` must always explain
why a profile passed or passed it over.
"""

from __future__ import annotations

from typing import Any

from scoutforge.bot.embeds.builder import alert_embed, match_embed, partners_embed

EMBED_DESCRIPTION_LIMIT = 4_096
EMBED_FIELD_VALUE_LIMIT = 1_024


def _fields(embed: Any) -> dict[str, str]:
    return {field.name: str(field.value) for field in embed.fields}


def _all_text(embed: Any) -> str:
    parts = [embed.title or "", embed.description or ""]
    for field in embed.fields:
        parts.extend([field.name or "", str(field.value)])
    return "\n".join(parts)


def _payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "universe_id": 1,
        "game_name": "Test Game",
        "level": "HOT",
        "score": 92.0,
        "current_ccu": 900,
        "lifetime_visits": 1_200_000,
        "ccu_to_visits_ratio": 0.00075,
        "growth_pct": 12.5,
        "growth_window_hours": 6,
        "roblox_url": "https://www.roblox.com/games/1",
        "discord_url": None,
        "creator_name": "someone",
        "creator_type": "Group",
        "thumbnail_url": None,
        "first_seen_at": "2026-01-01T00:00:00+00:00",
        "reasons": ["high CCU"],
        "caveats": [],
        "partners": [],
        "mention": None,
    }
    payload.update(overrides)
    return payload


class TestAlertEmbedPartners:
    def test_omits_the_block_when_no_partner_matched(self) -> None:
        embed = alert_embed(_payload())
        assert "🤝 Partner matches" not in _fields(embed)

    def test_names_only_eligible_partners(self) -> None:
        embed = alert_embed(
            _payload(
                partners=[
                    {
                        "partner_key": "indg",
                        "partner_name": "INDG",
                        "score": 88.0,
                        "eligible": True,
                        "reasons": ["Scout Score 92/100 (top tier)"],
                        "revenue_share": "20-30%",
                    },
                    {
                        "partner_key": "xolbor",
                        "partner_name": "Xolbor",
                        "score": 61.0,
                        "eligible": False,
                        "reasons": ["Scout Score 92/100 (top tier)"],
                        "revenue_share": "50%",
                    },
                ]
            )
        )
        rendered = _fields(embed)
        block = rendered["🤝 Partner matches"]
        assert "INDG" in block
        assert "Xolbor" not in block

    def test_renders_score_share_and_first_reason(self) -> None:
        embed = alert_embed(
            _payload(
                partners=[
                    {
                        "partner_key": "indg",
                        "partner_name": "INDG",
                        "score": 88.0,
                        "eligible": True,
                        "reasons": ["high CCU", "second reason"],
                        "revenue_share": "20-30%",
                    }
                ]
            )
        )
        block = _fields(embed)["🤝 Partner matches"]
        assert "88/100" in block
        assert "20-30%" in block
        assert "high CCU" in block
        assert "second reason" not in block

    def test_respects_discord_field_limit_with_many_partners(self) -> None:
        embed = alert_embed(
            _payload(
                partners=[
                    {
                        "partner_key": f"p{i}",
                        "partner_name": f"Partner {i}",
                        "score": 90.0,
                        "eligible": True,
                        "reasons": ["x" * 200],
                    }
                    for i in range(12)
                ]
            )
        )
        block = _fields(embed)["🤝 Partner matches"]
        assert len(block) <= EMBED_FIELD_VALUE_LIMIT
        assert block.count("\n") >= 2

    def test_unknown_partner_fields_do_not_crash(self) -> None:
        embed = alert_embed(_payload(partners=[{"partner_key": "indg"}]))
        assert "indg" in _all_text(embed)


class TestPartnersEmbed:
    def test_lists_one_field_per_profile(self) -> None:
        embed = partners_embed(
            [
                {
                    "key": "indg",
                    "name": "INDG",
                    "matched_games": 12,
                    "revenue_share_min_pct": 20,
                    "revenue_share_max_pct": 30,
                    "preferred_stages": ["early"],
                    "preferred_ccu_min": 100,
                    "preferred_visits_max": 5_000_000,
                    "min_engagement_pct": 5,
                    "exclude_risk": ["mount", "ugc"],
                },
                {"key": "xolbor", "name": "Xolbor", "matched_games": 0},
            ]
        )
        rendered = _fields(embed)
        assert "INDG • 12 matched" in rendered
        terms = rendered["INDG • 12 matched"]
        assert "20-30%" in terms
        assert "early" in terms
        assert "mount, ugc" in terms
        assert rendered["Xolbor • 0 matched"] == "no criteria configured"

    def test_marks_a_disabled_profile(self) -> None:
        embed = partners_embed([{"key": "indg", "name": "INDG", "enabled": False}])
        assert "INDG • disabled" in _fields(embed)

    def test_handles_an_empty_profile_set(self) -> None:
        embed = partners_embed([])
        assert embed.fields == []
        assert "No partner profiles" in (embed.description or "")

    def test_falls_back_to_the_key_when_unnamed(self) -> None:
        embed = partners_embed([{"key": "solo_partner"}])
        assert "solo_partner" in _fields(embed)


class TestMatchEmbed:
    def _matches(self) -> list[dict[str, Any]]:
        return [
            {
                "partner_key": "indg",
                "partner_name": "INDG",
                "score": 88.0,
                "eligible": True,
                "reasons": ["Scout Score 92/100 (top tier)", "no exclusions found"],
                "exclusions": [],
                "revenue_share": "20-30%",
            },
            {
                "partner_key": "locked_dev",
                "partner_name": "Locked.dev",
                "score": 44.0,
                "eligible": False,
                "reasons": ["Scout Score 92/100 (top tier)"],
                "exclusions": ["engagement 4.0% is below the 20% requirement"],
                "revenue_share": "100%",
            },
        ]

    def test_shows_eligible_in_description_and_passed_in_a_field(self) -> None:
        embed = match_embed(game_name="Test Game", universe_id=42, matches=self._matches())
        assert "INDG" in (embed.description or "")
        assert "88/100" in (embed.description or "")
        assert "Locked.dev" in _fields(embed)["Not a fit"]
        assert "engagement 4.0%" in _fields(embed)["Not a fit"]

    def test_explains_when_nobody_fits(self) -> None:
        matches = [{**self._matches()[1]}]
        embed = match_embed(game_name="Test Game", universe_id=42, matches=matches)
        assert "No partner profile currently fits" in (embed.description or "")

    def test_explains_when_nothing_is_stored_yet(self) -> None:
        embed = match_embed(game_name="Test Game", universe_id=42, matches=[])
        assert "No partner matches stored" in (embed.description or "")

    def test_respects_discord_limits(self) -> None:
        embed = match_embed(
            game_name="G" * 500,
            universe_id=1,
            matches=[
                {
                    "partner_key": f"p{i}",
                    "partner_name": f"Partner {i}",
                    "score": 90.0,
                    "eligible": True,
                    "reasons": ["y" * 300],
                }
                for i in range(30)
            ],
        )
        assert len(embed.description or "") <= EMBED_DESCRIPTION_LIMIT
        for field in embed.fields:
            assert len(str(field.value)) <= EMBED_FIELD_VALUE_LIMIT
