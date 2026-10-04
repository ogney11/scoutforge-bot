"""Discord implementation of :class:`~scoutforge.services.alert_service.AlertDispatcher`.

Responsibilities are deliberately narrow: render the payload to an embed, post
it to the configured channel, mention the configured role/users. It owns no
scoring or cooldown logic, and it never sends anything but a scouting alert.
"""

from __future__ import annotations

from typing import Any, cast

import discord

from scoutforge.bot.embeds.builder import alert_embed
from scoutforge.config.logging import get_logger, log_event
from scoutforge.enums import AlertLevel
from scoutforge.services.alert_service import AlertDispatcher

log = get_logger(__name__)


class DiscordAlertDispatcher(AlertDispatcher):
    """Posts HOT/EXTREME alerts to the configured Discord channel."""

    def __init__(
        self,
        channel: discord.abc.Messageable | None,
        *,
        role: discord.Role | None = None,
        mention_user_ids: tuple[int, ...] = (),
        allowed_mentions: discord.AllowedMentions | None = None,
    ) -> None:
        self.channel = channel
        self.role = role
        self.mention_user_ids = mention_user_ids
        self.allowed_mentions = allowed_mentions or discord.AllowedMentions(
            # Snowflake ids are plain ints at runtime; discord.py's stub types
            # are stricter than the library itself.
            roles=cast("Any", [role.id]) if role is not None else [],
            users=cast("Any", list(mention_user_ids)),
            everyone=False,
            replied_user=False,
        )

    async def send_alert(self, payload: dict[str, Any]) -> int | None:
        """Post the alert. Returns the created message id, or ``None`` if unroutable."""
        if self.channel is None:
            log_event(log, 25, "ALERT_NO_CHANNEL", universe=payload.get("universe_id"))
            return None

        level = payload.get("level", AlertLevel.HOT.value)
        mention = self.build_mention(str(level))
        embed = alert_embed(payload, mention=None)

        content = mention or None
        try:
            message = await self.channel.send(
                content=content,
                embed=embed,
                allowed_mentions=self.allowed_mentions,
            )
        except discord.Forbidden as exc:
            log_event(log, 40, "ALERT_FORBIDDEN", error=str(exc)[:200])
            raise
        except discord.HTTPException as exc:
            log_event(log, 40, "ALERT_HTTP_ERROR", error=type(exc).__name__)
            raise

        log_event(
            log,
            20,
            "ALERT_SENT",
            universe=payload.get("universe_id"),
            alert_level=level,
            score=payload.get("score"),
            channel_id=getattr(self.channel, "id", None),
            message_id=message.id,
        )
        return message.id

    def build_mention(self, level: str) -> str:
        """Build the mention line (role on HOT, role + users on EXTREME)."""
        parts: list[str] = []
        if self.role is not None:
            parts.append(self.role.mention)
        if level == AlertLevel.EXTREME.value:
            parts.extend(f"<@{user_id}>" for user_id in self.mention_user_ids)
        return " ".join(parts)


__all__ = ["DiscordAlertDispatcher"]
