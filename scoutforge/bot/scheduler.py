"""Async scheduler for the discovery worker.

Uses APScheduler's ``AsyncIOScheduler`` in interval mode with
``coalesce=True`` and ``max_instances=1`` so a slow scan can never queue up
behind itself - a backlog of scans would only increase pressure on the Roblox
APIs.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import timedelta
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from scoutforge.config.logging import get_logger, log_event
from scoutforge.config.settings import WorkerSettings
from scoutforge.services.discovery_service import DiscoveryWorker

log = get_logger(__name__)

JOB_ID = "scoutforge_discovery_scan"


class ScanScheduler:
    """Owns the background scan loop."""

    def __init__(
        self,
        worker: DiscoveryWorker,
        settings: WorkerSettings,
        *,
        scheduler: AsyncIOScheduler | None = None,
    ) -> None:
        self.worker = worker
        self.settings = settings
        self.scheduler = scheduler or AsyncIOScheduler(timezone="UTC")
        self._owns_scheduler = scheduler is None
        self._task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------ api
    def start(self, *, run_on_startup: bool | None = None) -> None:
        """Schedule the recurring scan job."""
        if not self.settings.enabled:
            log_event(log, 25, "SCHEDULER_DISABLED", reason="WORKER_ENABLED=false")
            return

        if not self.scheduler.running:
            self.scheduler.start()

        self.scheduler.add_job(
            self._run_job,
            trigger=IntervalTrigger(seconds=self.settings.scan_interval_seconds),
            id=JOB_ID,
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=self.settings.scan_interval_seconds,
            next_run_time=None,
        )
        log_event(
            log,
            20,
            "SCHEDULER_STARTED",
            interval_s=self.settings.scan_interval_seconds,
            run_on_startup=(
                self.settings.run_on_startup if run_on_startup is None else run_on_startup
            ),
        )

        if run_on_startup if run_on_startup is not None else self.settings.run_on_startup:
            self._task = asyncio.create_task(self._run_once_delayed(), name="scoutforge-first-scan")

    async def _run_once_delayed(self) -> None:
        """Run the first scan shortly after startup so Discord is connected first."""
        await asyncio.sleep(5)
        await self.run_now(triggered_by="startup")

    async def run_now(self, *, triggered_by: str = "manual") -> Any:
        """Run a scan immediately, outside the schedule."""
        return await self.worker.run_once(triggered_by=triggered_by)

    async def _run_job(self) -> None:
        try:
            result = await self.worker.run_once(triggered_by="scheduler")
            log_event(
                log,
                20,
                "SCHEDULED_SCAN",
                status=result.status.value,
                accepted=result.games_accepted,
                alerts=result.alerts_sent,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log_event(log, 40, "SCHEDULED_SCAN_FAILED", error=type(exc).__name__)

    def reschedule(self, interval_seconds: int) -> None:
        """Change the scan interval at runtime."""
        self.settings.scan_interval_seconds = interval_seconds
        if not self.scheduler.get_job(JOB_ID):
            return
        self.scheduler.reschedule_job(JOB_ID, trigger=IntervalTrigger(seconds=interval_seconds))
        log_event(log, 20, "SCHEDULER_RESCHEDULED", interval_s=interval_seconds)

    def pause(self) -> None:
        if self.scheduler.get_job(JOB_ID):
            self.scheduler.pause_job(JOB_ID)
            log_event(log, 20, "SCHEDULER_PAUSED")

    def resume(self) -> None:
        if self.scheduler.get_job(JOB_ID):
            self.scheduler.resume_job(JOB_ID)
            log_event(log, 20, "SCHEDULER_RESUMED")

    async def shutdown(self, *, wait: bool = True) -> None:
        """Stop the scheduler cleanly."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        if self._owns_scheduler and self.scheduler.running:
            self.scheduler.shutdown(wait=wait)
        log_event(log, 20, "SCHEDULER_STOPPED")

    def next_run_time(self) -> Any:
        job = self.scheduler.get_job(JOB_ID)
        return job.next_run_time if job else None

    def describe(self) -> str:
        job = self.scheduler.get_job(JOB_ID)
        if job is None:
            return "not scheduled"
        return f"every {timedelta(seconds=self.settings.scan_interval_seconds)}"


__all__ = ["JOB_ID", "ScanScheduler"]
