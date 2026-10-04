"""ScoutForge slash commands."""

from scoutforge.bot.commands import (
    base,
    crm_cmds,
    discovery_cmds,
    lead_cmds,
    ops_cmds,
    partner_cmds,
)

#: Command modules registered on the bot tree, in registration order.
COMMAND_MODULES = (
    discovery_cmds,
    lead_cmds,
    crm_cmds,
    ops_cmds,
    partner_cmds,
)

__all__ = [
    "COMMAND_MODULES",
    "base",
    "crm_cmds",
    "discovery_cmds",
    "lead_cmds",
    "ops_cmds",
    "partner_cmds",
]
