"""Interactive lead action buttons.

Buttons are a convenience layer over the same service methods the slash commands
use, so a mis-click can never bypass status validation, permissions or the
audit trail. Every interaction answers ephemerally, and only the scout who
opened the card may use it.
"""

from __future__ import annotations

from functools import partial
from typing import Any

import discord
from discord import ButtonStyle, Embed

from scoutforge.bot.embeds.builder import STATUS_EMOJI, lead_embed
from scoutforge.config.logging import get_logger, log_event
from scoutforge.enums import LeadStatus
from scoutforge.services.container import Container
from scoutforge.services.crm_service import (
    InvalidTransitionError,
    LeadService,
    UnknownStatusError,
    allowed_transitions,
)
from scoutforge.utils import format_compact, relative_time

log = get_logger(__name__)

#: Buttons offered on every lead card, in priority order.
PRIMARY_ACTIONS: tuple[LeadStatus, ...] = (
    LeadStatus.RESEARCHED,
    LeadStatus.CONTACTED,
    LeadStatus.REPLIED,
    LeadStatus.INTERESTED,
    LeadStatus.NOT_INTERESTED,
)


def _style_for(status: LeadStatus) -> ButtonStyle:
    if status in (LeadStatus.DEAL, LeadStatus.INTERESTED):
        return ButtonStyle.success
    if status in (LeadStatus.REJECTED, LeadStatus.NOT_INTERESTED):
        return ButtonStyle.danger
    if status in (LeadStatus.NEGOTIATING, LeadStatus.SENT_TO_WORLDENT):
        return ButtonStyle.primary
    return ButtonStyle.secondary


class StatusButton(discord.ui.Button["LeadActionsView"]):
    """A button whose callback is bound to a specific :class:`LeadStatus`."""

    def __init__(self, *, universe_id: int, target: LeadStatus, **kwargs: Any) -> None:
        kwargs.setdefault("style", _style_for(target))
        kwargs.setdefault("label", f"{STATUS_EMOJI.get(target, '•')} {target.value}")
        kwargs.setdefault("custom_id", f"lead:{universe_id}:{target.value}")
        super().__init__(**kwargs)
        self.universe_id = universe_id
        self.target = target

    @property
    def callback(self) -> Any:
        view = self._view
        if not isinstance(view, LeadActionsView):  # pragma: no cover - defensive
            return None
        return partial(view.on_status_click, self.target)


class LeadActionsView(discord.ui.View):
    """Status buttons rendered under a lead embed."""

    def __init__(
        self,
        *,
        universe_id: int,
        status: LeadStatus,
        roblox_url: str,
        owner_id: int,
        growth: dict[str, Any] | None = None,
        timeout: float = 900.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self.universe_id = universe_id
        self.current_status = status
        self.roblox_url = roblox_url
        self.owner_id = owner_id
        self.growth = growth or {}

        permitted = allowed_transitions(status)
        offered = [target for target in PRIMARY_ACTIONS if target in permitted]
        if not offered:
            offered = [LeadStatus.REJECTED] if LeadStatus.REJECTED in permitted else []

        for target in offered[:5]:
            self.add_item(StatusButton(universe_id=universe_id, target=target, row=0))

        self.add_item(
            discord.ui.Button(
                style=ButtonStyle.link,
                label="Open on Roblox",
                url=roblox_url,
                row=1,
            )
        )

    # -------------------------------------------------------------- guards
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Only the scout who opened the card may act on it."""
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the scout who opened this card can change its status.", ephemeral=True
            )
            return False
        return True

    # ------------------------------------------------------------ handlers
    async def on_status_click(self, target: LeadStatus, interaction: discord.Interaction) -> None:
        """Apply a status change and re-render the card."""
        await interaction.response.defer(ephemeral=True, thinking=True)
        container: Container = interaction.client.container  # type: ignore[attr-defined]
        session = container.session()
        service = LeadService(session)
        try:
            game, entry = await service.change_status(
                self.universe_id,
                target,
                actor_id=interaction.user.id,
                actor_name=str(interaction.user),
                reason=f"button:{target.value}",
            )
            await session.commit()
        except (InvalidTransitionError, UnknownStatusError, LookupError) as exc:
            await session.rollback()
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return
        finally:
            await session.close()

        self.current_status = LeadStatus(game.status)
        embed = lead_embed(game=game, growth=self.growth, status=self.current_status)
        embed.add_field(
            name="✅ Updated",
            value=f"{interaction.user} set status to **{target.value}**",
            inline=False,
        )
        view = LeadActionsView(
            universe_id=self.universe_id,
            status=self.current_status,
            roblox_url=game.roblox_url or "",
            owner_id=self.owner_id,
            growth=self.growth,
        )
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        log_event(
            log,
            20,
            "CRM_BUTTON",
            universe=self.universe_id,
            status=target.value,
            by=interaction.user.id,
            entry_id=entry.id,
        )


def mini_status_embed(game: Any) -> Embed:
    """Small embed used when confirming a status change."""
    status = LeadStatus(game.status)
    embed = Embed(
        title=f"{STATUS_EMOJI[status]} {game.game_name}",
        description=f"Status is now **{status.value}**",
        colour=discord.Colour.blurple(),
    )
    embed.add_field(name="CCU", value=format_compact(game.current_ccu), inline=True)
    embed.add_field(
        name="Score",
        value=f"{game.current_score:.0f}" if game.current_score else "n/a",
        inline=True,
    )
    embed.add_field(name="Last seen", value=relative_time(game.last_seen_at), inline=True)
    return embed


__all__ = ["LeadActionsView", "StatusButton", "mini_status_embed"]
