from fastapi import FastAPI, HTTPException, Response
import requests
import yt_dlp
import tempfile
import os

app = FastAPI(title="TikTok Downloader Hybrid")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
}

def _download_via_ytdlp(url: str) -> bytes:
    """Резервный метод скачивания через yt-dlp."""
    with tempfile.TemporaryDirectory() as tmpdir:
        opts = {
            "format": "bestaudio/best",
            "outtmpl": os.path.join(tmpdir, "%(id)s.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "http_headers": HEADERS,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.extract_info(url, download=True)
            files = os.listdir(tmpdir)
            if not files:
                raise RuntimeError("yt-dlp не создал файл")
            with open(os.path.join(tmpdir, files[0]), "rb") as f:
                return f.read()

@app.get("/download")
def download_tiktok_audio(url: str):
    # --- Шаг 1: Пробуем быстро через TikWM ---
    try:
        api_resp = requests.get(
            "https://www.tikwm.com/api/", 
            params={"url": url}, 
            headers=HEADERS, 
            timeout=7
        )
        if api_resp.status_code == 200:
            res_json = api_resp.json()
            if res_json.get("code") == 0:
                data = res_json.get("data", {})
                audio_url = data.get("music") or data.get("play")
                if audio_url:
                    if audio_url.startswith("/"):
                        audio_url = f"https://www.tikwm.com{audio_url}"
                    
                    audio_file = requests.get(audio_url, headers=HEADERS, timeout=10)
                    if audio_file.status_code == 200:
                        return Response(content=audio_file.content, media_type="audio/mpeg")
    except Exception:
        pass  # Если TikWM сбоит — бесшумно идем в yt-dlp

    # --- Шаг 2: Фолбэк на yt-dlp если TikWM не справился ---
    try:
        audio_bytes = _download_via_ytdlp(url)
        return Response(content=audio_bytes, media_type="audio/mpeg")
    except Exception as e:
        raise HTTPException(
            status_code=500, 
            detail=f"Не удалось скачать видео ни через TikWM, ни через yt-dlp: {str(e)}"
        )
        