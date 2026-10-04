"""ScoutForge entrypoint.

Subcommands
-----------
``run``        start the Discord bot with the scan scheduler (default)
``worker``     run the discovery worker without Discord (headless scouting)
``sync-commands`` register slash commands and exit
``migrate``    apply database migrations
``seed-demo``  insert demo data so a fresh install is not empty
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from scoutforge import __version__
from scoutforge.config.logging import get_logger, log_event, setup_logging
from scoutforge.config.settings import Settings, get_settings

log = get_logger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scoutforge",
        description="ScoutForge - Roblox game scouting intelligence for Discord",
    )
    parser.add_argument("--version", action="version", version=f"scoutforge {__version__}")
    parser.add_argument(
        "command",
        nargs="?",
        default="run",
        choices=("run", "worker", "sync-commands", "migrate", "seed-demo"),
        help="What to do (default: run the bot)",
    )
    parser.add_argument("--log-level", default=None, help="Override LOG_LEVEL")
    parser.add_argument(
        "--no-scheduler", action="store_true", help="Do not start the scan scheduler"
    )
    parser.add_argument("--check", action="store_true", help="Validate config and exit")
    parser.add_argument(
        "--force",
        action="store_true",
        help="seed-demo only: seed even if the database looks like production",
    )
    return parser


#: Repository root, resolved once at import time (filesystem access on the
#: event loop is never acceptable).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent


async def _amigrate(settings: Settings) -> None:
    """Run alembic upgrade head programmatically.

    Works both from a source checkout (alembic.ini next to the package) and from
    an installed wheel, where only the migrations directory is on disk and the
    ini has to come from the working directory.
    """
    from alembic import command
    from alembic.config import Config

    script_location = _PROJECT_ROOT / "scoutforge" / "database" / "migrations"
    ini_path = _PROJECT_ROOT / "alembic.ini"
    if not ini_path.exists():
        ini_path = Path("alembic.ini")

    config = Config(str(ini_path))
    config.set_main_option("script_location", str(script_location))
    config.set_main_option("sqlalchemy.url", settings.database.database_url)
    await asyncio.to_thread(command.upgrade, config, "head")
    log_event(log, 20, "MIGRATIONS_APPLIED")


async def _ensure_schema(settings: Settings) -> None:
    """Create tables directly when migrations are not available (first run)."""
    from scoutforge.database.models import Base
    from scoutforge.database.session import connect_with_retry, create_engine

    engine = create_engine(settings.database)
    try:
        await connect_with_retry(engine, retries=settings.database.connect_retries)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        log_event(log, 20, "SCHEMA_READY")
    finally:
        await engine.dispose()


def _validate(settings: Settings) -> list[str]:
    """Return a list of configuration problems (empty when healthy)."""
    problems: list[str] = []
    if not settings.discord.token.get_secret_value():
        problems.append("DISCORD_TOKEN is not set")
    if not settings.database.database_url:
        problems.append("DATABASE_URL is not set")
    if settings.discord.alert_channel_id is None:
        problems.append("ALERT_CHANNEL_ID is not set - alerts cannot be delivered")
    if not settings.discovery.enabled_sources:
        problems.append("ENABLED_SOURCES is empty - no discovery will happen")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    settings = get_settings()
    if args.log_level:
        settings.log_level = args.log_level.upper()
    setup_logging(settings)

    if args.check:
        problems = _validate(settings)
        if problems:
            for problem in problems:
                print(f"✗ {problem}")
            return 1
        print("✓ configuration looks valid")
        return 0

    match args.command:
        case "migrate":
            asyncio.run(_amigrate(settings))
            return 0
        case "run":
            return asyncio.run(_run_bot(settings, with_scheduler=not args.no_scheduler))
        case "worker":
            return asyncio.run(_run_worker_only(settings))
        case "sync-commands":
            return asyncio.run(_sync_commands(settings))
        case "seed-demo":
            return asyncio.run(_seed_demo(settings, force=args.force))
        case _:  # pragma: no cover - argparse validates the choice
            parser.print_help()
            return 2


async def _run_bot(settings: Settings, *, with_scheduler: bool = True) -> int:
    from scoutforge.bot.client import create_bot

    problems = _validate(settings)
    if problems:
        for problem in problems:
            log_event(log, 40, "CONFIG_PROBLEM", detail=problem)

    bot = create_bot(settings)
    if not with_scheduler:
        bot.settings.worker.enabled = False

    await bot.run_forever()
    return 0


async def _run_worker_only(settings: Settings) -> int:
    from scoutforge.bot.scheduler import ScanScheduler
    from scoutforge.services.container import Container
    from scoutforge.services.discovery_service import DiscoveryWorker

    await _ensure_schema(settings)
    container = Container(settings=settings)
    worker = DiscoveryWorker(container)
    scheduler = ScanScheduler(worker, settings.worker)
    log_event(log, 20, "WORKER_ONLY_START", interval=settings.worker.scan_interval_seconds)
    scheduler.start(run_on_startup=True)
    try:
        # Nothing to poll for: block until the process is cancelled or signalled.
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):  # pragma: no cover - interactive
        pass
    finally:
        await scheduler.shutdown()
        await container.aclose()
        if container.engine is not None:
            await container.engine.dispose()
    return 0


async def _sync_commands(settings: Settings) -> int:
    from scoutforge.bot.client import create_bot

    bot = create_bot(settings)
    async with bot:
        await bot.login(settings.discord.token_value)
        count = await bot.sync_commands()
    if bot.container.engine is not None:
        await bot.container.engine.dispose()
    print(f"✓ registered {count} commands")
    return 0


async def _seed_demo(settings: Settings, *, force: bool = False) -> int:  # pragma: no cover
    from scoutforge.scripts.seed_demo import seed_demo

    # The guard runs inside ``seed_demo``, so it is checked *before* the schema is
    # touched: refusing must never create tables in the database it just judged
    # to be production.
    count = await seed_demo(settings, force=force)
    print(f"✓ seeded {count} demo leads")
    return 0


if __name__ == "__main__":
    sys.exit(main())
