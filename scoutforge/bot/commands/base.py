"""Shared helpers for the ScoutForge slash commands.

Every command is a thin adapter: it parses arguments, delegates to a service and
renders an embed. No business logic and no SQL live in command modules.
"""

from __future__ import annotations

from typing import Any

import discord
from discord import app_commands

from scoutforge.bot.embeds.builder import error_embed
from scoutforge.config.logging import get_logger
from scoutforge.services.container import Container

log = get_logger(__name__)

#: Upper bound on text the bot will echo back, protecting the Discord API limits.
MAX_NOTE_LENGTH = 1500


def container_of(interaction: discord.Interaction) -> Container:
    """Return the service container attached to the running bot."""
    container = getattr(interaction.client, "container", None)
    if container is None:  # pragma: no cover - defensive
        raise RuntimeError("ScoutForge container is not attached to the bot client")
    return container


async def respond(
    interaction: discord.Interaction,
    *,
    embed: discord.Embed | None = None,
    content: str | None = None,
    view: discord.ui.View | None = None,
    ephemeral: bool = False,
) -> None:
    """Send a response, deferring first when we already did work."""
    kwargs: dict[str, Any] = {"ephemeral": ephemeral}
    if embed is not None:
        kwargs["embed"] = embed
    if content is not None:
        kwargs["content"] = content
    if view is not None:
        kwargs["view"] = view
    if interaction.response.is_done():
        await interaction.followup.send(**kwargs)
        return
    await interaction.response.send_message(**kwargs)


async def fail(
    interaction: discord.Interaction,
    title: str,
    message: str,
    *,
    ephemeral: bool = False,
) -> None:
    """Send a red error embed."""
    if interaction.response.is_done():
        await interaction.followup.send(embed=error_embed(title, message), ephemeral=ephemeral)
    else:
        await interaction.response.send_message(
            embed=error_embed(title, message), ephemeral=ephemeral
        )


async def commit(session: Any) -> None:
    await session.commit()


def lead_autocomplete(games_repo: Any, *, leads_only: bool = True, limit: int = 25) -> Any:
    """Build an autocomplete callback that resolves tracked games by name."""

    async def callback(
        interaction: discord.Interaction, current: app_commands.Choice[str]
    ) -> list[app_commands.Choice[str]]:
        container = container_of(interaction)
        session = container.session()
        try:
            search = (current.value or "").strip()
            results = await games_repo(session).list_leads(
                search=search or None,
                leads_only=leads_only,
                order_by="score",
                limit=limit,
            )
            return [
                app_commands.Choice(
                    name=(game.game_name or f"Universe {game.universe_id}")[:100],
                    value=str(game.universe_id),
                )
                for game in results
            ]
        except Exception as exc:
            log.exception("lead_autocomplete failed: %s", type(exc).__name__)
            return []
        finally:
            await session.close()

    return callback


def group() -> Any:
    """Placeholder for future nested command groups."""
    return app_commands.Group(name="scoutforge", description="ScoutForge administration")


__all__ = [
    "MAX_NOTE_LENGTH",
    "commit",
    "container_of",
    "fail",
    "group",
    "lead_autocomplete",
    "respond",
]
