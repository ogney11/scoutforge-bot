"""Shared Discord embed styling.

One visual language for every message:

* NORMAL / WARM leads - ScoutForge blue
* HOT leads - amber/red gradient with a distinct title and a role mention
* EXTREME leads - magenta "🚨" treatment so it is impossible to miss

Nothing here fetches data; embeds are pure functions of their inputs, which
keeps them trivial to unit test.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from discord import Colour, Embed, Member, User

from scoutforge.enums import AlertLevel, LeadStatus
from scoutforge.utils import format_compact, format_pct, relative_time, utcnow

# ---------------------------------------------------------------------------
# Palette
# ---------------------------------------------------------------------------
COLOUR_NORMAL = Colour.blurple()
COLOUR_WARM = Colour.from_str("#F5A623")
COLOUR_HOT = Colour.from_str("#ED4245")
COLOUR_EXTREME = Colour.from_str("#EB459E")
COLOUR_ERROR = Colour.from_str("#8B0000")

LEVEL_COLOURS: dict[AlertLevel, Colour] = {
    AlertLevel.NORMAL: COLOUR_NORMAL,
    AlertLevel.WARM: COLOUR_WARM,
    AlertLevel.HOT: COLOUR_HOT,
    AlertLevel.EXTREME: COLOUR_EXTREME,
}

LEVEL_EMOJI: dict[AlertLevel, str] = {
    AlertLevel.NORMAL: "🔎",
    AlertLevel.WARM: "🌤",
    AlertLevel.HOT: "🔥",
    AlertLevel.EXTREME: "🚨",
}

STATUS_EMOJI: dict[LeadStatus, str] = {
    LeadStatus.NEW: "🆕",
    LeadStatus.RESEARCHED: "🔎",
    LeadStatus.CONTACTED: "✉️",
    LeadStatus.REPLIED: "💬",
    LeadStatus.INTERESTED: "⭐",
    LeadStatus.SENT_TO_WORLDENT: "🏢",
    LeadStatus.NEGOTIATING: "🤝",
    LeadStatus.DEAL: "🎉",
    LeadStatus.REJECTED: "❌",
    LeadStatus.NOT_INTERESTED: "🚫",
}

FOOTER = "ScoutForge • human-in-the-loop scouting • score is not a success prediction"


def level_emoji(level: AlertLevel | str | None) -> str:
    if level is None:
        return LEVEL_EMOJI[AlertLevel.NORMAL]
    try:
        return LEVEL_EMOJI[AlertLevel(level)]
    except (ValueError, KeyError):
        return LEVEL_EMOJI[AlertLevel.NORMAL]


def level_colour(level: AlertLevel | str | None) -> Colour:
    if level is None:
        return COLOUR_NORMAL
    try:
        return LEVEL_COLOURS[AlertLevel(level)]
    except (ValueError, KeyError):
        return COLOUR_NORMAL


def _fmt_pct(ratio: float | None) -> str:
    if ratio is None:
        return "n/a"
    return f"{ratio * 100:.2f}%"


def _fmt_timestamp(value: datetime | None) -> str:
    if value is None:
        return "unknown"
    epoch = int(value.timestamp())
    return f"<t:{epoch}:f>"


def _score_bar(score: float, *, width: int = 10) -> str:
    filled = round(max(0.0, min(100.0, score)) / 100.0 * width)
    return "█" * filled + "░" * (width - filled)


# ---------------------------------------------------------------------------
# Lead embeds
# ---------------------------------------------------------------------------
def lead_embed(
    *,
    game: Any,
    score: float | None = None,
    level: AlertLevel | None = None,
    growth: dict[str, Any] | None = None,
    status: LeadStatus | None = None,
    description_limit: int = 200,
    show_notes: bool = False,
    notes: Sequence[Any] = (),
) -> Embed:
    """Build the standard lead embed.

    Args:
        game: A :class:`~scoutforge.database.models.Game` row.
        score: Current Scout Score, if known.
        level: Alert level, if known.
        growth: Output of :meth:`GrowthMetrics.to_dict`.
        status: CRM status override (defaults to the game's own status).
        description_limit: Max characters of the game description to show.
        show_notes: Append the most recent notes.
        notes: Notes to render when ``show_notes`` is set.
    """
    level = level or (
        AlertLevel(game.current_level) if getattr(game, "current_level", None) else None
    )
    status = status or (
        LeadStatus(game.status) if getattr(game, "status", None) else LeadStatus.NEW
    )
    score = score if score is not None else getattr(game, "current_score", None)
    growth = growth or {}

    embed = Embed(
        title=f"{level_emoji(level)} {game.game_name or f'Universe {game.universe_id}'}",
        colour=level_colour(level),
        url=game.roblox_url,
    )

    if getattr(game, "thumbnail_url", None):
        embed.set_thumbnail(url=game.thumbnail_url)

    embed.add_field(name="🎮 Game", value=game.game_name or "Unknown", inline=True)
    embed.add_field(name="👥 CCU", value=format_compact(game.current_ccu), inline=True)
    embed.add_field(
        name="👀 Lifetime Visits",
        value=f"{game.lifetime_visits:,}" if game.lifetime_visits else "n/a",
        inline=True,
    )
    embed.add_field(name="📊 CCU/Visits", value=_fmt_pct(game.ccu_to_visits_ratio), inline=True)
    embed.add_field(name="📈 Growth", value=_format_growth(growth), inline=True)
    embed.add_field(name="🔥 Scout Score", value=_format_score(score, level), inline=True)

    creator_line = game.creator_name or "unknown"
    if game.creator_type:
        creator_line += f" ({game.creator_type})"
    group_line = (
        f"{game.group_name} (`{game.group_id}`)"
        if game.group_name
        else (f"`{game.group_id}`" if game.group_id else "n/a")
    )
    embed.add_field(name="Creator", value=creator_line, inline=True)
    embed.add_field(name="Group", value=group_line, inline=True)
    embed.add_field(name="Universe ID", value=f"`{game.universe_id}`", inline=True)

    if game.peak_ccu:
        embed.add_field(name="Peak CCU", value=format_compact(game.peak_ccu), inline=True)
    if game.favorites:
        embed.add_field(name="⭐ Favorites", value=f"{game.favorites:,}", inline=True)

    links = [f"[Roblox]({game.roblox_url})"]
    if game.public_discord_url:
        links.append(f"[Discord]({game.public_discord_url})")
    if game.creator_url:
        links.append(f"[Creator]({game.creator_url})")
    embed.add_field(name="Links", value=" • ".join(links), inline=False)

    embed.add_field(
        name=f"{STATUS_EMOJI[status]} Status",
        value=f"**{status.value}**",
        inline=True,
    )
    embed.add_field(
        name="First detected",
        value=f"{_fmt_timestamp(game.first_seen_at)} ({relative_time(game.first_seen_at)})",
        inline=True,
    )
    embed.add_field(name="Last seen", value=relative_time(game.last_seen_at), inline=True)

    description = (getattr(game, "description", None) or "").strip()
    if description:
        if len(description) > description_limit:
            description = description[: description_limit - 1].rstrip() + "…"
        embed.description = f"{game.game_name or ''}\n{description}".strip()

    missing = getattr(game, "missing_metrics", None) or []
    if missing:
        embed.add_field(
            name="⚠️ Unavailable metrics",
            value=", ".join(str(m) for m in missing),
            inline=False,
        )

    if show_notes and notes:
        rendered = "\n".join(
            f"• {getattr(note, 'author_name', None) or 'scout'}: {note.body[:120]}"
            for note in notes[:3]
        )
        embed.add_field(name="🗒 Notes", value=rendered[:1000], inline=False)

    embed.set_footer(text=FOOTER)
    return embed


def _format_growth(growth: dict[str, Any]) -> str:
    windows = growth.get("windows") or {}
    for hours in ("24", "6", "1"):
        window = windows.get(hours)
        if not window:
            continue
        return f"{format_pct(window.get('pct_change'))} / {hours}h"
    return "no history yet"


def _format_score(score: float | None, level: AlertLevel | None) -> str:
    if score is None:
        return "unscored"
    suffix = f" `{level.value}`" if level else ""
    return f"**{score:.0f}/100**{suffix}\n{_score_bar(score)}"


def alert_embed(payload: dict[str, Any], *, mention: str | None = None) -> Embed:
    """Build the HOT / EXTREME alert embed.

    ``payload`` is the dict produced by
    :meth:`scoutforge.services.alert_service.AlertPayload.to_dict`.
    """
    raw_level = payload.get("level", AlertLevel.HOT)
    try:
        level = AlertLevel(raw_level)
    except ValueError:
        level = AlertLevel.HOT

    title_prefix = "🚨 EXTREME" if level is AlertLevel.EXTREME else "🔥 HOT"
    embed = Embed(
        title=f"{title_prefix} ROBLOX LEAD",
        description=f"**{payload.get('game_name', 'Unknown game')}**",
        colour=level_colour(level),
        url=payload.get("roblox_url"),
    )

    if payload.get("thumbnail_url"):
        embed.set_thumbnail(url=payload["thumbnail_url"])

    ccu = payload.get("current_ccu")
    visits = payload.get("lifetime_visits")
    ratio = payload.get("ccu_to_visits_ratio")
    growth_pct = payload.get("growth_pct")
    window = payload.get("growth_window_hours")

    embed.add_field(name="👥 CCU", value=f"{ccu:,}" if ccu else "n/a", inline=True)
    embed.add_field(
        name="👀 Lifetime Visits", value=f"{visits:,}" if visits else "n/a", inline=True
    )
    embed.add_field(name="📊 CCU/Visits", value=_fmt_pct(ratio), inline=True)
    growth_text = (
        f"{format_pct(growth_pct)} / {window}h"
        if growth_pct is not None and window
        else "no history yet"
    )
    embed.add_field(name="📈 Growth", value=growth_text, inline=True)
    embed.add_field(
        name="🔥 Scout Score", value=_format_score(payload.get("score"), level), inline=True
    )
    embed.add_field(name="Creator", value=payload.get("creator_name") or "unknown", inline=True)

    links = [f"[Roblox]({payload.get('roblox_url')})"] if payload.get("roblox_url") else []
    if payload.get("discord_url"):
        links.append(f"[Discord]({payload['discord_url']})")
    if links:
        embed.add_field(name="Links", value=" • ".join(links), inline=False)

    embed.add_field(
        name="First detected",
        value=_fmt_timestamp(_parse_iso(payload.get("first_seen_at"))),
        inline=False,
    )

    reasons = payload.get("reasons") or []
    if reasons:
        embed.add_field(
            name="Why",
            value="\n".join(f"• {reason}" for reason in reasons[:5])[:1024],
            inline=False,
        )

    caveats = payload.get("caveats") or []
    if caveats:
        embed.add_field(
            name="Limitations", value="\n".join(f"• {c}" for c in caveats[:3])[:1024], inline=False
        )

    partner_block = _format_partners(payload.get("partners") or [])
    if partner_block:
        embed.add_field(name="🤝 Partner matches", value=partner_block, inline=False)

    embed.set_footer(text=FOOTER)
    if mention:
        embed.set_author(name=mention, icon_url=None)
    return embed


def _format_partners(partners: Sequence[dict[str, Any]], *, limit: int = 3) -> str:
    """Render matched partners with their score and first reason.

    Only eligible matches are rendered: the exclusions live on the lead detail
    page and in ``/match``, so an alert stays readable.
    """
    eligible = [partner for partner in partners if partner.get("eligible", True)]
    if not eligible:
        return ""
    lines: list[str] = []
    for partner in eligible[:limit]:
        name = partner.get("partner_name") or partner.get("partner_key") or "?"
        score = partner.get("score")
        share = partner.get("revenue_share")
        header = f"**{name}**"
        if score is not None:
            header += f" • {float(score):.0f}/100"
        if share:
            header += f" • {share}"
        reasons = [str(reason) for reason in (partner.get("reasons") or []) if reason]
        lines.append(header + (f"\n↳ {reasons[0]}" if reasons else ""))
    return "\n".join(lines)[:1024]


def _parse_iso(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Operational embeds
# ---------------------------------------------------------------------------
def scan_result_embed(result: Any) -> Embed:
    """Embed for the result of ``/scan``."""
    duration = result.finished_at and (result.finished_at - result.started_at).total_seconds()
    embed = Embed(
        title="🔄 Scan complete",
        colour=COLOUR_NORMAL if result.status.value == "SUCCESS" else COLOUR_WARM,
    )
    embed.add_field(name="Candidates found", value=str(result.candidates_found), inline=True)
    embed.add_field(name="Accepted", value=str(result.games_accepted), inline=True)
    embed.add_field(name="Rejected", value=str(result.games_rejected), inline=True)
    embed.add_field(name="Newly discovered", value=str(result.games_discovered), inline=True)
    embed.add_field(name="Updated", value=str(result.games_updated), inline=True)
    embed.add_field(name="Snapshots", value=str(result.snapshots_written), inline=True)
    embed.add_field(name="Scores computed", value=str(result.scores_computed), inline=True)
    embed.add_field(name="Alerts sent", value=str(result.alerts_sent), inline=True)
    embed.add_field(name="Alerts suppressed", value=str(result.alerts_suppressed), inline=True)
    embed.add_field(name="API errors", value=str(result.api_errors), inline=True)
    embed.add_field(name="Duration", value=f"{duration:.1f}s" if duration else "n/a", inline=True)
    embed.add_field(name="Run ID", value=f"`{result.run_id}`", inline=True)

    if result.rejection_reasons:
        lines = "\n".join(
            f"`{reason}`: {count}"
            for reason, count in sorted(result.rejection_reasons.items(), key=lambda kv: -kv[1])
        )
        embed.add_field(name="Rejection reasons", value=lines[:1024], inline=False)
    if result.source_stats:
        lines = "\n".join(
            f"`{name}`: {stats.get('candidates', 0)} candidates, {stats.get('pages', 0)} pages"
            + (f" — {stats['notes']}" if stats.get("notes") else "")
            for name, stats in result.source_stats.items()
        )
        embed.add_field(name="Sources", value=lines[:1024], inline=False)
    if result.error:
        embed.add_field(name="Error", value=result.error[:1000], inline=False)
    embed.set_footer(text=FOOTER)
    return embed


def stats_embed(stats: Any) -> Embed:
    """Embed for ``/stats``."""
    embed = Embed(title="📊 ScoutForge stats", colour=COLOUR_NORMAL)
    embed.add_field(name="Scans completed", value=str(stats.scans_completed), inline=True)
    embed.add_field(name="Scans (24h)", value=str(stats.scans_24h), inline=True)
    embed.add_field(name="Candidates processed", value=f"{stats.candidates_found:,}", inline=True)
    embed.add_field(name="Games discovered", value=f"{stats.games_discovered:,}", inline=True)
    embed.add_field(name="Games tracked", value=f"{stats.games_tracked:,}", inline=True)
    embed.add_field(name="Snapshots", value=f"{stats.snapshots_written:,}", inline=True)
    embed.add_field(name="API requests", value=f"{stats.http_requests:,}", inline=True)
    embed.add_field(name="Rate limited", value=str(stats.rate_limited), inline=True)
    embed.add_field(name="API errors", value=str(stats.api_errors), inline=True)
    embed.add_field(
        name="Avg scan duration",
        value=f"{stats.average_scan_duration_ms / 1000:.1f}s",
        inline=True,
    )
    embed.add_field(
        name="Last scan",
        value=relative_time(stats.last_scan_at),
        inline=True,
    )

    alerts = stats.alerts_by_level or {}
    embed.add_field(
        name="Alerts",
        value=(
            f"HOT: {alerts.get('HOT', 0)}\n"
            f"EXTREME: {alerts.get('EXTREME', 0)}\n"
            f"WARM: {alerts.get('WARM', 0)}\n"
            f"Suppressed by cooldown: {stats.alerts_suppressed}"
        ),
        inline=True,
    )
    embed.add_field(
        name="Leads by level",
        value=(
            f"🔥 HOT: {stats.hot_leads}\n"
            f"🚨 EXTREME: {stats.extreme_leads}\n"
            f"🌤 WARM: {stats.warm_leads}"
        ),
        inline=True,
    )
    statuses = stats.leads_by_status or {}
    embed.add_field(
        name="Pipeline",
        value="\n".join(f"{status}: {count}" for status, count in sorted(statuses.items()))[:1024]
        or "no leads yet",
        inline=False,
    )
    embed.set_footer(text=FOOTER)
    return embed


def error_embed(title: str, message: str) -> Embed:
    embed = Embed(title=f"⚠️ {title}", description=message[:4000], colour=COLOUR_ERROR)
    embed.set_footer(text=FOOTER)
    return embed


def partners_embed(profiles: Sequence[dict[str, Any]]) -> Embed:
    """Embed for ``/partners``: the active deal terms, one field per partner."""
    embed = Embed(
        title="🤝 Partner profiles",
        description=(
            "Every game is ranked against these profiles. Deal terms are editable "
            "(dashboard → Settings, or `PARTNER_PROFILES_FILE`)."
        ),
        colour=COLOUR_NORMAL,
    )
    if not profiles:
        embed.description = "No partner profiles are configured."
        embed.set_footer(text=FOOTER)
        return embed

    for profile in profiles[:10]:
        embed.add_field(
            name=_partner_title(profile),
            value=_partner_terms(profile) or "no criteria configured",
            inline=False,
        )
    embed.set_footer(text=FOOTER)
    return embed


def match_embed(
    *,
    game_name: str,
    universe_id: int,
    matches: Sequence[dict[str, Any]],
    roblox_url: str | None = None,
) -> Embed:
    """Embed for ``/match``: who this game fits, and why the rest said no."""
    embed = Embed(
        title=f"🤝 Partner matches — {game_name}",
        colour=COLOUR_NORMAL,
        url=roblox_url,
    )
    eligible = [m for m in matches if m.get("eligible")]
    declined = [m for m in matches if not m.get("eligible")]

    if not matches:
        embed.description = (
            "No partner matches stored for this game yet. They are computed "
            "automatically on the next discovery scan."
        )
    elif not eligible:
        embed.description = "No partner profile currently fits this game."
    else:
        embed.description = "\n".join(_match_line(match) for match in eligible[:6])[:2048]

    if declined:
        lines = [
            f"• **{m.get('partner_name') or m.get('partner_key')}** — "
            + (
                "; ".join(str(x) for x in (m.get("mismatches") or m.get("exclusions") or []))
                or "no fit"
            )
            for m in declined[:6]
        ]
        embed.add_field(name="Not a fit", value="\n".join(lines)[:1024], inline=False)

    embed.add_field(name="Universe ID", value=f"`{universe_id}`", inline=True)
    embed.set_footer(text=FOOTER)
    return embed


def _match_line(match: dict[str, Any]) -> str:
    name = match.get("partner_name") or match.get("partner_key") or "?"
    score = match.get("score")
    header = f"**{name}**"
    if score is not None:
        header += f" — {float(score):.0f}/100"
    share = match.get("revenue_share")
    if share:
        header += f" — {share}"
    reasons = [str(reason) for reason in (match.get("reasons") or []) if reason]
    return header + ("\n" + "\n".join(f"↳ {reason}" for reason in reasons[:3]) if reasons else "")


def _partner_title(profile: dict[str, Any]) -> str:
    name = profile.get("name") or profile.get("key") or "partner"
    matched = profile.get("matched_games")
    title = f"{name}"
    if matched is not None:
        title += f" • {matched} matched"
    if not profile.get("enabled", True):
        title += " • disabled"
    return title


def _partner_terms(profile: dict[str, Any]) -> str:
    lines: list[str] = []
    share = profile.get("revenue_share_note") or ""
    if not share:
        low = profile.get("revenue_share_min_pct")
        high = profile.get("revenue_share_max_pct")
        if low and high:
            share = f"{low:g}-{high:g}%"
        elif low:
            share = f"{low:g}%"
        elif high:
            share = f"up to {high:g}%"
    if share:
        lines.append(f"**Share:** {share}")
    if profile.get("max_game_value_usd"):
        lines.append(f"**Ceiling:** ${profile['max_game_value_usd']:,.0f}")
    stages = profile.get("preferred_stages") or []
    if stages and "any" not in stages:
        lines.append(f"**Stage:** {', '.join(stages)}")
    if profile.get("preferred_ccu_min"):
        lines.append(f"**CCU ≥** {profile['preferred_ccu_min']:,}")
    if profile.get("preferred_visits_max"):
        lines.append(f"**Visits ≤** {profile['preferred_visits_max']:,}")
    if profile.get("min_engagement_pct"):
        lines.append(f"**Engagement ≥** {profile['min_engagement_pct']:g}%")
    excluded = list(profile.get("exclude_risk") or []) + list(profile.get("extra_exclusions") or {})
    if excluded:
        lines.append("**Excludes:** " + ", ".join(sorted({str(e) for e in excluded})))
    if profile.get("notes"):
        lines.append(f"_{str(profile['notes'])[:400]}_")
    return "\n".join(lines)


def help_embed() -> Embed:
    embed = Embed(
        title="ScoutForge — Roblox scouting intelligence",
        description=(
            "ScoutForge watches public Roblox data for games matching your criteria, "
            "scores them transparently and pings you when one gets hot.\n\n"
            "**Contacting developers is always manual** — the bot only tracks and reminds."
        ),
        colour=COLOUR_NORMAL,
    )
    embed.add_field(
        name="Discovery",
        value=(
            "`/scan` — run a discovery scan now\n"
            "`/search <query>` — find tracked games by name or creator\n"
            "`/hot` — top HOT/EXTREME opportunities right now\n"
            "`/leads` — browse the pipeline\n"
            "`/lead <id>` — full detail for one game"
        ),
        inline=False,
    )
    embed.add_field(
        name="Partners",
        value=(
            "`/partners` — deal terms of every active partner profile\n"
            "`/match <game>` — who this game fits and who passed"
        ),
        inline=False,
    )
    embed.add_field(
        name="CRM",
        value=(
            "`/status <lead> <status>` — move a lead along the pipeline\n"
            "`/note <lead> <text>` — attach a scouting note\n"
            "`/history <lead>` — status + note timeline\n"
            "`/assign <lead>` — assign a lead to a scout"
        ),
        inline=False,
    )
    embed.add_field(
        name="Control",
        value=(
            "`/alerts status` — current alert configuration\n"
            "`/alerts toggle <on|off>` — turn alerting on or off\n"
            "`/alerts threshold hot=<score>` — set the HOT score\n"
            "`/alerts cooldown hot_hours=<h>` — set the HOT cooldown\n"
            "`/alerts history` — recent alerts\n"
            "`/stats` — scanning and alerting telemetry"
        ),
        inline=False,
    )
    embed.set_footer(text=FOOTER)
    return embed


def mention_user(user: User | Member | None) -> str:
    """Render a user mention string, or an empty string when absent."""
    return user.mention if user else ""


def role_mention(role: Any | None) -> str:
    """Render a role mention string, or an empty string when absent."""
    return role.mention if role is not None else ""


def utc_stamp() -> str:
    return utcnow().isoformat(timespec="seconds")


__all__ = [
    "COLOUR_ERROR",
    "COLOUR_EXTREME",
    "COLOUR_HOT",
    "COLOUR_NORMAL",
    "COLOUR_WARM",
    "FOOTER",
    "LEVEL_COLOURS",
    "LEVEL_EMOJI",
    "STATUS_EMOJI",
    "alert_embed",
    "error_embed",
    "help_embed",
    "lead_embed",
    "level_colour",
    "level_emoji",
    "match_embed",
    "mention_user",
    "partners_embed",
    "role_mention",
    "scan_result_embed",
    "stats_embed",
]
