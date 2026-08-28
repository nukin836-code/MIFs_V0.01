import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.types import InlineQuery, InlineQueryResultCachedAudio


MIFS_DATABASE = [
    {
        "id": "1",
        "title": "Звук 1",
        "file_id": "CQACAgIAAxkBAAEuHO9qkZ03pi_mLbWFOsWW01ynB2cZUAACQQ0AAu5m4EjwVpcEQ4qmpz0E",
        "tags": "окс миф 1",
    },
    {
        "id": "2",
        "title": "Звук 2",
        "file_id": "CQACAgIAAxkBAAEuHPFqkZ0352jEAWpootn6YibYFZpZowACYGIAAt-jkEgtnX7stJl5Lj0E",
        "tags": "мем 2",
    },
    {
        "id": "3",
        "title": "Звук 3",
        "file_id": "CQACAgIAAxkBAAEuHPJqkZ031jPFF2-6TwFG4V8JEyKEAgACDUkAAhDEgUqWSpFk4iZf9z0E",
        "tags": "мем 3",
    },
    {
        "id": "4",
        "title": "Звук 4",
        "file_id": "CQACAgIAAxkBAAEuHPNqkZ03lpuLxHkyZ92-BqzLJ45uNAACtQoAAmHC0UpMkDlDJZnKTT0E",
        "tags": "мем 4",
    },
    {
        "id": "5",
        "title": "Звук 5",
        "file_id": "CQACAgIAAxkBAAEuHPRqkZ03kWvsJw7X42bCEC1T5cOrYwAC_B4AAl0YqUpRRiT9sSu2Hj0E",
        "tags": "мем 5",
    },
    {
        "id": "6",
        "title": "Звук 6",
        "file_id": "CQACAgIAAxkBAAEuHPBqkZ034-HvwqfoGRjcXCWQogOW4wACqX8AAiq0IUiw_aQAAVa0QZo9BA",
        "tags": "мем 6",
    },
]


dp = Dispatcher()


@dp.inline_query()
async def search_mifs(query: InlineQuery) -> None:
    user_input = query.query.lower().strip()
    results = []

    for mif in MIFS_DATABASE:
        searchable_text = f'{mif["title"]} {mif["tags"]}'.lower()

        if not user_input or user_input in searchable_text:
            results.append(
                InlineQueryResultCachedAudio(
                    id=mif["id"],
                    audio_file_id=mif["file_id"],
                    caption=mif["title"],
                )
            )

    await query.answer(
        results=results,
        cache_time=1,
        is_personal=True,
    )


async def main() -> None:
    bot_token = os.getenv("BOT_TOKEN")
    if not bot_token:
        raise RuntimeError("BOT_TOKEN is not configured")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    logger = logging.getLogger("mif-bot")

    async with Bot(token=bot_token) as bot:
        bot_info = await bot.get_me()
        logger.info("MIF bot started as @%s", bot_info.username)
        await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
