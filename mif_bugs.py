import logging
import os
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

# Импортируем функцию для обрезки текста из основного файла
from mif_core import clip_text

logger = logging.getLogger("mif-bot.bugs")

# ПРИМЕЧАНИЕ: минимальный аналог функции баг-репортов — у тебя, по твоим
# словам, была своя, я её не получил. Если найдёшь/пришлёшь — просто замени
# тело report_bug() ниже на вызов твоей, остальной код от этого не зависит.
BUG_REPORT_CHAT_ID = os.getenv("BUG_REPORT_CHAT_ID", "-5476127508")


async def report_bug(bot: Bot, text: str) -> None:
    """Шлёт короткое сообщение об ошибке в группу для баг-репортов."""
    try:
        await bot.send_message(chat_id=int(BUG_REPORT_CHAT_ID), text=clip_text(text, 3500))
    except (TelegramAPIError, ValueError):
        logger.exception("Не удалось отправить баг-репорт в группу")
        