import re
from fastapi import FastAPI, HTTPException, Response
import requests

app = FastAPI()

# Заголовки для разворачивания коротких ссылок (vm.tiktok.com)
WEB_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
}

# Заголовки мобильного приложения TikTok (для обхода защиты и доступа к API)
MOBILE_API_HEADERS = {
    "User-Agent": "com.ss.android.ugc.trill/2613 (Linux; U; Android 10; en_US; Pixel 4; Build/QQ3A.200805.001; Cronet/58.0.2991.0)"
}


def extract_video_id(url: str) -> str:
    """
    Разворачивает короткую ссылку и вытаскивает цифровой ID видео из URL.
    """
    session = requests.Session()
    session.headers.update(WEB_HEADERS)
    
    try:
        # Используем stream=True, чтобы не скачивать тело страницы, а только получить итоговый URL редиректа
        response = session.get(url, allow_redirects=True, timeout=10, stream=True)
        final_url = response.url
        response.close()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Не удалось открыть ссылку: {str(e)}")

    # Ищем ID в формате /video/123456789... или /photo/123456789...
    match = re.search(r'/(?:video|photo|v)/(\d+)', final_url)
    
    # Если ссылка нестандартная, ищем любую последовательность из 18-20 цифр
    if not match:
        match = re.search(r'(\d{18,20})', final_url)
        
    if not match:
        raise HTTPException(status_code=400, detail="Не удалось извлечь ID видео из предоставленной ссылки")

    return match.group(1)


@app.get("/download")
def download_tiktok_audio(url: str):
    try:
        # 1. Извлекаем ID видео из ссылки
        video_id = extract_video_id(url)
        
        # 2. Обращаемся напрямую к внутреннему мобильному API TikTok
        api_url = f"https://api16-normal-c-useast1a.tiktokv.com/aweme/v1/feed/?aweme_id={video_id}"
        api_resp = requests.get(api_url, headers=MOBILE_API_HEADERS, timeout=10)
        
        if api_resp.status_code != 200:
            raise HTTPException(status_code=502, detail=f"API TikTok вернул статус {api_resp.status_code}")

        data = api_resp.json()
        aweme_list = data.get("aweme_list", [])

        if not aweme_list:
            raise HTTPException(status_code=404, detail="Видео не найдено или доступ к нему ограничен")

        aweme_data = aweme_list[0]
        
        # 3. Извлекаем прямую ссылку на mp3 из структуры JSON
        music_info = aweme_data.get("music", {})
        play_url_info = music_info.get("play_url", {})
        
        url_list = play_url_info.get("url_list", [])
        audio_download_url = None
        
        if url_list:
            audio_download_url = url_list[0]
        elif play_url_info.get("uri"):
            audio_download_url = play_url_info.get("uri")

        if not audio_download_url:
            raise HTTPException(status_code=500, detail="Не удалось найти ссылку на аудио в отчете TikTok API")

        # 4. Скачиваем аудиофайл
        audio_resp = requests.get(audio_download_url, headers=WEB_HEADERS, timeout=15)
        if audio_resp.status_code != 200:
            raise HTTPException(status_code=502, detail="CDN TikTok отклонил скачивание mp3 файла")

        return Response(content=audio_resp.content, media_type="audio/mpeg")

    except HTTPException as http_ex:
        raise http_ex
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка сервера: {str(e)}")
        