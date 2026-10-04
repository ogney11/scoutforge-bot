"""Configuration parsing and the ``--check`` validation gate.

Two failure modes are covered here: a list-valued env var written in JSON syntax
(``ENABLED_SOURCES=["roblox_explore"]``) which used to parse into names that
matched nothing, and a typo in ``ENABLED_SOURCES`` which used to start a worker
that silently discovered nothing.
"""

from __future__ import annotations

import pytest

from scoutforge.__main__ import _validate
from scoutforge.config.settings import (
    AlertSettings,
    DatabaseSettings,
    DiscordSettings,
    DiscoverySettings,
    PartnerSettings,
    Settings,
    parse_csv_list,
    parse_int_list,
)


class TestParseCsvList:
    def test_plain_csv(self) -> None:
        assert parse_csv_list("roblox_explore,roblox_search") == [
            "roblox_explore",
            "roblox_search",
        ]

    def test_whitespace_and_empty_items(self) -> None:
        assert parse_csv_list(" a , ,b ") == ["a", "b"]

    def test_json_style_list(self) -> None:
        assert parse_csv_list('["roblox_explore", "roblox_search"]') == [
            "roblox_explore",
            "roblox_search",
        ]

    def test_single_quoted_items(self) -> None:
        assert parse_csv_list("'trending', 'new'") == ["trending", "new"]

    def test_empty_string(self) -> None:
        assert parse_csv_list("") == []


class TestParseIntList:
    def test_plain_csv(self) -> None:
        assert parse_int_list("111,222") == [111, 222]

    def test_json_style_list(self) -> None:
        assert parse_int_list("[111, 222]") == [111, 222]

    def test_empty_string(self) -> None:
        assert parse_int_list("") == []

    def test_non_numeric_is_reported(self) -> None:
        with pytest.raises(ValueError, match="whole number"):
            parse_int_list("111,everyone")


class TestEnvListFieldsAcceptBothSyntaxes:
    """Regression: every list field used to raise SettingsError on plain CSV.

    Newer pydantic-settings runs ``json.loads`` on env values for list fields
    before validators run, so the comma separated form documented in
    ``.env.example`` could not even be loaded.
    """

    @pytest.mark.parametrize(
        "raw", ["roblox_explore,roblox_search", '["roblox_explore", "roblox_search"]']
    )
    def test_settings_loads_from_env_with_either_syntax(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        monkeypatch.setenv("ENABLED_SOURCES", raw)
        monkeypatch.setenv("ROBLOX_SEARCH_TERMS", "trending,new")
        monkeypatch.setenv("ROBLOX_EXPLORE_SORT_ORDERS", "Desc,Asc")
        monkeypatch.setenv("PARTNER_RISK_KEYWORDS", "Scam,UGC")
        monkeypatch.setenv("ALERT_MENTION_USER_IDS", "111,222")
        monkeypatch.setenv("ADMIN_ROLE_IDS", "333,444")
        monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

        settings = Settings(_env_file=None)

        assert settings.discovery.enabled_sources == ["roblox_explore", "roblox_search"]
        assert settings.discovery.search_terms == ["trending", "new"]
        assert settings.discovery.sort_orders == ["Desc", "Asc"]
        assert settings.partners.risk_keywords == ["scam", "ugc"]
        assert settings.discord.alert_mention_user_ids == [111, 222]
        assert settings.discord.admin_role_ids == [333, 444]

    def test_json_syntax_works_for_id_lists(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ADMIN_ROLE_IDS", "[333, 444]")
        monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

        assert Settings(_env_file=None).discord.admin_role_ids == [333, 444]


class TestDiscoverySettingsParsing:
    def test_enabled_sources_from_env_string(self) -> None:
        assert DiscoverySettings(
            enabled_sources="roblox_explore,roblox_search"
        ).enabled_sources == [
            "roblox_explore",
            "roblox_search",
        ]

    def test_enabled_sources_accepts_json_style_value(self) -> None:
        settings = DiscoverySettings(enabled_sources='["roblox_explore","roblox_search"]')

        assert settings.enabled_sources == ["roblox_explore", "roblox_search"]


def _settings(**overrides) -> Settings:
    base = {
        "discord": DiscordSettings(token="token", alert_channel_id=1),
        "database": DatabaseSettings(database_url="mysql+asyncmy://u:p@h/scoutforge"),
        "alerts": AlertSettings(channel_id=1),
    }
    base.update(overrides)
    return Settings(**base)


class TestValidateEnabledSources:
    def test_healthy_config_has_no_problems(self) -> None:
        settings = _settings(discovery=DiscoverySettings(enabled_sources="roblox_explore"))

        assert _validate(settings) == []

    def test_empty_sources_is_reported(self) -> None:
        problems = _validate(_settings(discovery=DiscoverySettings(enabled_sources="")))

        assert any("ENABLED_SOURCES is empty" in p for p in problems)

    def test_unknown_source_is_reported_with_the_valid_names(self) -> None:
        problems = _validate(
            _settings(discovery=DiscoverySettings(enabled_sources="roblox_explore,roblox_typo"))
        )

        assert len(problems) == 1
        assert "roblox_typo" in problems[0]
        assert "roblox_explore, roblox_search, roblox_similar" in problems[0]


class TestPartnerSettingsParsing:
    def test_risk_keywords_from_env_string(self) -> None:
        assert PartnerSettings(risk_keywords="Scam, UGC").risk_keywords == ["scam", "ugc"]

    def test_risk_keywords_accept_json_style_value(self) -> None:
        settings = PartnerSettings(risk_keywords='["Scam", "UGC"]')

        assert settings.risk_keywords == ["scam", "ugc"]
