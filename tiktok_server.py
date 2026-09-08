import re
from fastapi import FastAPI, HTTPException, Response
import requests

app = FastAPI()

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Referer": "https://www.tiktok.com/",
}

def extract_video_id(url: str) -> str:
    session = requests.Session()
    session.headers.update(HEADERS)
    try:
        response = session.get(url, allow_redirects=True, timeout=10, stream=True)
        final_url = response.url
        response.close()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Ошибка ссылки: {str(e)}")

    match = re.search(r'/(?:video|photo|v)/(\d+)', final_url) or re.search(r'(\d{18,20})', final_url)
    if not match:
        raise HTTPException(status_code=400, detail="Не удалось извлечь ID видео")
    return match.group(1)

@app.get("/download")
def download_tiktok_audio(url: str):
    try:
        video_id = extract_video_id(url)
        
        # Embed API v2 не требует мобильных подписей и обходит ошибку 429
        embed_url = f"https://www.tiktok.com/embed/v2/{video_id}"
        session = requests.Session()
        session.headers.update(HEADERS)
        
        resp = session.get(embed_url, timeout=10)
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail=f"Embed вернул статус {resp.status_code}")

        play_urls = re.findall(r'"playUrl":"(https?:\\u002F\\u002F[^"]+)"', resp.text)
        if not play_urls:
            play_urls = re.findall(r'"playUrl":"(https?://[^"]+)"', resp.text)
        if not play_urls:
            play_urls = re.findall(r'"playAddr":"(https?:\\u002F\\u002F[^"]+)"', resp.text)

        if not play_urls:
            raise HTTPException(status_code=500, detail="Не удалось извлечь прямую ссылку из Embed")

        audio_url = play_urls[0].replace(r"\u002F", "/")

        audio_resp = session.get(audio_url, headers=HEADERS, timeout=15)
        if audio_resp.status_code != 200:
            raise HTTPException(status_code=502, detail="CDN TikTok отклонил скачивание mp3")

        return Response(content=audio_resp.content, media_type="audio/mpeg")

    except HTTPException as http_ex:
        raise http_ex
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка сервера: {str(e)}")
        