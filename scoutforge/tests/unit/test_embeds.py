"""Unit tests for :mod:`scoutforge.bot.embeds.builder`.

Embeds are pure functions of their inputs, so they can be asserted directly.
The properties that matter: Discord's hard limits are respected, unknown data
renders as a placeholder rather than ``None``/``0``, and the HOT/EXTREME
treatment stays visually distinct.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from discord import Colour, Embed

from scoutforge.bot.embeds.builder import (
    COLOUR_ERROR,
    COLOUR_EXTREME,
    COLOUR_HOT,
    COLOUR_NORMAL,
    LEVEL_COLOURS,
    LEVEL_EMOJI,
    STATUS_EMOJI,
    alert_embed,
    error_embed,
    help_embed,
    lead_embed,
    level_colour,
    level_emoji,
    mention_user,
    role_mention,
    scan_result_embed,
    stats_embed,
)
from scoutforge.enums import AlertLevel, LeadStatus, RunStatus
from scoutforge.tests.conftest import NOW

EMBED_DESCRIPTION_LIMIT = 4_096
EMBED_FIELD_VALUE_LIMIT = 1_024
EMBED_TOTAL_LIMIT = 6_000
EMBED_FIELD_COUNT_LIMIT = 25


def fields(embed: Embed) -> dict[str, str]:
    return {field.name: str(field.value) for field in embed.fields}


class FakeGame:
    """Minimal stand-in for a ``Game`` row."""

    def __init__(self, **overrides: Any) -> None:
        defaults: dict[str, Any] = {
            "universe_id": 900_000_001,
            "place_id": 1_234_567_890,
            "game_name": "Integration Game",
            "description": "A very interesting game about testing.",
            "creator_id": 42,
            "creator_name": "TestDev",
            "creator_type": "User",
            "group_id": None,
            "group_name": None,
            "current_ccu": 5_000,
            "lifetime_visits": 120_000,
            "favorites": 9_000,
            "peak_ccu": 6_200,
            "current_score": 93.4,
            "current_level": AlertLevel.HOT.value,
            "status": LeadStatus.NEW.value,
            "missing_metrics": [],
            "thumbnail_url": "https://tr.rbxcdn.test/thumb.png",
            "first_seen_at": NOW - timedelta(days=3),
            "last_seen_at": NOW - timedelta(minutes=20),
            "ccu_to_visits_ratio": 0.0416,
            "roblox_url": "https://www.roblox.com/games/1234567890",
            "public_discord_url": "https://discord.gg/example",
            "creator_url": "https://www.roblox.com/users/42/profile",
        }
        defaults.update(overrides)
        for key, value in defaults.items():
            setattr(self, key, value)


class FakeNote:
    def __init__(self, body: str, author_name: str | None = "scout") -> None:
        self.body = body
        self.author_name = author_name


def all_text(embed: Embed) -> str:
    """Everything a reader would see, for limit and placeholder assertions."""
    parts = [embed.title or "", embed.description or ""]
    parts += [field.name for field in embed.fields]
    parts += [str(field.value) for field in embed.fields]
    if embed.footer.text:
        parts.append(embed.footer.text)
    if embed.author.name:
        parts.append(embed.author.name)
    return "\n".join(parts)


class TestLevelHelpers:
    @pytest.mark.parametrize("level", list(AlertLevel))
    def test_every_level_has_an_emoji_and_colour(self, level: AlertLevel) -> None:
        assert level_emoji(level)
        assert isinstance(level_colour(level), Colour)
        assert LEVEL_EMOJI[level] != ""
        assert level_colour(level) == LEVEL_COLOURS[level]

    @pytest.mark.parametrize("level", list(AlertLevel))
    def test_raw_strings_are_accepted(self, level: AlertLevel) -> None:
        assert level_emoji(level.value) == level_emoji(level)
        assert level_colour(level.value) == level_colour(level)

    def test_extreme_is_visually_distinct_from_hot(self) -> None:
        assert level_colour(AlertLevel.EXTREME) == COLOUR_EXTREME
        assert level_colour(AlertLevel.HOT) == COLOUR_HOT
        assert COLOUR_EXTREME != COLOUR_HOT
        assert level_emoji(AlertLevel.EXTREME) != level_emoji(AlertLevel.HOT)

    @pytest.mark.parametrize("value", [None, "", "NOT_A_LEVEL", 7])
    def test_unknown_levels_fall_back_to_normal(self, value: Any) -> None:
        assert level_emoji(value) == LEVEL_EMOJI[AlertLevel.NORMAL]
        assert level_colour(value) == COLOUR_NORMAL


class TestLeadEmbed:
    def test_renders_the_core_metrics(self) -> None:
        embed = lead_embed(game=FakeGame())
        rendered = fields(embed)

        assert embed.title.startswith(LEVEL_EMOJI[AlertLevel.HOT])
        assert "Integration Game" in embed.title
        assert rendered["👥 CCU"] == "5K"
        assert rendered["👀 Lifetime Visits"] == "120,000"
        assert rendered["📊 CCU/Visits"] == "4.16%"
        assert rendered["Creator"] == "TestDev (User)"
        assert rendered["Universe ID"] == "`900000001`"
        assert rendered["⭐ Favorites"] == "9,000"

    def test_score_bar_is_rendered_with_the_level(self) -> None:
        rendered = fields(lead_embed(game=FakeGame()))
        assert "93/100" in rendered["🔥 Scout Score"]
        assert "HOT" in rendered["🔥 Scout Score"]
        assert "█" in rendered["🔥 Scout Score"]

    def test_unscored_games_say_so_instead_of_showing_zero(self) -> None:
        embed = lead_embed(game=FakeGame(current_score=None), score=None)
        assert fields(embed)["🔥 Scout Score"] == "unscored"

    def test_missing_metrics_are_listed(self) -> None:
        embed = lead_embed(game=FakeGame(missing_metrics=["favorites", "likes"]))
        assert "⚠️ Unavailable metrics" in fields(embed)
        assert "favorites, likes" in fields(embed)["⚠️ Unavailable metrics"]

    @pytest.mark.parametrize(
        ("field", "overrides"),
        [
            ("👥 CCU", {"current_ccu": None}),
            ("👀 Lifetime Visits", {"lifetime_visits": None}),
            ("📊 CCU/Visits", {"ccu_to_visits_ratio": None}),
            ("Creator", {"creator_name": None}),
            ("Group", {"group_id": None, "group_name": None}),
        ],
    )
    def test_unknown_data_renders_as_a_placeholder(
        self, field: str, overrides: dict[str, Any]
    ) -> None:
        """Regression risk: ``None`` must never reach Discord as ``"None"``."""
        rendered = fields(lead_embed(game=FakeGame(**overrides)))
        assert "None" not in rendered[field]
        assert rendered[field].startswith(("n/a", "unknown")), rendered[field]

    def test_links_include_every_available_destination(self) -> None:
        rendered = fields(lead_embed(game=FakeGame()))
        assert "[Roblox](" in rendered["Links"]
        assert "[Discord](" in rendered["Links"]
        assert "[Creator](" in rendered["Links"]

    def test_links_still_render_without_a_roblox_url(self) -> None:
        rendered = fields(lead_embed(game=FakeGame(roblox_url=None)))
        assert "[Discord](" in rendered["Links"]

    def test_growth_prefers_the_longest_available_window(self) -> None:
        embed = lead_embed(
            game=FakeGame(),
            growth={
                "windows": {
                    "24": {"pct_change": 120.0},
                    "6": {"pct_change": 40.0},
                    "1": {"pct_change": 10.0},
                }
            },
        )
        assert fields(embed)["📈 Growth"] == "+120.00% / 24h"

    def test_growth_falls_back_through_shorter_windows(self) -> None:
        embed = lead_embed(game=FakeGame(), growth={"windows": {"1": {"pct_change": 10.0}}})
        assert fields(embed)["📈 Growth"] == "+10.00% / 1h"

    @pytest.mark.parametrize("growth", [{}, {"windows": {}}, {"windows": {"24": None}}])
    def test_no_growth_history_is_stated_plainly(self, growth: dict[str, Any]) -> None:
        embed = lead_embed(game=FakeGame(), growth=growth)
        assert fields(embed)["📈 Growth"] == "no history yet"

    def test_long_descriptions_are_truncated_with_an_ellipsis(self) -> None:
        embed = lead_embed(game=FakeGame(description="x" * 5_000))
        assert embed.description is not None
        assert len(embed.description) < 5_000
        assert embed.description.endswith("…")

    def test_notes_are_only_shown_when_requested(self) -> None:
        notes = [FakeNote("looks promising")]
        assert "🗒 Notes" not in fields(lead_embed(game=FakeGame(), notes=notes))
        assert "🗒 Notes" in fields(lead_embed(game=FakeGame(), notes=notes, show_notes=True))

    def test_notes_without_an_author_fall_back_to_scout(self) -> None:
        embed = lead_embed(
            game=FakeGame(), notes=[FakeNote("text", author_name=None)], show_notes=True
        )
        assert "scout: text" in fields(embed)["🗒 Notes"]

    def test_status_override_wins_over_the_games_own(self) -> None:
        rendered = fields(
            lead_embed(game=FakeGame(status=LeadStatus.NEW.value), status=LeadStatus.NEGOTIATING)
        )
        assert "NEGOTIATING" in rendered[f"{STATUS_EMOJI[LeadStatus.NEGOTIATING]} Status"]

    def test_nameless_games_fall_back_to_the_universe_id(self) -> None:
        embed = lead_embed(game=FakeGame(game_name=None))
        assert "900000001" in embed.title

    def test_respects_discord_limits(self) -> None:
        embed = lead_embed(
            game=FakeGame(
                game_name="G" * 300,
                description="d" * 4_000,
                creator_name="c" * 300,
                missing_metrics=[f"metric_{index}" for index in range(50)],
            ),
            notes=[FakeNote("n" * 500) for _ in range(10)],
            show_notes=True,
        )
        assert len(all_text(embed)) <= EMBED_TOTAL_LIMIT
        assert all(len(str(field.value)) <= EMBED_FIELD_VALUE_LIMIT for field in embed.fields)


class TestAlertEmbed:
    def _payload(self, **overrides: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "game_name": "Hot Game",
            "level": AlertLevel.HOT.value,
            "roblox_url": "https://www.roblox.com/games/1",
            "discord_url": "https://discord.gg/example",
            "thumbnail_url": "https://tr.rbxcdn.test/thumb.png",
            "creator_name": "TestDev",
            "current_ccu": 12_000,
            "lifetime_visits": 300_000,
            "ccu_to_visits_ratio": 0.04,
            "growth_pct": 55.0,
            "growth_window_hours": 1,
            "score": 96.5,
            "reasons": ["CCU above threshold", "fast 1h growth"],
            "caveats": ["no 24h history"],
            "first_seen_at": NOW.isoformat(),
        }
        payload.update(overrides)
        return payload

    def test_hot_treatment(self) -> None:
        embed = alert_embed(self._payload())
        assert embed.title.startswith("🔥 HOT")
        assert embed.colour == COLOUR_HOT

    def test_extreme_treatment(self) -> None:
        embed = alert_embed(self._payload(level=AlertLevel.EXTREME.value))
        assert embed.title.startswith("🚨 EXTREME")
        assert embed.colour == COLOUR_EXTREME

    def test_unknown_level_defaults_to_hot(self) -> None:
        embed = alert_embed(self._payload(level="NOT_A_LEVEL"))
        assert embed.title.startswith("🔥 HOT")

    def test_missing_level_defaults_to_hot(self) -> None:
        embed = alert_embed(self._payload(level=None))
        assert embed.title.startswith("🔥 HOT")

    def test_reasons_and_caveats_are_surfaced(self) -> None:
        rendered = fields(alert_embed(self._payload()))
        assert "CCU above threshold" in rendered["Why"]
        assert "no 24h history" in rendered["Limitations"]

    def test_missing_reasons_do_not_create_an_empty_field(self) -> None:
        rendered = fields(alert_embed(self._payload(reasons=[], caveats=[])))
        assert "Why" not in rendered
        assert "Limitations" not in rendered

    def test_mention_becomes_the_author(self) -> None:
        embed = alert_embed(self._payload(), mention="<@123>")
        assert embed.author.name == "<@123>"

    def test_no_mention_leaves_the_author_unset(self) -> None:
        assert alert_embed(self._payload()).author.name is None

    def test_unknown_metrics_render_as_placeholders(self) -> None:
        rendered = fields(
            alert_embed(
                self._payload(
                    current_ccu=None,
                    lifetime_visits=None,
                    ccu_to_visits_ratio=None,
                    growth_pct=None,
                    game_name=None,
                    creator_name=None,
                )
            )
        )
        assert rendered["👥 CCU"] == "n/a"
        assert rendered["👀 Lifetime Visits"] == "n/a"
        assert rendered["📊 CCU/Visits"] == "n/a"
        assert rendered["📈 Growth"] == "no history yet"
        assert "None" not in rendered["Creator"]

    def test_unparseable_timestamp_does_not_crash(self) -> None:
        rendered = fields(alert_embed(self._payload(first_seen_at="not a date")))
        assert rendered["First detected"].startswith("unknown")

    def test_respects_discord_limits(self) -> None:
        embed = alert_embed(
            self._payload(
                game_name="G" * 300,
                reasons=[f"reason {index} " * 10 for index in range(40)],
                caveats=[f"caveat {index} " * 10 for index in range(20)],
            )
        )
        assert len(all_text(embed)) <= EMBED_TOTAL_LIMIT
        assert all(len(str(field.value)) <= EMBED_FIELD_VALUE_LIMIT for field in embed.fields)


class FakeScanResult:
    def __init__(self, **overrides: Any) -> None:
        defaults: dict[str, Any] = {
            "run_id": 7,
            "status": RunStatus.SUCCESS,
            "started_at": NOW - timedelta(seconds=90),
            "finished_at": NOW,
            "candidates_found": 500,
            "candidates_processed": 500,
            "games_discovered": 12,
            "games_accepted": 4,
            "games_rejected": 496,
            "games_updated": 3,
            "snapshots_written": 60,
            "scores_computed": 7,
            "alerts_sent": 2,
            "alerts_suppressed": 5,
            "api_errors": 0,
            "rejection_reasons": {"below_ccu": 400, "too_many_visits": 96},
            "source_stats": {
                "roblox_explore": {"candidates": 300, "pages": 3},
                "roblox_search": {"candidates": 200, "pages": 1, "notes": "partial"},
            },
            "error": None,
        }
        defaults.update(overrides)
        for key, value in defaults.items():
            setattr(self, key, value)


class TestScanResultEmbed:
    def test_counts_are_reported(self) -> None:
        rendered = fields(scan_result_embed(FakeScanResult()))
        assert rendered["Candidates found"] == "500"
        assert rendered["Accepted"] == "4"
        assert rendered["Alerts suppressed"] == "5"
        assert rendered["Run ID"] == "`7`"

    def test_duration_is_computed_from_the_timestamps(self) -> None:
        rendered = fields(scan_result_embed(FakeScanResult()))
        assert rendered["Duration"] == "90.0s"

    def test_unfinished_run_reports_no_duration(self) -> None:
        rendered = fields(scan_result_embed(FakeScanResult(finished_at=None)))
        assert rendered["Duration"] == "n/a"

    def test_failure_is_visually_distinct(self) -> None:
        success = scan_result_embed(FakeScanResult())
        failure = scan_result_embed(FakeScanResult(status=RunStatus.FAILED, error="boom"))
        assert success.colour != failure.colour
        assert fields(failure)["Error"] == "boom"

    def test_rejection_reasons_are_ranked_by_count(self) -> None:
        rendered = fields(scan_result_embed(FakeScanResult()))
        reasons = rendered["Rejection reasons"]
        assert reasons.index("below_ccu") < reasons.index("too_many_visits")

    def test_source_notes_are_included_when_present(self) -> None:
        rendered = fields(scan_result_embed(FakeScanResult()))
        assert "partial" in rendered["Sources"]
        assert "3 pages" in rendered["Sources"]

    def test_optional_sections_are_omitted_when_empty(self) -> None:
        rendered = fields(
            scan_result_embed(FakeScanResult(rejection_reasons={}, source_stats={}, error=None))
        )
        assert "Rejection reasons" not in rendered
        assert "Sources" not in rendered
        assert "Error" not in rendered


class FakeStats:
    def __init__(self, **overrides: Any) -> None:
        defaults: dict[str, Any] = {
            "scans_completed": 120,
            "scans_24h": 4,
            "candidates_found": 60_000,
            "games_discovered": 300,
            "games_tracked": 1_200,
            "snapshots_written": 40_000,
            "http_requests": 90_000,
            "rate_limited": 3,
            "api_errors": 1,
            "average_scan_duration_ms": 8_500.0,
            "last_scan_at": NOW - timedelta(minutes=30),
            "alerts_by_level": {"HOT": 5, "EXTREME": 2, "WARM": 11},
            "alerts_suppressed": 9,
            "hot_leads": 4,
            "extreme_leads": 2,
            "warm_leads": 9,
            "leads_by_status": {"NEW": 7, "CONTACTED": 3},
        }
        defaults.update(overrides)
        for key, value in defaults.items():
            setattr(self, key, value)


class TestStatsEmbed:
    def test_telemetry_is_reported(self) -> None:
        rendered = fields(stats_embed(FakeStats()))
        assert rendered["Scans completed"] == "120"
        assert rendered["Games tracked"] == "1,200"
        assert rendered["Avg scan duration"] == "8.5s"
        # relative_time uses the real clock here, so only the shape is stable.
        assert rendered["Last scan"].endswith("ago")

    def test_alert_and_level_breakdowns(self) -> None:
        rendered = fields(stats_embed(FakeStats()))
        assert "HOT: 5" in rendered["Alerts"]
        assert "EXTREME: 2" in rendered["Alerts"]
        assert "🔥 HOT: 4" in rendered["Leads by level"]

    def test_zeroed_collections_do_not_break_rendering(self) -> None:
        rendered = fields(
            stats_embed(FakeStats(alerts_by_level={}, leads_by_status={}, last_scan_at=None))
        )
        assert "HOT: 0" in rendered["Alerts"]
        assert rendered["Pipeline"] == "no leads yet"
        assert rendered["Last scan"] == "unknown"

    def test_respects_discord_limits(self) -> None:
        embed = stats_embed(
            FakeStats(
                leads_by_status={f"STATUS_{index}": index for index in range(200)},
                alerts_by_level={f"LEVEL_{index}": index for index in range(100)},
            )
        )
        assert len(all_text(embed)) <= EMBED_TOTAL_LIMIT
        assert all(len(str(field.value)) <= EMBED_FIELD_VALUE_LIMIT for field in embed.fields)


class TestSimpleEmbeds:
    def test_error_embed_truncates_and_colours(self) -> None:
        embed = error_embed("Boom", "x" * 10_000)
        assert embed.title == "⚠️ Boom"
        assert embed.colour == COLOUR_ERROR
        assert embed.description is not None
        assert len(embed.description) == 4_000

    def test_help_embed_documents_every_command_group(self) -> None:
        rendered = fields(help_embed())
        assert set(rendered) == {"Discovery", "CRM", "Partners", "Control"}
        for command in (
            "/scan",
            "/search",
            "/hot",
            "/leads",
            "/lead",
            "/status",
            "/note",
            "/partners",
            "/match",
        ):
            assert any(command in value for value in rendered.values()), command

    def test_help_embed_states_the_manual_contact_rule(self) -> None:
        assert "manual" in (help_embed().description or "").lower()


class TestMentionHelpers:
    class _Mentionable:
        mention = "<@123>"

    def test_mention_user(self) -> None:
        assert mention_user(self._Mentionable()) == "<@123>"

    def test_missing_user_renders_nothing(self) -> None:
        assert mention_user(None) == ""

    def test_role_mention(self) -> None:
        assert role_mention(self._Mentionable()) == "<@123>"

    def test_missing_role_renders_nothing(self) -> None:
        assert role_mention(None) == ""


class TestStructuralInvariants:
    @pytest.mark.parametrize(
        "build",
        [
            lambda: lead_embed(game=FakeGame()),
            lambda: alert_embed({"game_name": "X"}),
            lambda: scan_result_embed(FakeScanResult()),
            lambda: stats_embed(FakeStats()),
            lambda: error_embed("t", "m"),
            lambda: help_embed(),
        ],
    )
    def test_field_count_and_footer(self, build: Any) -> None:
        embed = build()
        assert len(embed.fields) <= EMBED_FIELD_COUNT_LIMIT
        assert embed.footer.text, "every embed carries the disclaimer footer"

    def test_every_status_and_level_has_an_emoji(self) -> None:
        """A missing entry would raise KeyError inside a live Discord handler."""
        assert set(STATUS_EMOJI) == set(LeadStatus)
        assert set(LEVEL_EMOJI) == set(AlertLevel)
        assert set(LEVEL_COLOURS) == set(AlertLevel)
