"""Lead browsing commands: ``/leads``, ``/lead``, ``/search`` and ``/hot``."""

from __future__ import annotations

from typing import Any

import discord
from discord import app_commands

from scoutforge.bot.commands.base import container_of, lead_autocomplete
from scoutforge.bot.embeds.builder import (
    COLOUR_NORMAL,
    Embed,
    error_embed,
    lead_embed,
    level_colour,
    level_emoji,
)
from scoutforge.bot.views.lead_actions import LeadActionsView
from scoutforge.database.repositories import (
    GameRepository,
    GameScoreRepository,
    GameSnapshotRepository,
)
from scoutforge.enums import AlertLevel, LeadStatus
from scoutforge.monitoring.growth import compute_growth
from scoutforge.monitoring.snapshots import SnapshotReader
from scoutforge.services.crm_service import LeadService
from scoutforge.utils import format_compact, format_pct, relative_time, safe_div

MAX_CARDS = 10


def register(bot: Any) -> None:
    """Attach the lead browsing commands to ``bot``."""
    autocomplete = lead_autocomplete(GameRepository)

    @bot.tree.command(name="leads", description="Browse scouting leads")
    @app_commands.describe(
        status="Filter by CRM status",
        level="Filter by alert level",
        sort="Sort order",
        limit="How many leads to show",
    )
    async def leads_command(
        interaction: discord.Interaction,
        status: app_commands.Choice[str] | None = None,
        level: app_commands.Choice[str] | None = None,
        sort: app_commands.Choice[str] | None = None,
        limit: app_commands.Range[int, 1, 10] = 5,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        container = container_of(interaction)
        session = container.session()
        try:
            games = await GameRepository(session).list_leads(
                status=LeadStatus(status.value) if status else None,
                level=AlertLevel(level.value) if level else None,
                order_by=sort.value if sort else "score",
                limit=limit,
            )
        finally:
            await session.close()

        if not games:
            await interaction.followup.send(
                embed=Embed(
                    title="No leads match",
                    description="Run `/scan` to discover games, or relax the filters.",
                    colour=COLOUR_NORMAL,
                ),
                ephemeral=True,
            )
            return

        for game in games:
            embed = lead_embed(game=game, description_limit=0)
            await interaction.followup.send(embed=embed, ephemeral=True)
        log_line = f"Showing {len(games)} lead(s)"
        if status:
            log_line += f" · status={status.value}"
        if level:
            log_line += f" · level={level.value}"
        await interaction.followup.send(content=log_line, ephemeral=True)

    @bot.tree.command(name="lead", description="Full detail for a single tracked game")
    @app_commands.describe(universe_id="The Roblox universe id")
    async def lead_command(interaction: discord.Interaction, universe_id: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        resolved = _resolve_universe_id(universe_id)
        if resolved is None:
            await interaction.followup.send(
                embed=error_embed("Invalid id", f"`{universe_id}` is not a Roblox universe id."),
                ephemeral=True,
            )
            return

        container = container_of(interaction)
        session = container.session()
        try:
            service = LeadService(session)
            game = await service.get_lead(resolved, require_lead=False)
            if game is None:
                await interaction.followup.send(
                    embed=error_embed(
                        "Not tracked", f"Universe `{resolved}` has never been seen by ScoutForge."
                    ),
                    ephemeral=True,
                )
                return

            reader = SnapshotReader(GameSnapshotRepository(session))
            snapshots = await reader.series(resolved, limit=200)
            growth = compute_growth(resolved, snapshots)
            view = await service.build_view(game, growth=growth.to_dict())
        finally:
            await session.close()

        embed = lead_embed(
            game=view.game,
            score=view.score,
            level=view.level,
            growth=view.growth,
            status=view.status,
            show_notes=True,
            notes=view.notes,
        )
        embed.add_field(name="🔗 Links", value=_link_block(view), inline=False)
        await interaction.followup.send(
            embed=embed,
            view=LeadActionsView(
                universe_id=resolved,
                status=view.status,
                roblox_url=view.game.roblox_url or "",
                owner_id=interaction.user.id,
                growth=view.growth,
            ),
            ephemeral=True,
        )

    lead_command.autocomplete("universe_id")(autocomplete)

    @bot.tree.command(name="search", description="Search tracked games by name or creator")
    @app_commands.describe(
        query="Game name or creator name",
        include_closed="Include rejected / not-interested leads",
        limit="Maximum results",
    )
    async def search_command(
        interaction: discord.Interaction,
        query: str,
        include_closed: bool = False,
        limit: app_commands.Range[int, 1, 10] = 5,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not query.strip():
            await interaction.followup.send(
                embed=error_embed("Empty query", "Provide a game or creator name to search for."),
                ephemeral=True,
            )
            return

        container = container_of(interaction)
        session = container.session()
        try:
            results = await GameRepository(session).list_leads(
                search=query.strip(),
                order_by="score",
                limit=limit,
                leads_only=not include_closed,
            )
            if include_closed:
                results = await GameRepository(session).list_leads(
                    search=query.strip(), order_by="score", limit=limit, leads_only=False
                )
        finally:
            await session.close()

        if not results:
            await interaction.followup.send(
                embed=Embed(
                    title="No matches",
                    description=f"Nothing tracked matches **{query.strip()}**.",
                    colour=COLOUR_NORMAL,
                ),
                ephemeral=True,
            )
            return

        lines: list[str] = []
        for game in results:
            score = f"{game.current_score:.0f}" if game.current_score is not None else "n/a"
            lines.append(
                f"{level_emoji(game.current_level)} [`{game.game_name}`]({game.roblox_url}) · "
                f"`{game.universe_id}`\n"
                f"CCU {format_compact(game.current_ccu)} · visits "
                f"{format_compact(game.lifetime_visits)} · score {score} · "
                f"{LeadStatus(game.status).value}"
            )
        embed = Embed(
            title=f"🔎 Search: {query.strip()}",
            description="\n".join(lines)[:4000],
            colour=COLOUR_NORMAL,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @bot.tree.command(name="hot", description="Top HOT and EXTREME opportunities right now")
    @app_commands.describe(limit="How many leads to show")
    async def hot_command(
        interaction: discord.Interaction, limit: app_commands.Range[int, 1, 10] = 5
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        container = container_of(interaction)
        session = container.session()
        try:
            games = await GameRepository(session).list_leads(
                min_score=container.settings.alerts.hot_score,
                order_by="score",
                limit=limit,
            )
            snapshots_repo = GameSnapshotRepository(session)
            growth_by_universe: dict[int, dict[str, Any]] = {}
            for game in games:
                series = await snapshots_repo.window(game.universe_id, limit=200)
                growth_by_universe[game.universe_id] = compute_growth(
                    game.universe_id, series
                ).to_dict()
        finally:
            await session.close()

        if not games:
            await interaction.followup.send(
                embed=Embed(
                    title="🔥 No hot leads",
                    description=(
                        f"Nothing currently scores ≥ **{container.settings.alerts.hot_score:.0f}**. "
                        "That is normal between spikes — run `/scan` to refresh."
                    ),
                    colour=COLOUR_NORMAL,
                ),
                ephemeral=True,
            )
            return

        for game in games:
            embed = lead_embed(
                game=game, growth=growth_by_universe.get(game.universe_id), description_limit=0
            )
            view = LeadActionsView(
                universe_id=game.universe_id,
                status=LeadStatus(game.status),
                roblox_url=game.roblox_url or "",
                owner_id=interaction.user.id,
                growth=growth_by_universe.get(game.universe_id),
            )
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @bot.tree.command(name="why", description="Explain a game's Scout Score component by component")
    @app_commands.describe(universe_id="The Roblox universe id")
    async def why_command(interaction: discord.Interaction, universe_id: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        resolved = _resolve_universe_id(universe_id)
        if resolved is None:
            await interaction.followup.send(
                embed=error_embed("Invalid id", f"`{universe_id}` is not a Roblox universe id."),
                ephemeral=True,
            )
            return

        container = container_of(interaction)
        session = container.session()
        try:
            latest = await GameScoreRepository(session).latest(resolved)
        finally:
            await session.close()

        if latest is None:
            await interaction.followup.send(
                embed=error_embed("No score", f"Universe `{resolved}` has never been scored."),
                ephemeral=True,
            )
            return

        embed = Embed(
            title=f"🧮 Why: {latest.score:.0f}/100",
            colour=level_colour(latest.level),
            description="Every point is attributable to a measured component.",
        )
        for key, component in (latest.components or {}).items():
            weight = component.get("weight", 0)
            points = component.get("points", 0.0)
            embed.add_field(
                name=component.get("label", key),
                value=f"`{points:.1f} / {weight}` points"
                + ("" if component.get("available") else " — *metric unavailable*"),
                inline=False,
            )
        if latest.reasons:
            embed.add_field(
                name="Reasons",
                value="\n".join(f"• {r}" for r in latest.reasons)[:1024],
                inline=False,
            )
        if latest.caveats:
            embed.add_field(
                name="Limitations",
                value="\n".join(f"• {c}" for c in latest.caveats)[:1024],
                inline=False,
            )
        embed.set_footer(text=f"Computed {relative_time(latest.computed_at)}")
        await interaction.followup.send(embed=embed, ephemeral=True)

    why_command.autocomplete("universe_id")(autocomplete)


def _resolve_universe_id(value: str) -> int | None:
    text = (value or "").strip()
    if text.isdigit():
        resolved = int(text)
        return resolved if resolved > 0 else None
    return None


def _link_block(view: Any) -> str:
    parts = [f"[Roblox]({view.game.roblox_url})"]
    if view.game.public_discord_url:
        parts.append(f"[Public Discord]({view.game.public_discord_url})")
    if view.game.creator_url:
        parts.append(f"[Creator]({view.game.creator_url})")
    return " • ".join(parts)


def growth_summary(snapshots: list[Any]) -> str:
    """Render a one-line growth summary for a snapshot series."""
    growth = compute_growth(0, snapshots)
    window = growth.primary_growth
    if window is None:
        return "no history yet"
    ratio = safe_div(window.to_ccu, window.samples)
    return f"{format_pct(window.pct_change)} over {window.window_hours}h · ~{ratio:.0f} CCU"


__all__ = ["register"]
