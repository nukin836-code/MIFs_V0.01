from fastapi import FastAPI, HTTPException, Response
import requests
from bs4 import BeautifulSoup
import json

app = FastAPI()

MOBILE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://www.tiktok.com/",
}

@app.get("/download")
def download_tiktok_audio(url: str):
    session = requests.Session()
    session.headers.update(MOBILE_HEADERS)
    
    try:
        # 1. Загружаем страницу TikTok напрямую (с мобильного IP Termux это прокатит без капчи)
        resp = session.get(url, timeout=10)
        if resp.status_code != 200:
            raise HTTPException(status_code=400, detail=f"TikTok вернул статус {resp.status_code}")
            
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        # 2. Ищем JSON-контейнер с данными внутри страницы
        script_tag = soup.find('script', id='__UNIVERSAL_DATA_FOR_REHYDRATION__')
        if not script_tag or not script_tag.string:
            script_tag = soup.find('script', id='SIGI_STATE')
            
        if not script_tag or not script_tag.string:
            raise HTTPException(status_code=404, detail="Не удалось найти данные о видео на странице")
            
        data = json.loads(script_tag.string)
        
        # 3. Достаем прямую ссылку на аудио (в зависимости от структуры ключей TikTok)
        # Обычно путь лежит в __DEFAULT_SCOPE__ -> app-common-detail или itemList
        download_url = None
        try:
            # Универсальный обход структуры hydration data
            item_module = data.get("__DEFAULT_SCOPE__", {}).get("webapp.video-detail", {}).get("itemInfo", {}).get("itemStruct", {})
            if not item_module:
                # Альтернативный путь для старых версий SIGI_STATE
                item_module = list(data.get("ItemModule", {}).values())[0]
                
            # Музыкальный трек или видео без водяного знака
            music_data = item_module.get("music", {})
            download_url = music_data.get("playUrl") or item_module.get("video", {}).get("playAddr")
        except Exception:
            pass
            
        if not download_url:
            raise HTTPException(status_code=500, detail="Не удалось извлечь прямую ссылку из JSON")
            
        # 4. Скачиваем байты медиа через наш сеанс и отдаем боту
        media_resp = session.get(download_url, timeout=15)
        if media_resp.status_code != 200:
            raise HTTPException(status_code=502, detail="CDN TikTok отклонил скачивание файла")
            
        return Response(content=media_resp.content, media_type="audio/mpeg")

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
        