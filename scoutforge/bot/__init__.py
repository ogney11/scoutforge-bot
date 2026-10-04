"""Discord bot layer: client, commands, views and embeds."""

from scoutforge.bot.alerts import DiscordAlertDispatcher
from scoutforge.bot.client import ScoutForgeBot, create_bot
from scoutforge.bot.permissions import is_admin, register_commands, require_admin
from scoutforge.bot.scheduler import ScanScheduler

__all__ = [
    "DiscordAlertDispatcher",
    "ScanScheduler",
    "ScoutForgeBot",
    "create_bot",
    "is_admin",
    "register_commands",
    "require_admin",
]
