from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import get_settings
from app.db.session import SessionLocal, init_db
from app.handlers import setup_routers
from app.logging_setup import setup_logging
from app.services.reminders import send_evening_recaps, send_morning_reminders
from app.services.archive import backfill_archive
from app.services.strength_levels import ensure_standards, sync_standards_from_catalog

logger = logging.getLogger("gymflex")


async def main() -> None:
    settings = get_settings()
    setup_logging(settings)
    await init_db()
    async with SessionLocal() as session:
        added = await backfill_archive(session)
        if added:
            logger.info("Exercise archive backfill: +%s", added)
        seeded = await ensure_standards(session)
        if seeded:
            logger.info("Strength standards seed: +%s", seeded)
        synced = await sync_standards_from_catalog(session)
        if synced:
            logger.info("Strength standards catalog sync: +%s", synced)

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(setup_routers())

    scheduler = AsyncIOScheduler(timezone=settings.timezone)
    scheduler.add_job(
        send_morning_reminders,
        "cron",
        minute=0,
        args=[bot, settings],
        id="morning_reminder",
        replace_existing=True,
    )
    scheduler.add_job(
        send_evening_recaps,
        "cron",
        minute=5,
        args=[bot, settings],
        id="evening_recap",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("Scheduler started (tz=%s)", settings.timezone)

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        me = await bot.get_me()
        logger.info("Bot polling as @%s (id=%s)", me.username, me.id)
        await dp.start_polling(bot)
    finally:
        logger.info("Shutting down")
        scheduler.shutdown(wait=False)
        await bot.session.close()


def run() -> None:
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Stopped by keyboard")
    except Exception:
        logging.getLogger("gymflex").exception("Fatal error, process exiting")
        sys.exit(1)


if __name__ == "__main__":
    run()
