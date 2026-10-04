"""Discovery commands: ``/scan`` and ``/sources``."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import discord
from discord import app_commands

if TYPE_CHECKING:  # pragma: no cover
    from scoutforge.bot.client import ScoutForgeBot

from scoutforge.bot.commands.base import container_of, fail, respond
from scoutforge.bot.embeds.builder import COLOUR_NORMAL, Embed, error_embed, scan_result_embed
from scoutforge.bot.permissions import is_admin
from scoutforge.config.logging import get_logger, log_event
from scoutforge.discovery.registry import available_sources
from scoutforge.services.discovery_service import DiscoveryWorker

log = get_logger(__name__)


def register(bot: Any) -> None:
    """Attach the discovery commands to ``bot``."""

    @bot.tree.command(name="scan", description="Run a discovery scan right now")
    @app_commands.describe(
        limit="Maximum games to inspect this scan (defaults to the configured budget)",
        admin_only="Restrict this command to admin roles",
    )
    async def scan_command(
        interaction: discord.Interaction,
        limit: app_commands.Range[int, 1, 1000] | None = None,
        admin_only: bool = False,
    ) -> None:
        if admin_only and not is_admin(interaction.user):
            await fail(
                interaction, "Not allowed", "You need the ScoutForge admin role.", ephemeral=True
            )
            return

        container = container_of(interaction)
        if not container.settings.worker.enabled and not is_admin(interaction.user):
            await fail(
                interaction,
                "Worker disabled",
                "The scheduled discovery worker is disabled (`WORKER_ENABLED=false`). "
                "Ask an admin to run a manual scan.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True, ephemeral=True)
        client = cast("ScoutForgeBot", interaction.client)
        worker: DiscoveryWorker = client.worker
        try:
            result = await worker.run_once(triggered_by=f"slash:{interaction.user.id}", limit=limit)
        except Exception as exc:
            log.exception("scan failed")
            await interaction.followup.send(
                embed=error_embed("Scan failed", f"{type(exc).__name__}: {exc}"[:1000]),
                ephemeral=True,
            )
            return

        log_event(
            log,
            20,
            "SCAN_COMMAND",
            by=interaction.user.id,
            candidates=result.candidates_found,
            accepted=result.games_accepted,
        )
        await interaction.followup.send(embed=scan_result_embed(result), ephemeral=True)

    @bot.tree.command(name="sources", description="Show the configured discovery sources")
    async def sources_command(interaction: discord.Interaction) -> None:
        container = container_of(interaction)
        embed = Embed(
            title="🔌 Discovery sources",
            colour=COLOUR_NORMAL,
            description=(
                "Candidates from every enabled source are merged and deduplicated by "
                "`universe_id` before filtering."
            ),
        )
        for source in container.sources.sources if container.sources else []:
            embed.add_field(
                name=source.name,
                value=source.description or type(source).__name__,
                inline=False,
            )
        enabled = set(container.settings.discovery.enabled_sources)
        embed.add_field(
            name="Registered but disabled",
            value=", ".join(name for name in available_sources() if name not in enabled) or "none",
            inline=False,
        )
        await respond(interaction, embed=embed, ephemeral=True)


__all__ = ["register"]
