"""Discord event handlers.

The bot only needs a couple of events:

* ``on_ready`` / ``on_resumed`` / ``on_disconnect`` - connection telemetry
* guild-scoped slash command sync for dev convenience
* keeping the alert dispatcher pointed at the right channel

It deliberately does **not** read message content, so the bot cannot be used to
harvest conversations.
"""

from __future__ import annotations

from typing import Any

import discord

from scoutforge.config.logging import get_logger, log_event

log = get_logger(__name__)


def register_events(bot: Any) -> None:
    """Attach ScoutForge's event listeners to ``bot``."""

    @bot.event
    async def on_connect() -> None:
        log_event(log, 20, "GATEWAY_CONNECTED")

    @bot.event
    async def on_resumed() -> None:
        log_event(log, 20, "GATEWAY_RESUMED")

    @bot.event
    async def on_disconnect() -> None:
        log_event(log, 25, "GATEWAY_DISCONNECTED")

    @bot.event
    async def on_guild_join(guild: discord.Guild) -> None:
        log_event(log, 20, "GUILD_JOIN", guild=guild.id, name=guild.name)

    if getattr(bot, "tree", None) is not None:
        bot.tree.on_error = _on_app_command_error


async def _on_app_command_error(interaction: discord.Interaction, error: Exception) -> None:
    """Report command errors to the user instead of leaving them hanging."""
    log_event(
        log,
        40,
        "COMMAND_ERROR",
        command=getattr(interaction.command, "qualified_name", "unknown"),
        error=type(error).__name__,
        user=interaction.user.id,
    )
    log.error("slash command failed: %r", error)
    message = "Something went wrong running that command. The error has been logged."
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


__all__ = ["register_events"]
