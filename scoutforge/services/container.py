"""Application service container.

One place constructs (and lazily shares) the engine, HTTP client, discovery
sources, repositories and pure engines. Everything else receives what it needs
from here, which keeps commands, the worker and the dashboard testable: any
collaborator can be replaced with a stub.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from scoutforge.config.logging import get_logger
from scoutforge.config.settings import Settings, get_settings
from scoutforge.database.repositories import (
    AlertRepository,
    DiscoveryRunRepository,
    GameRepository,
    GameScoreRepository,
    GameSnapshotRepository,
    LeadNoteRepository,
    LeadPartnerRepository,
    LeadStatusHistoryRepository,
    PartnerMatchRepository,
    ScoutUserRepository,
    SettingsRepository,
    StatsRepository,
)
from scoutforge.database.session import create_engine, create_session_factory
from scoutforge.discovery.enrichment import GameEnricher
from scoutforge.discovery.registry import SourceBundle, build_sources
from scoutforge.monitoring.alerts import AlertEngine
from scoutforge.monitoring.snapshots import SnapshotReader, SnapshotRecorder
from scoutforge.net.client import PoliteHttpClient
from scoutforge.scoring.scorer import ScoutScorer
from scoutforge.services.partner_service import PartnerService

log = get_logger(__name__)


@dataclass(slots=True)
class Container:
    """Holds long-lived collaborators for the whole process."""

    settings: Settings = field(default_factory=get_settings)
    engine: AsyncEngine | None = None
    session_factory: async_sessionmaker[AsyncSession] | None = None
    http: PoliteHttpClient | None = None
    sources: SourceBundle | None = None
    enricher: GameEnricher | None = None
    scorer: ScoutScorer | None = None
    alert_engine: AlertEngine | None = None

    def __post_init__(self) -> None:
        if self.engine is None:
            self.engine = create_engine(self.settings.database)
        if self.session_factory is None:
            self.session_factory = create_session_factory(self.engine)
        if self.http is None:
            roblox = self.settings.roblox
            self.http = PoliteHttpClient(
                rate_limit_rps=roblox.rate_limit_rps,
                timeout_seconds=roblox.timeout_seconds,
                max_retries=roblox.max_retries,
                backoff_base_seconds=roblox.backoff_base_seconds,
                user_agent=roblox.user_agent,
            )
        if self.sources is None:
            self.sources = build_sources(
                self.http,
                discovery=self.settings.discovery,
                roblox=self.settings.roblox,
            )
        if self.enricher is None:
            self.enricher = GameEnricher(
                self.sources.api,
                http=self.http,
                concurrency=self.settings.worker.concurrency,
            )
        if self.scorer is None:
            self.scorer = ScoutScorer(self.settings.score, self.settings.alerts)
        if self.alert_engine is None:
            self.alert_engine = AlertEngine(self.settings.alerts)

    # ------------------------------------------------------------ factories
    def session(self) -> AsyncSession:
        """Create a new session. The caller owns its transaction."""
        assert self.session_factory is not None
        return self.session_factory()

    def snapshot_recorder(self, session: AsyncSession) -> SnapshotRecorder:
        return SnapshotRecorder(GameSnapshotRepository(session))

    def snapshot_reader(self, session: AsyncSession) -> SnapshotReader:
        return SnapshotReader(GameSnapshotRepository(session))

    # --------------------------------------------------------- repositories
    @property
    def games(self) -> type[GameRepository]:
        return GameRepository

    def game_repository(self, session: AsyncSession) -> GameRepository:
        return GameRepository(session)

    def snapshot_repository(self, session: AsyncSession) -> GameSnapshotRepository:
        return GameSnapshotRepository(session)

    def score_repository(self, session: AsyncSession) -> GameScoreRepository:
        return GameScoreRepository(session)

    def alert_repository(self, session: AsyncSession) -> AlertRepository:
        return AlertRepository(session)

    def note_repository(self, session: AsyncSession) -> LeadNoteRepository:
        return LeadNoteRepository(session)

    def status_history_repository(self, session: AsyncSession) -> LeadStatusHistoryRepository:
        return LeadStatusHistoryRepository(session)

    def scout_user_repository(self, session: AsyncSession) -> ScoutUserRepository:
        return ScoutUserRepository(session)

    def settings_repository(self, session: AsyncSession) -> SettingsRepository:
        return SettingsRepository(session)

    def run_repository(self, session: AsyncSession) -> DiscoveryRunRepository:
        return DiscoveryRunRepository(session)

    def stats_repository(self, session: AsyncSession) -> StatsRepository:
        return StatsRepository(session)

    def partner_match_repository(self, session: AsyncSession) -> PartnerMatchRepository:
        return PartnerMatchRepository(session)

    def lead_partner_repository(self, session: AsyncSession) -> LeadPartnerRepository:
        return LeadPartnerRepository(session)

    def partner_service(self, session: AsyncSession) -> PartnerService:
        return PartnerService(session, self.settings.partners)

    # -------------------------------------------------------------- helpers
    @property
    def api(self) -> Any:
        assert self.sources is not None
        return self.sources.api

    @property
    def http_stats(self) -> dict[str, Any]:
        assert self.http is not None
        return self.http.stats.snapshot()

    async def aclose(self) -> None:
        """Release network resources. The engine is disposed by the caller."""
        if self.sources is not None:
            await self.sources.aclose()
        if self.http is not None:
            await self.http.aclose()


__all__ = ["Container"]
