"""The discovery pipeline.

One scan, eleven steps, each observable:

1. gather candidates from every enabled source
2. deduplicate by ``universe_id``
3. enrich (batched detail fetches)
4. apply hard filters
5. upsert the game
6. write a snapshot
7. compute growth from real history
8. compute the Scout Score
9. rank the game against the partner profiles
10. classify the alert level
11. notify through the dispatcher when warranted

The pipeline is deliberately tolerant: a single bad source or a single failing
universe never aborts a scan. Everything it does is recorded in
``discovery_runs`` so ``/stats`` and the dashboard can show what happened.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from scoutforge.config.logging import get_logger, log_event
from scoutforge.database.repositories import (
    DiscoveryRunRepository,
    GameRepository,
    GameScoreRepository,
    GameSnapshotRepository,
    StatsRepository,
)
from scoutforge.database.session import transaction
from scoutforge.discovery.base import SeededDiscoverySource
from scoutforge.discovery.dedupe import Deduplicator
from scoutforge.discovery.filters import GameFilter, RejectionReason
from scoutforge.discovery.models import GameCandidate, GameDetails
from scoutforge.enums import LeadStatus, RunStatus
from scoutforge.monitoring.growth import compute_growth
from scoutforge.monitoring.snapshots import SnapshotRecorder
from scoutforge.scoring.rules import ScoreInput
from scoutforge.scoring.scorer import ScoutScore
from scoutforge.services.alert_service import AlertDispatcher, AlertService
from scoutforge.services.container import Container
from scoutforge.utils import utcnow

log = get_logger(__name__)

#: Only the most recent snapshots are needed for growth maths.
GROWTH_HISTORY_HOURS = 72

#: A RUNNING row older than this belongs to a process that is gone.
STALE_RUN_SECONDS = 3600

#: Discord embeds only have room for a handful of partners.
MAX_ALERT_PARTNERS = 3


@dataclass(slots=True)
class ScanResult:
    """Everything one scan produced."""

    run_id: int | None = None
    started_at: datetime = field(default_factory=utcnow)
    finished_at: datetime | None = None
    status: RunStatus = RunStatus.RUNNING
    candidates_found: int = 0
    candidates_processed: int = 0
    games_discovered: int = 0
    games_accepted: int = 0
    games_rejected: int = 0
    games_updated: int = 0
    snapshots_written: int = 0
    scores_computed: int = 0
    alerts_sent: int = 0
    alerts_suppressed: int = 0
    api_errors: int = 0
    rejection_reasons: dict[str, int] = field(default_factory=dict)
    source_stats: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    hot_universe_ids: list[int] = field(default_factory=list)
    partner_matches_written: int = 0

    @property
    def duration_seconds(self) -> float:
        if self.finished_at is None:
            return 0.0
        return (self.finished_at - self.started_at).total_seconds()

    def summary_line(self) -> str:
        return (
            f"candidates={self.candidates_found} accepted={self.games_accepted} "
            f"rejected={self.games_rejected} partner_matches={self.partner_matches_written}"
        )


class DiscoveryWorker:
    """Runs discovery scans. Callable from ``/scan`` or the scheduler."""

    def __init__(
        self,
        container: Container,
        *,
        dispatcher: AlertDispatcher | None = None,
        game_filter: GameFilter | None = None,
    ) -> None:
        self.container = container
        self.dispatcher = dispatcher
        self.game_filter = game_filter or GameFilter(container.settings.filters)
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ api
    async def run_once(
        self,
        *,
        triggered_by: str = "scheduler",
        limit: int | None = None,
    ) -> ScanResult:
        """Execute one full discovery scan.

        Two overlapping scans would double the API traffic and race on the same
        rows for no benefit, so an in-process lock is backed by a cross-process
        check: a ``discovery_runs`` row still marked RUNNING means another worker
        (or a previous crash) owns the scan right now.
        """
        if self._lock.locked():
            log_event(log, 25, "SCAN_SKIPPED", reason="already_running")
            return ScanResult(status=RunStatus.PARTIAL, error="scan already in progress")

        async with self._lock:
            if await self._another_process_is_running():
                log_event(log, 25, "SCAN_SKIPPED", reason="another_run_in_progress")
                return ScanResult(
                    status=RunStatus.PARTIAL, error="another discovery run is already in progress"
                )
            return await self._execute(triggered_by=triggered_by, limit=limit)

    async def _another_process_is_running(self) -> bool:
        """True when a different process left a run open.

        A run older than :data:`STALE_RUN_SECONDS` is stale by definition - the
        process that owned it is gone - so it is recovered rather than treated as
        a live competitor.
        """
        session = self.container.session()
        try:
            runs = DiscoveryRunRepository(session)
            async with transaction(session):
                if await runs.recover_stale(older_than_seconds=STALE_RUN_SECONDS):
                    return False
                return await runs.is_running()
        except Exception as exc:
            # Never block a scan because the guard query failed; the in-process
            # lock is still doing its job.
            log_event(log, 25, "RUN_GUARD_FAILED", error=type(exc).__name__)
            return False
        finally:
            await session.close()

    # ------------------------------------------------------------- internals
    async def _execute(self, *, triggered_by: str, limit: int | None) -> ScanResult:
        result = ScanResult()
        session = self.container.session()
        runs = DiscoveryRunRepository(session)
        run = None
        try:
            async with transaction(session):
                runs = DiscoveryRunRepository(session)
                stats = StatsRepository(session)
                run = await runs.start(triggered_by=triggered_by)
                result.run_id = run.id

                # -- steps 1-2: discover + dedupe ----------------------
                candidates, source_stats, api_errors = await self._discover(limit=limit)
                candidate_index = {c.universe_id: c for c in candidates}
                result.candidates_found = len(candidates)
                result.source_stats = source_stats
                result.api_errors += api_errors
                await stats.increment("candidates_processed", len(candidates))

                # -- step 3: enrich -------------------------------------
                details_map = await self._enrich(candidates, limit=limit)
                enricher = self.container.enricher
                if enricher is not None:
                    result.api_errors += enricher.errors
                result.candidates_processed = len(details_map)

                # -- steps 4-10 -----------------------------------------
                for details in details_map.values():
                    await self._process_details(
                        details=details,
                        candidate=candidate_index.get(details.universe_id),
                        result=result,
                        session=session,
                    )

                run.candidates_found = result.candidates_found
                run.candidates_processed = result.candidates_processed
                run.games_discovered = result.games_discovered
                run.games_accepted = result.games_accepted
                run.games_rejected = result.games_rejected
                run.games_updated = result.games_updated
                run.snapshots_written = result.snapshots_written
                run.scores_computed = result.scores_computed
                run.alerts_sent = result.alerts_sent
                run.alerts_suppressed = result.alerts_suppressed
                run.api_errors = result.api_errors
                run.rejection_reasons = result.rejection_reasons
                run.source_stats = result.source_stats

                status = RunStatus.PARTIAL if result.api_errors else RunStatus.SUCCESS
                result.status = status
                result.finished_at = utcnow()
                await runs.finish(run, status=status)

                await stats.increment("scans_completed")
                await stats.increment("games_discovered", result.games_discovered)

            log_event(
                log,
                20,
                "DISCOVERY",
                candidates=result.candidates_found,
                accepted=result.games_accepted,
                rejected=result.games_rejected,
                duration_s=round(result.duration_seconds, 2),
            )
            return result

        except Exception as exc:
            result.status = RunStatus.FAILED
            result.error = f"{type(exc).__name__}: {exc}"[:500]
            result.finished_at = utcnow()
            log_event(log, 40, "SCAN_FAILED", error=type(exc).__name__, detail=result.error)
            await self._record_failure(session, run, result)
            return result
        finally:
            await session.close()

    # ------------------------------------------------------------- pipeline
    async def _discover(
        self, *, limit: int | None
    ) -> tuple[list[GameCandidate], dict[str, Any], int]:
        """Steps 1-2: gather candidates from every source and deduplicate."""
        settings = self.container.settings
        sources = self.container.sources
        assert sources is not None
        per_source_limit = limit or settings.worker.max_games_per_scan

        seeds = await self._load_seeds()

        deduper = Deduplicator()
        source_stats: dict[str, Any] = {}
        errors = 0

        async def run_source(source: Any) -> tuple[str, dict[str, Any], int]:
            if isinstance(source, SeededDiscoverySource):
                source.set_seeds(seeds)
            try:
                batch = await source.discover_games(limit=per_source_limit)
            except Exception as exc:
                log_event(log, 30, "SOURCE_FAILED", source=source.name, error=type(exc).__name__)
                return source.name, {"candidates": 0, "pages": 0, "error": type(exc).__name__}, 1

            deduper.add_all(batch.candidates)
            return (
                source.name,
                {
                    "candidates": len(batch.candidates),
                    "pages": batch.pages_fetched,
                    "errors": batch.errors,
                    "notes": batch.notes,
                },
                batch.errors,
            )

        # Sources run concurrently; the shared HTTP client's rate limiter keeps the
        # aggregate request rate polite no matter how many sources are active.
        outcomes = await asyncio.gather(*(run_source(source) for source in sources.sources))
        for name, stats, error_count in outcomes:
            source_stats[name] = stats
            errors += error_count

        near_duplicates = deduper.near_duplicates()
        if near_duplicates:
            log_event(log, 20, "NEAR_DUPLICATES", groups=len(near_duplicates))

        candidates = [entry.primary for entry in deduper.results.values()]
        return candidates, source_stats, errors

    async def _load_seeds(self) -> list[int]:
        """Universes recommendation-style sources should expand from."""
        settings = self.container.settings
        session = self.container.session()
        try:
            async with transaction(session):
                return await GameRepository(session).active_universe_ids(
                    limit=max(20, settings.worker.concurrency * 4)
                )
        except Exception as exc:
            log_event(log, 25, "SEED_LOAD_FAILED", error=type(exc).__name__)
            return []
        finally:
            await session.close()

    async def _enrich(
        self, candidates: list[GameCandidate], *, limit: int | None
    ) -> dict[int, GameDetails]:
        """Step 3: batch-fetch full details for every deduplicated candidate."""
        settings = self.container.settings
        enricher = self.container.enricher
        assert enricher is not None

        budget = min(
            limit or settings.worker.max_games_per_scan, settings.worker.max_detail_fetches_per_scan
        )
        # Candidates that already hinted at a high CCU are enriched first so the
        # per-scan API budget is spent on the most promising games.
        ordered = sorted(
            candidates,
            key=lambda c: (c.current_ccu is None, -(c.current_ccu or 0), c.discovery_rank or 0),
        )
        universe_ids = [c.universe_id for c in ordered[:budget]]
        return await enricher.enrich(universe_ids)

    async def _process_details(
        self,
        *,
        details: GameDetails,
        candidate: GameCandidate | None,
        result: ScanResult,
        session: AsyncSession,
    ) -> None:
        """Steps 4-10 for a single game."""
        games = GameRepository(session)
        scores = GameScoreRepository(session)
        snapshots = GameSnapshotRepository(session)
        recorder = SnapshotRecorder(snapshots)
        alerts = AlertService(
            session,
            self.container.alert_engine,
            dispatcher=self.dispatcher,
            settings=self.container.settings.alerts,
        )

        universe_id = details.universe_id
        existing = await games.get(universe_id)
        history: dict[str, Any] = {}
        if existing is not None:
            history = {
                "first_seen_at": existing.first_seen_at,
                "last_ccu": existing.current_ccu,
                "last_seen_at": existing.last_seen_at,
            }

        verdict = self.game_filter.evaluate(details, history=history)

        if not verdict.passed:
            reason = verdict.reason or RejectionReason.UNIDENTIFIABLE
            result.games_rejected += 1
            result.rejection_reasons[reason.value] = (
                result.rejection_reasons.get(reason.value, 0) + 1
            )
            # Keep an already tracked row fresh, but never create one for a
            # rejected game.
            if existing is not None:
                await games.upsert(universe_id, {"missing_metrics": details.missing_metrics})
            return

        # -- step 5: persist --------------------------------------------
        source = candidate.source if candidate else "unknown"
        upsert = await games.upsert(universe_id, _game_payload(details, candidate), source=source)
        game = upsert.game
        if upsert.created:
            result.games_discovered += 1
        else:
            result.games_updated += 1
        result.games_accepted += 1

        await games.mark_lead(game, lead_min_score=self.container.settings.alerts.lead_min_score)

        # -- step 6: snapshot -------------------------------------------
        write = await recorder.record([details], source=source)
        result.snapshots_written += write.written

        # -- step 7: growth (from observed snapshots only) ---------------
        series = await snapshots.window(
            universe_id,
            since=utcnow() - timedelta(hours=GROWTH_HISTORY_HOURS),
            limit=500,
        )
        growth = compute_growth(universe_id, series, windows=(1, 6, 24))

        # -- step 8: score ----------------------------------------------
        scorer = self.container.scorer
        assert scorer is not None
        score: ScoutScore = scorer.score(
            ScoreInput(
                universe_id=universe_id,
                name=game.game_name,
                current_ccu=game.current_ccu,
                lifetime_visits=game.lifetime_visits,
                favorites=game.favorites,
                likes=game.likes,
                dislikes=game.dislikes,
                first_seen_at=game.first_seen_at,
                created_at=game.game_created_at,
                updated_at=game.game_updated_at,
                peak_ccu=game.peak_ccu,
                snapshot_count=len(series),
                growth=growth,
            )
        )
        row = score.to_row()
        row.update(
            growth_1h_pct=growth.growth_1h_pct,
            growth_6h_pct=growth.growth_6h_pct,
            growth_24h_pct=growth.growth_24h_pct,
            ccus_per_day=growth.ccus_per_day,
        )
        await scores.add(row)
        await games.set_score(game, score=score.score, level=score.level)
        result.scores_computed += 1

        # -- step 9: partner matching --------------------------------------
        # Runs after scoring because the matcher consumes the component values,
        # and before alerting so a HOT alert can name the partners it fits.
        partners_payload: list[dict[str, Any]] = []
        if self.container.settings.partners.enabled:
            partners = self.container.partner_service(session)
            try:
                matched = await partners.match_game(game=game, score=score, growth=growth)
                result.partner_matches_written += len(matched.matches)
                partners_payload = [
                    match.to_dict() for match in matched.eligible[:MAX_ALERT_PARTNERS]
                ]
            except Exception as exc:
                log_event(
                    log, 25, "PARTNER_MATCH_FAILED", universe=universe_id, error=type(exc).__name__
                )

        # -- steps 10-11: alert -------------------------------------------
        if not game.is_lead:
            return
        if LeadStatus(game.status) in {LeadStatus.REJECTED, LeadStatus.NOT_INTERESTED}:
            return
        outcome = await alerts.process(
            game=game, score=score, growth=growth, partners=partners_payload
        )
        if outcome.delivered:
            result.alerts_sent += 1
            result.hot_universe_ids.append(universe_id)
        elif outcome.decision.level.rank >= 2:
            result.alerts_suppressed += 1

    async def _record_failure(self, session: AsyncSession, run: Any, result: ScanResult) -> None:
        """Best-effort persistence of a failed run."""
        if run is None or run.id is None:
            return
        try:
            async with transaction(session):
                runs = DiscoveryRunRepository(session)
                stored = await session.get(type(run), run.id)
                if stored is None:
                    return
                stored.candidates_found = result.candidates_found
                stored.candidates_processed = result.candidates_processed
                stored.games_discovered = result.games_discovered
                stored.games_accepted = result.games_accepted
                stored.games_rejected = result.games_rejected
                stored.snapshots_written = result.snapshots_written
                stored.scores_computed = result.scores_computed
                stored.alerts_sent = result.alerts_sent
                stored.alerts_suppressed = result.alerts_suppressed
                stored.api_errors = result.api_errors
                stored.rejection_reasons = result.rejection_reasons
                await runs.finish(stored, status=RunStatus.FAILED, error=result.error)
        except Exception:
            log_event(log, 40, "SCAN_FAILURE_RECORD_FAILED", run_id=run.id)


def _game_payload(details: GameDetails, candidate: GameCandidate | None) -> dict[str, Any]:
    """Map enriched details onto ``games`` columns."""
    creator = details.creator
    return {
        "place_id": details.place_id or details.root_place_id,
        "game_name": details.name or (candidate.name if candidate else None) or "Unknown game",
        "description": details.description,
        "creator_id": creator.creator_id,
        "creator_name": creator.creator_name,
        "creator_type": creator.creator_type,
        "group_id": creator.group_id,
        "group_name": creator.group_name,
        "creator_url": creator.creator_url,
        "current_ccu": details.current_ccu,
        "lifetime_visits": details.lifetime_visits,
        "favorites": details.favorites,
        "likes": details.likes,
        "dislikes": details.dislikes,
        "playing_now": details.playing_now,
        "voice_chat_visits": details.voice_chat_visits,
        "thumbnail_url": details.thumbnail_url,
        "public_discord_url": details.public_discord_url,
        "game_created_at": details.created_at,
        "game_updated_at": details.updated_at,
        "missing_metrics": details.missing_metrics,
    }


__all__ = [
    "GROWTH_HISTORY_HOURS",
    "MAX_ALERT_PARTNERS",
    "STALE_RUN_SECONDS",
    "DiscoveryWorker",
    "ScanResult",
]
