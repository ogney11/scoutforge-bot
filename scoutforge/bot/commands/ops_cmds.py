"""Operational commands: ``/stats`` and the ``/alerts`` control group."""

from __future__ import annotations

from typing import Any

import discord
from discord import app_commands

from scoutforge.bot.commands.base import container_of
from scoutforge.bot.embeds.builder import COLOUR_NORMAL, Embed, error_embed, stats_embed
from scoutforge.bot.permissions import is_admin
from scoutforge.config.logging import get_logger, log_event
from scoutforge.database.repositories import AlertRepository, GameRepository
from scoutforge.enums import AlertState
from scoutforge.services.settings_service import (
    KEY_ALERTS_ENABLED,
    KEY_EXTREME_COOLDOWN,
    KEY_EXTREME_SCORE,
    KEY_HOT_COOLDOWN,
    KEY_HOT_SCORE,
    KEY_WARM_SCORE,
    SettingsService,
    UnknownSettingError,
)
from scoutforge.services.stats_service import StatsService
from scoutforge.utils import format_duration, relative_time

log = get_logger(__name__)

STATUS_TEXT = {
    "on": ("✅", "Alerts **enabled**"),
    "off": ("⛔", "Alerts **disabled** — no more HOT/EXTREME notifications"),
    "threshold": ("🎚", "Alert thresholds updated"),
    "cooldown": ("⏳", "Cooldown updated"),
    "show": ("", ""),
}


