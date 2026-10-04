"""The ScoutForge Discord client.

Responsibilities kept deliberately small:

* connect to Discord and register slash commands
* own the service container, discovery worker and scheduler
* resolve the alert channel/role lazily and rebuild the dispatcher when needed
* shut everything down cleanly on SIGINT/SIGTERM

All scouting logic lives in :mod:`scoutforge.services`; this module only wires it
to the platform.
"""

from __future__ import annotations

import asyncio
import signal

import discord
from discord.ext import commands

from scoutforge.bot.alerts import DiscordAlertDispatcher
from scoutforge.bot.commands import (
    crm_cmds,
    discovery_cmds,
    lead_cmds,
    ops_cmds,
    partner_cmds,
)
from scoutforge.bot.events import register_events
from scoutforge.bot.permissions import register_commands
from scoutforge.bot.scheduler import ScanScheduler
from scoutforge.config.logging import get_logger, log_event, setup_logging
from scoutforge.config.settings import Settings, get_settings
from scoutforge.database.session import connect_with_retry
from scoutforge.services.container import Container
from scoutforge.services.discovery_service import DiscoveryWorker

log = get_logger(__name__)

INTENTS = discord.Intents.default()
INTENTS.message_content = False  # the bot never reads message content
INTENTS.members = False


class ScoutForgeBot(commands.Bot):
    """discord.py client with the ScoutForge container attached."""

    def __init__(
        self, settings: Settings | None = None, *, container: Container | None = None
    ) -> None:
        self.settings = settings or get_settings()
        super().__init__(command_prefix="!", intents=INTENTS, help_command=None)
        self.container = container or Container(settings=self.settings)
        self.worker = DiscoveryWorker(self.container)
        self.scheduler = ScanScheduler(self.worker, self.settings.worker)
        self.dispatcher: DiscordAlertDispatcher | None = None
        self._dispatcher_channel_id: int | None = None
        self._closing = False

    # ------------------------------------------------------------- start-up
    async def setup_hook(self) -> None:
        """Runs once the gateway connection is established."""
        await super().setup_hook()

        engine = self.container.engine
        if engine is not None:
            await connect_with_retry(engine, retries=self.settings.database.connect_retries)

        await self._recover_and_bootstrap()
        self._register_commands()
        self.worker.dispatcher = await self._build_dispatcher()
        self.scheduler.start()

        log_event(
            log,
            20,
            "BOT_READY",
            user=str(self.user),
            guilds=len(self.guilds),
            sources=self.container.sources.names() if self.container.sources else [],
            scan_interval=self.settings.worker.scan_interval_seconds,
        )

    async def _recover_and_bootstrap(self) -> None:
        """Reset stale runs and make sure the schema exists on first boot."""
        from scoutforge.database.repositories import DiscoveryRunRepository, StatsRepository
        from scoutforge.database.session import transaction

        session = self.container.session()
        try:
            async with transaction(session):
                recovered = await DiscoveryRunRepository(session).recover_stale()
                if recovered:
                    log_event(log, 25, "RECOVERED_RUNS", count=recovered)
                http = self.container.http
                if http is not None:
                    stats = http.stats.snapshot()
                    stats_repo = StatsRepository(session)
                    for key, value in (
                        ("http_requests", stats["requests"]),
                        ("http_retries", stats["retries"]),
                        ("http_rate_limited", stats["rate_limited"]),
                        ("http_errors", stats["errors"]),
                    ):
                        await stats_repo.increment(key, float(value))
        except Exception as exc:
            log_event(log, 30, "BOOTSTRAP_FAILED", error=type(exc).__name__)
        finally:
            await session.close()

    def _register_commands(self) -> None:
        discovery_cmds.register(self)
        lead_cmds.register(self)
        crm_cmds.register(self)
        ops_cmds.register(self)
        partner_cmds.register(self)
        register_events(self)

    async def on_ready(self) -> None:
        log_event(log, 20, "CONNECTED", user=str(self.user), guilds=len(self.guilds))

    async def on_resumed(self) -> None:
        log_event(log, 20, "RESUMED")

    async def on_disconnect(self) -> None:
        log_event(log, 25, "DISCONNECTED")

    # ------------------------------------------------------------ dispatch
    async def _build_dispatcher(self) -> DiscordAlertDispatcher | None:
        """Resolve the alert channel + role into a dispatcher."""
        discord_settings = self.settings.discord
        channel = None
        if discord_settings.alert_channel_id:
            channel = await self._resolve_channel(discord_settings.alert_channel_id)
        role = None
        if discord_settings.alert_role_id:
            role = await self._resolve_role(discord_settings.alert_role_id)
        self._dispatcher_channel_id = (
            discord_settings.alert_channel_id if channel is not None else None
        )
        if channel is None:
            log_event(
                log, 25, "ALERT_CHANNEL_MISSING", channel_id=discord_settings.alert_channel_id
            )
        return DiscordAlertDispatcher(
            channel,
            role=role,
            mention_user_ids=tuple(discord_settings.alert_mention_user_ids),
        )

    async def _resolve_channel(self, channel_id: int) -> discord.abc.Messageable | None:
        cached = self.get_channel(channel_id)
        if isinstance(cached, discord.abc.Messageable):
            return cached
        try:
            fetched = await self.fetch_channel(channel_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
            log_event(
                log,
                30,
                "ALERT_CHANNEL_FETCH_FAILED",
                channel_id=channel_id,
                error=type(exc).__name__,
            )
            return None
        return fetched if isinstance(fetched, discord.abc.Messageable) else None

    async def _resolve_role(self, role_id: int) -> discord.Role | None:
        """Find a role by id across the cached guilds, fetching it if needed."""
        for guild in self.guilds:
            role = guild.get_role(role_id)
            if role is not None:
                return role
        configured = (
            self.get_guild(self.settings.discord.guild_id)
            if self.settings.discord.guild_id
            else None
        )
        if configured is None:
            return None
        try:
            fetched = await configured.fetch_role(role_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None
        return fetched

    async def ensure_dispatcher(self) -> DiscordAlertDispatcher | None:
        """Rebuild the dispatcher when the configured channel changed."""
        settings = self.settings.discord
        if self.dispatcher is not None and settings.alert_channel_id == self._dispatcher_channel_id:
            return self.dispatcher
        self.dispatcher = await self._build_dispatcher()
        self.worker.dispatcher = self.dispatcher
        return self.dispatcher

    async def on_message(self, message: discord.Message) -> None:
        """Refresh the alert dispatcher when the bot can see the alert channel."""
        if message.channel.id == self.settings.discord.alert_channel_id:
            await self.ensure_dispatcher()
        await super().on_message(message)

    # ----------------------------------------------------------- shutdown
    async def close(self) -> None:
        """Graceful shutdown: stop the scheduler, then release resources."""
        if self._closing:
            return
        self._closing = True
        log_event(log, 20, "SHUTDOWN_START")
        await self.scheduler.shutdown(wait=False)
        await self.container.aclose()
        engine = self.container.engine
        if engine is not None:
            await engine.dispose()
        await super().close()
        log_event(log, 20, "SHUTDOWN_COMPLETE")

    async def run_forever(self) -> None:
        """Run until a termination signal arrives, then shut down cleanly."""
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except (NotImplementedError, RuntimeError):  # pragma: no cover - platform dependent
                # Signal handlers are unavailable on some platforms (e.g. Windows).
                log_event(log, 30, "SIGNAL_HANDLER_UNAVAILABLE", signal=sig.name)
        try:
            await self.start(self.settings.discord.token_value)
            await stop.wait()
        finally:
            if not self.is_closed():
                await self.close()

    async def sync_commands(self) -> int:
        """Register slash commands (called by ``__main__``)."""
        return await register_commands(self, self.settings.discord)


def create_bot(settings: Settings | None = None) -> ScoutForgeBot:
    """Factory used by the entrypoint and tests."""
    settings = settings or get_settings()
    setup_logging(settings)
    return ScoutForgeBot(settings)


__all__ = ["INTENTS", "ScoutForgeBot", "create_bot"]
