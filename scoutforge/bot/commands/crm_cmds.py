"""CRM commands: ``/status``, ``/note``, ``/history`` and ``/assign``.

All status input is validated against the closed :class:`LeadStatus` enum and the
transition state machine — an invalid or unreachable status produces a helpful
error listing the legal choices, never a silent write.
"""

from __future__ import annotations

from typing import Any

import discord
from discord import app_commands

from scoutforge.bot.commands.base import MAX_NOTE_LENGTH, container_of, lead_autocomplete
from scoutforge.bot.embeds.builder import COLOUR_NORMAL, STATUS_EMOJI, Embed, error_embed
from scoutforge.database.repositories import GameRepository, ScoutUserRepository
from scoutforge.enums import LeadStatus
from scoutforge.services.crm_service import (
    InvalidTransitionError,
    LeadService,
    UnknownStatusError,
    allowed_transitions,
)
from scoutforge.utils import relative_time

MAX_HISTORY = 15


def status_choices() -> list[app_commands.Choice[str]]:
    """Autocomplete choices for every valid status."""
    return [app_commands.Choice(name=s.value, value=s.value) for s in LeadStatus]


def register(bot: Any) -> None:
    """Attach the CRM commands to ``bot``."""
    autocomplete = lead_autocomplete(GameRepository)

    @bot.tree.command(name="status", description="Move a lead through the scouting pipeline")
    @app_commands.describe(
        universe_id="The Roblox universe id",
        status="New status",
        reason="Optional note stored in the audit trail",
        force="Admin only: skip transition validation",
    )
    async def status_command(
        interaction: discord.Interaction,
        universe_id: str,
        status: str,
        reason: str | None = None,
        force: bool = False,
    ) -> None:
        from scoutforge.bot.permissions import is_admin

        await interaction.response.defer(ephemeral=True, thinking=True)
        resolved = _resolve(universe_id)
        if resolved is None:
            await _bad_id(interaction, universe_id)
            return

        if force and not is_admin(interaction.user):
            await interaction.followup.send(
                embed=error_embed("Not allowed", "`force` requires the ScoutForge admin role."),
                ephemeral=True,
            )
            return

        container = container_of(interaction)
        session = container.session()
        service = LeadService(session)
        try:
            game, entry = await service.change_status(
                resolved,
                status,
                actor_id=interaction.user.id,
                actor_name=str(interaction.user),
                reason=reason,
                force=force,
            )
            await session.commit()
        except UnknownStatusError as exc:
            await session.rollback()
            await interaction.followup.send(
                embed=error_embed("Invalid status", str(exc)), ephemeral=True
            )
            return
        except InvalidTransitionError as exc:
            await session.rollback()
            legal = ", ".join(f"`{s.value}`" for s in sorted(exc.allowed, key=lambda s: s.value))
            await interaction.followup.send(
                embed=error_embed(
                    "Invalid transition",
                    f"{exc}\n\nAllowed next steps: {legal or '_none — use `force=true` as an admin_'}",
                ),
                ephemeral=True,
            )
            return
        except LookupError as exc:
            await session.rollback()
            await interaction.followup.send(
                embed=error_embed("Not tracked", str(exc)), ephemeral=True
            )
            return
        finally:
            await session.close()

        embed = Embed(
            title=f"{STATUS_EMOJI[LeadStatus(game.status)]} {game.game_name}",
            description=(
                f"Status **{LeadStatus(game.status).value}** "
                f"(set by {interaction.user}" + (f" · {reason}" if reason else "") + ")"
            ),
            colour=COLOUR_NORMAL,
        )
        embed.add_field(name="Universe", value=f"`{game.universe_id}`", inline=True)
        embed.add_field(name="Entry", value=f"`{entry.id}`", inline=True)
        embed.add_field(
            name="Remaining steps", value=_remaining(LeadStatus(game.status)), inline=False
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    status_command.autocomplete("universe_id")(autocomplete)
    status_command.autocomplete("status")(_status_autocomplete)

    @bot.tree.command(name="note", description="Attach a scouting note to a lead")
    @app_commands.describe(universe_id="The Roblox universe id", text="Your note")
    async def note_command(interaction: discord.Interaction, universe_id: str, text: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        resolved = _resolve(universe_id)
        if resolved is None:
            await _bad_id(interaction, universe_id)
            return

        body = text.strip()
        if not body:
            await interaction.followup.send(
                embed=error_embed("Empty note", "Write something before submitting the note."),
                ephemeral=True,
            )
            return
        if len(body) > MAX_NOTE_LENGTH:
            await interaction.followup.send(
                embed=error_embed(
                    "Note too long", f"Notes are limited to {MAX_NOTE_LENGTH} characters."
                ),
                ephemeral=True,
            )
            return

        container = container_of(interaction)
        session = container.session()
        try:
            note = await LeadService(session).add_note(
                resolved, body, author_id=interaction.user.id, author_name=str(interaction.user)
            )
            await session.commit()
        except LookupError as exc:
            await session.rollback()
            await interaction.followup.send(
                embed=error_embed("Not tracked", str(exc)), ephemeral=True
            )
            return
        finally:
            await session.close()

        await interaction.followup.send(
            content=f"📝 Note #{note.id} saved on universe `{resolved}`.",
            ephemeral=True,
        )

    note_command.autocomplete("universe_id")(autocomplete)

    @bot.tree.command(name="history", description="Show a lead's status + note timeline")
    @app_commands.describe(universe_id="The Roblox universe id", limit="How many entries")
    async def history_command(
        interaction: discord.Interaction,
        universe_id: str,
        limit: app_commands.Range[int, 1, 25] = MAX_HISTORY,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        resolved = _resolve(universe_id)
        if resolved is None:
            await _bad_id(interaction, universe_id)
            return

        container = container_of(interaction)
        session = container.session()
        try:
            game = await GameRepository(session).get(resolved)
            if game is None:
                await interaction.followup.send(
                    embed=error_embed("Not tracked", f"Universe `{resolved}` is unknown."),
                    ephemeral=True,
                )
                return
            entries = await LeadService(session).timeline(resolved, limit=limit)
        finally:
            await session.close()

        if not entries:
            await interaction.followup.send(
                content=f"No history yet for universe `{resolved}`.",
                ephemeral=True,
            )
            return

        lines = []
        for entry in entries:
            icon = "🗒" if entry["kind"] == "note" else "🔀"
            author = entry["author"] or "system"
            lines.append(
                f"{icon} <t:{int(entry['at'].timestamp())}:R> · **{author}** — {entry['text']}"
            )

        embed = Embed(
            title=f"🧾 History: {game.game_name}",
            description="\n".join(lines)[:4000],
            colour=COLOUR_NORMAL,
        )
        embed.add_field(name="Universe", value=f"`{resolved}`", inline=True)
        embed.add_field(
            name="Status",
            value=LeadStatus(game.status).value,
            inline=True,
        )
        embed.add_field(
            name="Last seen",
            value=relative_time(game.last_seen_at),
            inline=True,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    history_command.autocomplete("universe_id")(autocomplete)

    @bot.tree.command(name="assign", description="Assign a lead to a scout")
    @app_commands.describe(universe_id="The Roblox universe id")
    async def assign_command(interaction: discord.Interaction, universe_id: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        resolved = _resolve(universe_id)
        if resolved is None:
            await _bad_id(interaction, universe_id)
            return

        container = container_of(interaction)
        session = container.session()
        try:
            users = ScoutUserRepository(session)
            await users.upsert(
                interaction.user.id,
                username=interaction.user.name,
                display_name=interaction.user.display_name,
            )
            await users.assign(
                resolved, user_id=interaction.user.id, display_name=interaction.user.display_name
            )
            game = await GameRepository(session).get(resolved)
            await session.commit()
        finally:
            await session.close()

        await interaction.followup.send(
            content=(
                f"👤 Assigned universe `{resolved}` to {interaction.user.display_name}."
                if game
                else f"👤 Universe `{resolved}` is not tracked yet."
            ),
            ephemeral=True,
        )

    assign_command.autocomplete("universe_id")(autocomplete)

    @bot.tree.command(
        name="myqueue", description="Leads assigned to you that are not contacted yet"
    )
    @app_commands.describe(limit="How many leads to show")
    async def myqueue_command(
        interaction: discord.Interaction, limit: app_commands.Range[int, 1, 10] = 5
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        container = container_of(interaction)
        session = container.session()
        service = LeadService(session)
        try:
            candidates = await service.contact_candidates(limit=limit * 3)
            mine = [g for g in candidates if g.assigned_scout_id == interaction.user.id]
        finally:
            await session.close()

        if not mine:
            await interaction.followup.send(
                content="📭 Nothing in your queue. Use `/assign <id>` to claim a lead.",
                ephemeral=True,
            )
            return

        lines = [
            f"• [`{g.game_name}`]({g.roblox_url}) — {STATUS_EMOJI[LeadStatus(g.status)]} "
            f"{LeadStatus(g.status).value} · score {g.current_score:.0f}"
            for g in mine[:limit]
            if g.current_score is not None
        ]
        embed = Embed(
            title="📋 Your scouting queue",
            description="\n".join(lines) or "No scored leads assigned yet.",
            colour=COLOUR_NORMAL,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _resolve(value: str) -> int | None:
    text = (value or "").strip()
    if text.isdigit():
        resolved = int(text)
        return resolved if resolved > 0 else None
    return None


async def _bad_id(interaction: discord.Interaction, value: str) -> None:
    await interaction.followup.send(
        embed=error_embed("Invalid id", f"`{value}` is not a Roblox universe id."),
        ephemeral=True,
    )


def _remaining(status: LeadStatus) -> str:
    options = sorted(allowed_transitions(status), key=lambda s: s.value)
    return ", ".join(f"`{s.value}`" for s in options) or "none"


async def _status_autocomplete(
    interaction: discord.Interaction, current: app_commands.Choice[str]
) -> list[app_commands.Choice[str]]:
    query = (current.value or "").strip().upper()
    choices = status_choices()
    if not query:
        return choices
    return [c for c in choices if query in c.name]


__all__ = ["register", "status_choices"]