def register(bot: Any) -> None:
    """Attach the operational commands to ``bot``."""

    @bot.tree.command(name="stats", description="Scanning, discovery and alerting telemetry")
    async def stats_command(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        container = container_of(interaction)
        session = container.session()
        try:
            snapshot = await StatsService(session).snapshot()
            funnel = await StatsService(session).funnel_counts()
        finally:
            await session.close()

        embed = stats_embed(snapshot)
        embed.add_field(
            name="Funnel",
            value=(
                f"Discovered {funnel['discovered']} → Leads {funnel['leads']} → "
                f"Contacted {funnel['contacted']} → Interested {funnel['interested']} → "
                f"Deals {funnel['deals']}"
            ),
            inline=False,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    alerts_group = app_commands.Group(
        name="alerts",
        description="Control scouting alerts",
    )

    @alerts_group.command(name="status", description="Show the current alert configuration")
    async def alerts_status(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        container = container_of(interaction)
        session = container.session()
        try:
            service = SettingsService(session)
            effective = await service.effective_alerts(container.settings.alerts)
            rows = await service.describe()
        finally:
            await session.close()

        embed = Embed(title="🔔 Alert configuration", colour=COLOUR_NORMAL)
        embed.add_field(
            name="State",
            value="✅ Enabled" if effective.enabled else "⛔ Disabled",
            inline=True,
        )
        embed.add_field(
            name="Thresholds",
            value=(
                f"WARM ≥ {effective.warm_score:.0f}\n"
                f"HOT ≥ {effective.hot_score:.0f}\n"
                f"EXTREME ≥ {effective.extreme_score:.0f}"
            ),
            inline=True,
        )
        embed.add_field(
            name="Cooldowns",
            value=(
                f"HOT {format_duration(effective.hot_cooldown_seconds)}\n"
                f"EXTREME {format_duration(effective.extreme_cooldown_seconds)}\n"
                f"WARM {format_duration(effective.warm_cooldown_seconds)}"
            ),
            inline=True,
        )
        embed.add_field(
            name="Runtime overrides",
            value="\n".join(f"`{key}` = {value}" for key, value, _desc in rows),
            inline=False,
        )
        embed.add_field(
            name="Re-alert exceptions",
            value=(
                f"Score jump ≥ +{effective.score_jump_threshold:.0f} points, or "
                f"CCU spike ≥ +{effective.ccu_spike_pct:.0f}%."
            ),
            inline=False,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @alerts_group.command(name="toggle", description="Turn alerting on or off")
    @app_commands.describe(state="Desired alerting state")
    async def alerts_toggle(
        interaction: discord.Interaction,
        state: AlertState,
    ) -> None:
        if not is_admin(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Not allowed", "Changing alert state requires the admin role."),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        container = container_of(interaction)
        session = container.session()
        try:
            value = await SettingsService(session).set(
                KEY_ALERTS_ENABLED, state is AlertState.ON, updated_by=interaction.user.id
            )
            await session.commit()
        finally:
            await session.close()

        log_event(log, 20, "ALERTS_TOGGLED", enabled=value, by=interaction.user.id)
        emoji, text = STATUS_TEXT["on" if value else "off"]
        await interaction.followup.send(content=f"{emoji} {text}", ephemeral=True)

    @alerts_group.command(name="threshold", description="Set the HOT / EXTREME score thresholds")
    @app_commands.describe(
        hot="Minimum score for a HOT alert",
        extreme="Minimum score for an EXTREME alert (must be >= hot)",
        warm="Minimum score for a WARM alert (must be <= hot)",
    )
    async def alerts_threshold(
        interaction: discord.Interaction,
        hot: app_commands.Range[float, 0, 100] | None = None,
        extreme: app_commands.Range[float, 0, 100] | None = None,
        warm: app_commands.Range[float, 0, 100] | None = None,
    ) -> None:
        if not is_admin(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Not allowed", "Changing thresholds requires the admin role."),
                ephemeral=True,
            )
            return
        if hot is None and extreme is None and warm is None:
            await interaction.response.send_message(
                embed=error_embed(
                    "Nothing to change", "Provide at least one of `hot`, `extreme` or `warm`."
                ),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        container = container_of(interaction)
        session = container.session()
        service = SettingsService(session)
        try:
            base = container.settings.alerts
            new_hot = hot if hot is not None else base.hot_score
            new_extreme = extreme if extreme is not None else max(base.extreme_score, new_hot)
            new_warm = warm if warm is not None else min(base.warm_score, new_hot)

            if not (new_warm <= new_hot <= new_extreme):
                await session.rollback()
                await interaction.followup.send(
                    embed=error_embed("Invalid thresholds", "Expected WARM ≤ HOT ≤ EXTREME."),
                    ephemeral=True,
                )
                return

            await service.set(KEY_WARM_SCORE, new_warm, updated_by=interaction.user.id)
            await service.set(KEY_HOT_SCORE, new_hot, updated_by=interaction.user.id)
            await service.set(KEY_EXTREME_SCORE, new_extreme, updated_by=interaction.user.id)
            await session.commit()
        except UnknownSettingError as exc:
            await session.rollback()
            await interaction.followup.send(
                embed=error_embed("Unknown setting", str(exc)), ephemeral=True
            )
            return
        finally:
            await session.close()

        await interaction.followup.send(
            content=(
                "🎚 Thresholds updated: "
                f"WARM ≥ {new_warm:.0f} · HOT ≥ {new_hot:.0f} · EXTREME ≥ {new_extreme:.0f}"
            ),
            ephemeral=True,
        )

    @alerts_group.command(name="cooldown", description="Set alert cooldowns in hours")
    @app_commands.describe(
        hot_hours="Hours before the same HOT lead can alert again",
        extreme_hours="Hours before the same EXTREME lead can alert again",
    )
    async def alerts_cooldown(
        interaction: discord.Interaction,
        hot_hours: app_commands.Range[float, 0, 168] | None = None,
        extreme_hours: app_commands.Range[float, 0, 168] | None = None,
    ) -> None:
        if not is_admin(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Not allowed", "Changing cooldowns requires the admin role."),
                ephemeral=True,
            )
            return
        if hot_hours is None and extreme_hours is None:
            await interaction.response.send_message(
                embed=error_embed(
                    "Nothing to change", "Provide `hot_hours` and/or `extreme_hours`."
                ),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        container = container_of(interaction)
        session = container.session()
        service = SettingsService(session)
        try:
            if hot_hours is not None:
                await service.set(
                    KEY_HOT_COOLDOWN, int(hot_hours * 3600), updated_by=interaction.user.id
                )
            if extreme_hours is not None:
                await service.set(
                    KEY_EXTREME_COOLDOWN, int(extreme_hours * 3600), updated_by=interaction.user.id
                )
            await session.commit()
        finally:
            await session.close()

        message = "⏳ Cooldowns updated"
        if hot_hours is not None:
            message += f" · HOT {hot_hours:g}h"
        if extreme_hours is not None:
            message += f" · EXTREME {extreme_hours:g}h"
        await interaction.followup.send(content=message, ephemeral=True)

    @alerts_group.command(name="history", description="Recent alert history")
    @app_commands.describe(limit="How many entries")
    async def alerts_history(
        interaction: discord.Interaction, limit: app_commands.Range[int, 1, 25] = 10
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        container = container_of(interaction)
        session = container.session()
        try:
            alerts = await AlertRepository(session).recent(limit=limit)
            games = {
                g.universe_id: g.game_name
                for g in await GameRepository(session).get_many([a.universe_id for a in alerts])
            }
        finally:
            await session.close()

        if not alerts:
            await interaction.followup.send(content="No alerts recorded yet.", ephemeral=True)
            return

        lines = []
        for alert in alerts:
            state = "sent" if alert.delivered else (alert.suppressed_reason or "not sent")
            lines.append(
                f"`{alert.level}` **{games.get(alert.universe_id, alert.universe_id)}** · "
                f"score {alert.score:.0f} · {state} · {relative_time(alert.created_at)}"
            )
        embed = Embed(
            title="🔔 Alert history",
            description="\n".join(lines)[:4000],
            colour=COLOUR_NORMAL,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    bot.tree.add_command(alerts_group)

    @bot.tree.command(name="help", description="ScoutForge command overview")
    async def help_command(interaction: discord.Interaction) -> None:
        from scoutforge.bot.embeds.builder import help_embed

        await interaction.response.send_message(embed=help_embed(), ephemeral=True)


__all__ = ["register"]
