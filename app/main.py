from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import get_settings
from app.db.session import init_db
from app.handlers import setup_routers
from app.services.reminders import send_evening_recaps, send_morning_reminders

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("gymflex")


async def main() -> None:
    settings = get_settings()
    await init_db()

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
        logger.info("Bot polling…")
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
