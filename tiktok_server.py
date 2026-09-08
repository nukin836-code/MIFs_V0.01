from fastapi import FastAPI, HTTPException, Response
import requests
import yt_dlp

app = FastAPI(title="TikTok Downloader with yt-dlp")

TIKTOK_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://www.tiktok.com/",
}

@app.get("/download")
def download_tiktok_audio(url: str):
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "format": "bestaudio/best",
        "http_headers": TIKTOK_HEADERS,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            
            audio_url = None
            
            # 1. Проверяем запрошенные загрузки
            if "requested_downloads" in info and info["requested_downloads"]:
                audio_url = info["requested_downloads"][0].get("url")
            
            # 2. Ищем отдельный аудио-поток во всех форматах
            if not audio_url:
                for fmt in info.get("formats", []):
                    if fmt.get("acodec") != "none" and fmt.get("vcodec") == "none":
                        audio_url = fmt.get("url")
                        break

            # 3. Фолбэк на основной медиа-URL
            if not audio_url:
                audio_url = info.get("url")

            if not audio_url:
                raise HTTPException(status_code=500, detail="yt-dlp не смог извлечь URL аудио")

            # Скачиваем байты потока
            resp = requests.get(audio_url, headers=TIKTOK_HEADERS, timeout=20)
            if resp.status_code != 200:
                raise HTTPException(status_code=502, detail=f"CDN TikTok вернул статус {resp.status_code}")

            return Response(content=resp.content, media_type="audio/mpeg")

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка yt-dlp: {str(e)}")
        