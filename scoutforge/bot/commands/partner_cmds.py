"""Partner commands: ``/partners``, ``/match``, ``/shortlist`` and ``/outreach``.

These commands only read what the matcher already computed and record human
actions. There is deliberately no code path that sends a message to a partner or
a developer on anyone's behalf.
"""

from __future__ import annotations

from typing import Any

import discord
from discord import app_commands

from scoutforge.bot.commands.base import container_of, lead_autocomplete
from scoutforge.bot.embeds.builder import (
    COLOUR_NORMAL,
    Embed,
    error_embed,
    match_embed,
    partners_embed,
)
from scoutforge.config.logging import get_logger
from scoutforge.database.repositories import GameRepository
from scoutforge.enums import PartnerOutreach
from scoutforge.services.partner_service import ProfileLoadError

log = get_logger(__name__)


def register(bot: Any) -> None:
    """Attach the partner commands to ``bot``."""
    autocomplete = lead_autocomplete(GameRepository)

    @bot.tree.command(name="partners", description="Deal terms of every active partner profile")
    @app_commands.describe(partner="Show the best-fit games for one partner")
    async def partners_command(
        interaction: discord.Interaction,
        partner: app_commands.Choice[str] | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        container = container_of(interaction)
        session = container.session()
        service = container.partner_service(session)
        try:
            profiles = await service.list_profiles()
            top = await service.top_games_for(partner.value, limit=5) if partner is not None else []
        except ProfileLoadError as exc:
            await session.rollback()
            await interaction.followup.send(
                embed=error_embed("Partner profiles", str(exc)), ephemeral=True
            )
            return
        finally:
            await session.close()

        await interaction.followup.send(
            embed=partners_embed(profiles)
            if partner is None
            else _partner_games(top, partner.value),
            ephemeral=True,
        )

    partners_command.autocomplete("partner")(_partner_autocomplete)

    @bot.tree.command(name="match", description="Which partner profile fits this game")
    @app_commands.describe(universe_id="The Roblox universe id")
    async def match_command(interaction: discord.Interaction, universe_id: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        resolved = _resolve(universe_id)
        if resolved is None:
            await interaction.followup.send(
                embed=error_embed("Invalid id", f"`{universe_id}` is not a Roblox universe id."),
                ephemeral=True,
            )
            return

        container = container_of(interaction)
        session = container.session()
        service = container.partner_service(session)
        try:
            game = await GameRepository(session).get(resolved)
            if game is None:
                payload: dict[str, Any] = {"universe_id": resolved, "matches": []}
                name, url = f"Universe {resolved}", None
            else:
                payload = await service.match_payload(resolved)
                name = game.game_name or f"Universe {resolved}"
                url = game.roblox_url
        finally:
            await session.close()

        await interaction.followup.send(
            embed=match_embed(
                game_name=name,
                universe_id=resolved,
                matches=payload["matches"],
                roblox_url=url,
            ),
            ephemeral=True,
        )

    match_command.autocomplete("universe_id")(autocomplete)

    @bot.tree.command(
        name="shortlist", description="Shortlist a lead for a partner (no message is sent)"
    )
    @app_commands.describe(
        universe_id="The Roblox universe id",
        partner="Partner profile key",
        notes="Optional internal note",
    )
    async def shortlist_command(
        interaction: discord.Interaction, universe_id: str, partner: str, notes: str | None = None
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        resolved = _resolve(universe_id)
        if resolved is None:
            await interaction.followup.send(
                embed=error_embed("Invalid id", f"`{universe_id}` is not a Roblox universe id."),
                ephemeral=True,
            )
            return

        container = container_of(interaction)
        session = container.session()
        service = container.partner_service(session)
        try:
            block = await service.shortlist(resolved, partner.strip(), notes=notes)
            await session.commit()
        except Exception as exc:
            await session.rollback()
            log.exception("shortlist failed: %s", type(exc).__name__)
            await interaction.followup.send(
                embed=error_embed("Shortlist failed", f"{type(exc).__name__}: {exc}"[:400]),
                ephemeral=True,
            )
            return
        finally:
            await session.close()

        await interaction.followup.send(
            embed=Embed(
                title=f"⭐ Shortlisted for {block['partner']}",
                description=(
                    f"Stored on universe `{resolved}`.\n"
                    "ScoutForge has **not** contacted anyone — you still need to reach out."
                ),
                colour=COLOUR_NORMAL,
            ),
            ephemeral=True,
        )

    shortlist_command.autocomplete("universe_id")(autocomplete)
    shortlist_command.autocomplete("partner")(_partner_autocomplete)

    @bot.tree.command(name="outreach", description="Record partner outreach you performed")
    @app_commands.describe(
        universe_id="The Roblox universe id",
        partner="Partner profile key",
        action="What happened",
        method="How you reached them",
        reference="Link or message reference",
        notes="Optional note",
    )
    async def outreach_command(
        interaction: discord.Interaction,
        universe_id: str,
        partner: str,
        action: app_commands.Choice[str],
        method: str,
        reference: str | None = None,
        notes: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        resolved = _resolve(universe_id)
        if resolved is None:
            await interaction.followup.send(
                embed=error_embed("Invalid id", f"`{universe_id}` is not a Roblox universe id."),
                ephemeral=True,
            )
            return

        try:
            status = PartnerOutreach(action.value)
        except ValueError:
            await interaction.followup.send(
                embed=error_embed("Unknown action", f"`{action.value}` is not a valid action."),
                ephemeral=True,
            )
            return

        container = container_of(interaction)
        session = container.session()
        service = container.partner_service(session)
        try:
            if status is PartnerOutreach.CONTACTED:
                block = await service.record_contact(
                    resolved,
                    partner.strip(),
                    contact_method=method.strip(),
                    contact_reference=reference,
                    author_id=interaction.user.id,
                    author_name=str(interaction.user),
                    notes=notes,
                )
            else:
                block = await service.set_outreach_status(
                    resolved, partner.strip(), status, notes=notes
                )
            await session.commit()
        except Exception as exc:
            await session.rollback()
            log.exception("outreach failed: %s", type(exc).__name__)
            await interaction.followup.send(
                embed=error_embed("Outreach failed", f"{type(exc).__name__}: {exc}"[:400]),
                ephemeral=True,
            )
            return
        finally:
            await session.close()

        embed = Embed(
            title=f"🤝 {block['partner']} — {block['status']}",
            colour=COLOUR_NORMAL,
            description=(
                f"Recorded on universe `{resolved}` by {interaction.user}.\n"
                f"Logged: **{block['status']}** · via **{method}**"
            ),
        )
        if block.get("contact_reference"):
            embed.add_field(
                name="Reference", value=str(block["contact_reference"])[:1000], inline=False
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

    outreach_command.autocomplete("universe_id")(autocomplete)
    outreach_command.autocomplete("partner")(_partner_autocomplete)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _resolve(value: str) -> int | None:
    text = (value or "").strip()
    if text.isdigit():
        resolved = int(text)
        return resolved if resolved > 0 else None
    return None


def _partner_games(rows: list[dict[str, Any]], partner_key: str) -> Embed:
    embed = Embed(
        title=f"🤝 Best fits for {partner_key}",
        description=(
            "\n".join(
                f"• [`{row['universe_id']}`](https://www.roblox.com/games/{row['universe_id']}) "
                f"— {row['score']:.0f}/100"
                + (f" · {row['revenue_share']}" if row.get("revenue_share") else "")
                + (f"\n  ↳ {row['reasons'][0]}" if row.get("reasons") else "")
                for row in rows
            )
            or "No games match this profile yet."
        ),
        colour=COLOUR_NORMAL,
    )
    embed.set_footer(text="ScoutForge ranks fits; outreach is always manual")
    return embed


async def _partner_autocomplete(
    interaction: discord.Interaction, current: app_commands.Choice[str]
) -> list[app_commands.Choice[str]]:
    container = container_of(interaction)
    session = container.session()
    query = (current.value or "").strip().lower()
    try:
        profiles = await container.partner_service(session).list_profiles()
    except Exception as exc:
        log.warning("partner autocomplete failed: %s", type(exc).__name__)
        return []
    finally:
        await session.close()

    choices = [
        app_commands.Choice(
            name=str(p.get("name") or p.get("key"))[:100], value=str(p.get("key"))[:100]
        )
        for p in profiles
    ]
    if not query:
        return choices
    return [c for c in choices if query in c.name.lower() or query in c.value.lower()]


__all__ = ["register"]
