"""Typed, environment driven application settings.

Every tunable number used by ScoutForge lives here - nothing is hard-coded in
business logic. Values may be overridden through environment variables (or a
``.env`` file) and, at runtime, a subset may be overridden from the ``settings``
database table by :class:`scoutforge.services.settings_service.SettingsService`.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from scoutforge.partners.keywords import DEFAULT_RISK_KEYWORDS

Environment = Literal["development", "staging", "production"]

#: pydantic-settings runs ``json.loads`` on env values for list/dict fields *before*
#: validators see them, which turns the documented comma separated form
#: (``ENABLED_SOURCES=roblox_explore,roblox_search``) into a hard ``SettingsError``
#: at startup. ``NoDecode`` hands the raw string to our own parser instead, so one
#: function owns every accepted syntax.
CsvStrList = Annotated[list[str], NoDecode]
CsvIntList = Annotated[list[int], NoDecode]

_QUOTES = ('"', "'")


def parse_csv_list(value: str) -> list[str]:
    """Parse a comma separated env value into a clean list of items.

    Accepts ``a,b`` and also the JSON-ish ``["a","b"]`` form people reach for out
    of habit, so a well-meant typo degrades to the same list instead of silently
    producing names that match nothing.
    """
    text = value.strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    items: list[str] = []
    for part in text.split(","):
        item = part.strip()
        if len(item) >= 2 and item[0] == item[-1] and item[0] in _QUOTES:
            item = item[1:-1].strip()
        if item:
            items.append(item)
    return items


def parse_int_list(value: str) -> list[int]:
    """Parse a comma separated env value into ints (Discord id lists)."""
    out: list[int] = []
    for item in parse_csv_list(value):
        try:
            out.append(int(item))
        except ValueError:
            raise ValueError(f"expected a whole number, got {item!r}") from None
    return out


class _Base(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )


class DatabaseSettings(_Base):
    """MySQL connection and pooling options.

    Production runs MySQL 8.x through the pure-Python ``asyncmy`` driver.
    ``+aiosqlite`` is accepted as well so the test-suite and a laptop scratch
    database work without a server.
    """

    database_url: str = Field(
        default="mysql+asyncmy://scoutforge:scoutforge@localhost:3306/scoutforge?charset=utf8mb4",
        validation_alias="DATABASE_URL",
    )
    pool_size: int = Field(default=10, ge=1, le=100, validation_alias="DB_POOL_SIZE")
    max_overflow: int = Field(default=10, ge=0, le=100, validation_alias="DB_MAX_OVERFLOW")
    echo: bool = Field(default=False, validation_alias="DB_ECHO")
    pool_timeout_seconds: float = Field(default=30.0, gt=0)
    pool_recycle_seconds: int = Field(default=1800, gt=0)
    connect_retries: int = Field(default=5, ge=0, le=20)
    isolation_level: str = Field(
        default="READ COMMITTED",
        validation_alias="DB_ISOLATION_LEVEL",
        description=(
            "Transaction isolation requested on every connection. READ COMMITTED "
            "keeps long discovery scans from holding read locks in InnoDB."
        ),
    )

    @field_validator("database_url")
    @classmethod
    def _require_async_driver(cls, value: str) -> str:
        if "+asyncmy" not in value and "+aiosqlite" not in value:
            raise ValueError(
                "DATABASE_URL must use an async driver: 'mysql+asyncmy://...' "
                "(or '+aiosqlite://' for tests and local scratch databases)"
            )
        return value

    @field_validator("isolation_level")
    @classmethod
    def _normalise_isolation(cls, value: str) -> str:
        allowed = {"READ COMMITTED", "REPEATABLE READ", "READ UNCOMMITTED", "SERIALIZABLE"}
        text = value.strip().upper()
        if text not in allowed:
            raise ValueError(f"DB_ISOLATION_LEVEL must be one of {sorted(allowed)}")
        return text

    @property
    def is_sqlite(self) -> bool:
        """True for the throwaway test/scratch database."""
        return "+aiosqlite" in self.database_url


class DiscordSettings(_Base):
    """Discord application credentials and routing."""

    token: SecretStr = Field(default=SecretStr(""), validation_alias="DISCORD_TOKEN")
    guild_id: int | None = Field(default=None, validation_alias="DISCORD_GUILD_ID")
    alert_channel_id: int | None = Field(default=None, validation_alias="ALERT_CHANNEL_ID")
    alert_role_id: int | None = Field(default=None, validation_alias="ALERT_ROLE_ID")
    alert_mention_user_ids: CsvIntList = Field(
        default_factory=list, validation_alias="ALERT_MENTION_USER_IDS"
    )
    admin_role_ids: CsvIntList = Field(default_factory=list, validation_alias="ADMIN_ROLE_IDS")
    command_global: bool = Field(default=False, validation_alias="DISCORD_COMMAND_GLOBAL")
    embed_color: int = Field(default=0x5865F2)
    alert_embed_color: int = Field(default=0xED4245)

    @field_validator("alert_mention_user_ids", "admin_role_ids", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return parse_int_list(value.replace(" ", ""))
        return value

    @field_validator("guild_id", "alert_channel_id", "alert_role_id", mode="before")
    @classmethod
    def _blank_to_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        if isinstance(value, str) and value.strip().lower() in {"none", "null"}:
            return None
        return value

    @property
    def token_value(self) -> str:
        """Plain token string. Never log this value."""
        return self.token.get_secret_value()


class FilterSettings(_Base):
    """Hard gating rules applied to every candidate."""

    min_ccu: int = Field(default=1000, ge=0, validation_alias="MIN_CCU")
    max_lifetime_visits: int = Field(
        default=0,
        ge=0,
        validation_alias="MAX_LIFETIME_VISITS",
        description=(
            "Optional ceiling on lifetime visits. 0 (the default) disables the gate "
            "entirely: an established game with millions of visits can still be a "
            "good partner fit, and the Scout Score already rewards healthy CCU."
        ),
    )
    abandoned_ccu_threshold: int = Field(
        default=50, ge=0, validation_alias="ABANDONED_CCU_THRESHOLD"
    )
    abandoned_after_days: int = Field(default=30, ge=0, validation_alias="ABANDONED_AFTER_DAYS")
    require_universe_id: bool = True
    require_creator_id: bool = True


class ScoreWeights(_Base):
    """Component weights for the transparent 0-100 Scout Score."""

    ccu: int = Field(default=16, ge=0, validation_alias="SCORE_WEIGHT_CCU")
    ratio: int = Field(default=22, ge=0, validation_alias="SCORE_WEIGHT_RATIO")
    growth: int = Field(default=16, ge=0, validation_alias="SCORE_WEIGHT_GROWTH")
    freshness: int = Field(default=8, ge=0, validation_alias="SCORE_WEIGHT_FRESHNESS")
    consistency: int = Field(default=10, ge=0, validation_alias="SCORE_WEIGHT_CONSISTENCY")
    footprint: int = Field(default=6, ge=0, validation_alias="SCORE_WEIGHT_FOOTPRINT")
    engagement: int = Field(default=8, ge=0, validation_alias="SCORE_WEIGHT_ENGAGEMENT")
    monetization: int = Field(default=8, ge=0, validation_alias="SCORE_WEIGHT_MONETIZATION")
    originality: int = Field(default=6, ge=0, validation_alias="SCORE_WEIGHT_ORIGINALITY")

    ccu_reference: float = Field(default=10_000.0, gt=0, validation_alias="SCORE_CCU_REFERENCE")
    ratio_reference: float = Field(default=0.02, gt=0, validation_alias="SCORE_RATIO_REFERENCE")
    growth_reference: float = Field(default=1.0, gt=0, validation_alias="SCORE_GROWTH_REFERENCE")
    freshness_days: int = Field(default=30, ge=1, validation_alias="SCORE_FRESHNESS_DAYS")
    consistency_window_hours: int = Field(
        default=24, ge=1, validation_alias="SCORE_CONSISTENCY_WINDOW_HOURS"
    )
    monetization_visits_reference: float = Field(
        default=500_000.0, gt=0, validation_alias="SCORE_MONETIZATION_VISITS_REFERENCE"
    )

    @model_validator(mode="after")
    def _validate_total(self) -> ScoreWeights:
        total = (
            self.ccu
            + self.ratio
            + self.growth
            + self.freshness
            + self.consistency
            + self.footprint
            + self.engagement
            + self.monetization
            + self.originality
        )
        if total != 100:
            raise ValueError(f"SCORE_WEIGHT_* values must sum to 100, got {total}")
        return self


class PartnerSettings(_Base):
    """Tuning for the configurable partner-matching engine.

    The six built-in profiles live in :mod:`scoutforge.partners.profiles`. They can
    be replaced wholesale with ``PARTNER_PROFILES_FILE`` (a JSON document) and
    otherwise overridden at runtime through the ``partner_profiles`` database
    setting key, which is what the dashboard settings page edits.
    """

    enabled: bool = Field(default=True, validation_alias="PARTNER_MATCHING_ENABLED")
    profiles_file: str = Field(default="", validation_alias="PARTNER_PROFILES_FILE")
    min_score: float = Field(
        default=35.0,
        ge=0,
        le=100,
        validation_alias="PARTNER_MIN_SCORE",
        description="Compatibility score below which a partner is not reported.",
    )
    max_matches_per_game: int = Field(
        default=6, ge=1, le=50, validation_alias="PARTNER_MAX_MATCHES_PER_GAME"
    )
    min_game_score: float = Field(
        default=0.0,
        ge=0,
        le=100,
        validation_alias="PARTNER_MIN_GAME_SCORE",
        description="Only match partners against games scoring at least this much.",
    )
    risk_keywords: CsvStrList = Field(
        default_factory=lambda: list(DEFAULT_RISK_KEYWORDS),
        validation_alias="PARTNER_RISK_KEYWORDS",
    )

    @field_validator("risk_keywords", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return [part.lower() for part in parse_csv_list(value)]
        return value


class AlertSettings(_Base):
    """Alert level thresholds and notification cooldowns."""

    enabled: bool = Field(default=True, validation_alias="ALERTS_ENABLED")
    warm_score: float = Field(default=75.0, ge=0, le=100, validation_alias="WARM_SCORE")
    hot_score: float = Field(default=90.0, ge=0, le=100, validation_alias="HOT_SCORE")
    extreme_score: float = Field(default=95.0, ge=0, le=100, validation_alias="EXTREME_SCORE")
    score_jump_threshold: float = Field(
        default=5.0, ge=0, validation_alias="ALERT_SCORE_JUMP_THRESHOLD"
    )
    ccu_spike_pct: float = Field(default=50.0, ge=0, validation_alias="ALERT_CCU_SPIKE_PCT")
    hot_cooldown_seconds: int = Field(
        default=21_600, ge=0, validation_alias="HOT_ALERT_COOLDOWN_SECONDS"
    )
    extreme_cooldown_seconds: int = Field(
        default=10_800, ge=0, validation_alias="EXTREME_ALERT_COOLDOWN_SECONDS"
    )
    warm_cooldown_seconds: int = Field(
        default=86_400, ge=0, validation_alias="WARM_ALERT_COOLDOWN_SECONDS"
    )
    lead_min_score: float = Field(default=0.0, ge=0, le=100, validation_alias="LEAD_MIN_SCORE")

    @model_validator(mode="after")
    def _validate_thresholds(self) -> AlertSettings:
        if not (self.warm_score <= self.hot_score <= self.extreme_score):
            raise ValueError("Expected WARM_SCORE <= HOT_SCORE <= EXTREME_SCORE")
        return self


class WorkerSettings(_Base):
    """Discovery worker scheduling and throughput limits."""

    enabled: bool = Field(default=True, validation_alias="WORKER_ENABLED")
    scan_interval_seconds: int = Field(default=600, ge=15, validation_alias="SCAN_INTERVAL_SECONDS")
    concurrency: int = Field(default=4, ge=1, le=32, validation_alias="SCAN_CONCURRENCY")
    max_games_per_scan: int = Field(default=400, ge=1, validation_alias="MAX_GAMES_PER_SCAN")
    max_detail_fetches_per_scan: int = Field(
        default=120, ge=1, validation_alias="MAX_DETAIL_FETCHES_PER_SCAN"
    )
    snapshot_retention_days: int = Field(
        default=180, ge=1, validation_alias="SNAPSHOT_RETENTION_DAYS"
    )
    run_on_startup: bool = True


class RobloxApiSettings(_Base):
    """Polite HTTP client behaviour for Roblox public APIs."""

    api_base: str = Field(default="https://apis.roblox.com", validation_alias="ROBLOX_API_BASE")
    search_base: str = Field(
        default="https://apis.roblox.com", validation_alias="ROBLOX_SEARCH_BASE"
    )
    rate_limit_rps: float = Field(default=8.0, gt=0, validation_alias="ROBLOX_RATE_LIMIT_RPS")
    timeout_seconds: float = Field(default=15.0, gt=0, validation_alias="ROBLOX_TIMEOUT_SECONDS")
    max_retries: int = Field(default=3, ge=0, le=10, validation_alias="ROBLOX_MAX_RETRIES")
    backoff_base_seconds: float = Field(
        default=0.75, gt=0, validation_alias="ROBLOX_BACKOFF_BASE_SECONDS"
    )
    user_agent: str = "ScoutForge/1.0 (+https://github.com/scoutforge; public-data-research)"


class DiscoverySettings(_Base):
    """Which discovery sources run, and how much they fetch."""

    enabled_sources: CsvStrList = Field(
        default_factory=lambda: ["roblox_explore", "roblox_search"],
        validation_alias="ENABLED_SOURCES",
    )
    explore_pages: int = Field(default=3, ge=1, le=50, validation_alias="ROBLOX_EXPLORE_PAGES")
    explore_sort: str = Field(default="DescPlayers", validation_alias="ROBLOX_EXPLORE_SORT")
    explore_page_size: int = Field(default=50, ge=1, le=100)
    search_terms: CsvStrList = Field(
        default_factory=lambda: [
            "trending",
            "new",
            "popular",
            "viral",
            "roleplay",
            "simulator",
        ],
        validation_alias="ROBLOX_SEARCH_TERMS",
    )
    search_terms_per_scan: int = Field(default=4, ge=0, le=50)
    sort_orders: CsvStrList = Field(
        default_factory=lambda: ["Desc", "Asc"], validation_alias="ROBLOX_EXPLORE_SORT_ORDERS"
    )

    @field_validator("enabled_sources", "search_terms", "sort_orders", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return parse_csv_list(value)
        return value


class Settings(_Base):
    """Root settings object combining every subsystem configuration."""

    environment: Environment = Field(default="production", validation_alias="ENVIRONMENT")
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")
    log_json: bool = Field(default=True, validation_alias="LOG_JSON")
    timezone: str = Field(default="UTC", validation_alias="TIMEZONE")

    discord: DiscordSettings = Field(default_factory=DiscordSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    filters: FilterSettings = Field(default_factory=FilterSettings)
    alerts: AlertSettings = Field(default_factory=AlertSettings)
    worker: WorkerSettings = Field(default_factory=WorkerSettings)
    roblox: RobloxApiSettings = Field(default_factory=RobloxApiSettings)
    discovery: DiscoverySettings = Field(default_factory=DiscoverySettings)
    score: ScoreWeights = Field(default_factory=ScoreWeights)
    partners: PartnerSettings = Field(default_factory=PartnerSettings)

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()


def reload_settings() -> Settings:
    """Clear the settings cache and re-read the environment (used by tests)."""
    get_settings.cache_clear()
    return get_settings()
