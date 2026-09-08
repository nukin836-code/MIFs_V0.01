from fastapi import FastAPI, HTTPException, Response
import requests

app = FastAPI(title="TikTok Downloader")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
}

@app.get("/download")
def download_tiktok_audio(url: str):
    try:
        # Запрос к бесплатной обертке TikWM без использования OEmbed
        api_resp = requests.get(
            "https://www.tikwm.com/api/", 
            params={"url": url}, 
            headers=HEADERS, 
            timeout=10
        )
        
        if api_resp.status_code != 200:
            raise HTTPException(status_code=502, detail="TikWM API недоступен")

        res_json = api_resp.json()
        if res_json.get("code") != 0:
            raise HTTPException(status_code=400, detail=f"TikWM: {res_json.get('msg')}")

        data = res_json.get("data", {})
        audio_url = data.get("music") or data.get("play")
        
        if not audio_url:
            raise HTTPException(status_code=500, detail="Не найден URL аудио")

        if audio_url.startswith("/"):
            audio_url = f"https://www.tikwm.com{audio_url}"

        # Скачиваем байты аудио
        audio_file = requests.get(audio_url, headers=HEADERS, timeout=15)
        if audio_file.status_code != 200:
            raise HTTPException(status_code=502, detail="CDN отклонил скачивание")

        # Возвращаем байты прямо в import_tiktok.py -> loader.py
        return Response(content=audio_file.content, media_type="audio/mpeg")

    except HTTPException as http_ex:
        raise http_ex
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
        