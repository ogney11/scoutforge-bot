"""Permission helpers.

ScoutForge separates two tiers:

* **scout** - may run every command (browse leads, update statuses, take notes)
* **admin** - may additionally change global alert settings and trigger scans

Both are driven by configuration, never hard-coded user ids.
"""

from __future__ import annotations

from typing import Any

from discord.ext import commands

from scoutforge.config.logging import get_logger, log_event
from scoutforge.config.settings import DiscordSettings, get_settings

log = get_logger(__name__)


def configured_admin_role_ids(settings: DiscordSettings | None = None) -> set[int]:
    settings = settings or get_settings().discord
    return set(settings.admin_role_ids)


def is_admin(member: Any, *, settings: DiscordSettings | None = None) -> bool:
    """Whether ``member`` may run administrative commands."""
    settings = settings or get_settings().discord
    if member is None:
        return False

    permissions = getattr(member, "guild_permissions", None)
    if permissions is not None and (permissions.administrator or permissions.manage_guild):
        return True

    admin_roles = configured_admin_role_ids(settings)
    if not admin_roles:
        return False
    roles = getattr(member, "roles", None) or ()
    return any(getattr(role, "id", None) in admin_roles for role in roles)


def has_admin_role(member: Any, settings: DiscordSettings | None = None) -> bool:
    """Alias of :func:`is_admin`, kept for readability at call sites."""
    return is_admin(member, settings=settings)


def require_admin() -> Any:
    """``check`` decorator for administrative commands."""

    async def predicate(ctx: commands.Context) -> bool:
        if is_admin(ctx.author):
            return True
        await ctx.send("❌ This command requires the ScoutForge admin role.", ephemeral=True)
        return False

    return commands.check(predicate)


async def register_commands(bot: Any, settings: DiscordSettings | None = None) -> int:
    """Register slash commands to the dev guild (fast) or globally.

    Guild-scoped registration takes effect in seconds, which is what you want
    while iterating. Global registration propagates over ~1 hour, so it is only
    used for production.

    Returns:
        Number of commands in the tree after syncing.
    """
    settings = settings or get_settings().discord
    tree = bot.tree
    scope = "global"
    if not settings.command_global and settings.guild_id:
        guild = bot.get_guild(settings.guild_id)
        target = guild if guild is not None else await bot.fetch_guild(settings.guild_id)
        await tree.sync(guild=target)
        scope = f"guild:{settings.guild_id}"
    else:
        await tree.sync()

    count = len(tree.get_commands())
    log_event(log, 20, "COMMANDS_SYNCED", scope=scope, count=count)
    return count


__all__ = [
    "configured_admin_role_ids",
    "has_admin_role",
    "is_admin",
    "register_commands",
    "require_admin",
]
