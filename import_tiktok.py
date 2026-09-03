import requests

async def search_catalog(session: requests.Session, query: str, min_score: int = 0, max_results: int = 1):
    # Временная заглушка — возвращает пустой список, бот сразу переключится на MyInstants
    return []

def download_audio(session: requests.Session, tiktok_url: str) -> bytes:
    raise NotImplementedError("Скачивание из TikTok ещё не настроено.")
    