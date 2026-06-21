import os
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Response
import httpx
from google import genai
from google.genai import types

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"].strip()
GOOGLE_API_KEY = os.environ["GOOGLE_API_KEY"].strip()
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "").strip()
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

SYSTEM_PROMPT = """你是一個專業的《寶可夢 朱/紫》(Pokémon Scarlet/Violet) 遊戲攻略助理，名字叫「紫羅蘭」。

你擅長回答：
- 寶可夢的能力值、屬性、特性、弱點、進化條件
- 各種招式的取得方式（升等學習、技能機、蛋招、教招師等）
- 主線劇情攻略（勝利之路、傳說故事、星團作戰）、道館順序建議
- 支線任務與隱藏要素
- 對戰組隊建議、招式搭配、持有道具推薦
- 特殊捕捉地點（哪裡找、幾率、天氣條件等）
- 奇異異聞所攻略
- 識別遊戲截圖內容並給出攻略建議

回答規則：
1. 優先用繁體中文回答
2. 回答要具體、實用，附上遊戲內正確名稱
3. 有多個選項時用條列式整理
4. 對戰建議要說明原因
5. 不確定答案時請誠實說明並建議查閱 Bulbapedia 或攻略網站
6. 回答長度適中，不要過於冗長
7. 看到遊戲截圖時，主動描述畫面內容並給出相關攻略建議"""

chat_histories: dict[int, list] = {}
MAX_HISTORY_TURNS = 20

gemini_client = genai.Client(api_key=GOOGLE_API_KEY)


def get_gemini_reply(chat_id: int, parts: list) -> str:
    history = chat_histories.get(chat_id, [])
    history.append(types.Content(role="user", parts=parts))

    try:
        response = gemini_client.models.generate_content(
            model="gemini-1.5-flash",
            contents=history,
            config=types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT),
        )
        reply = response.text
        history.append(types.Content(role="model", parts=[types.Part(text=reply)]))
        if len(history) > MAX_HISTORY_TURNS * 2:
            history = history[-(MAX_HISTORY_TURNS * 2):]
        chat_histories[chat_id] = history
        return reply
    except Exception as e:
        history.pop()
        logger.error(f"Gemini error: {e}")
        return f"⚠️ 發生錯誤，請稍後再試。\n錯誤訊息：{str(e)}"


async def download_photo(http: httpx.AsyncClient, file_id: str) -> bytes:
    r = await http.get(f"{TELEGRAM_API}/getFile", params={"file_id": file_id})
    file_path = r.json()["result"]["file_path"]
    r = await http.get(f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}")
    return r.content


async def send_message(http: httpx.AsyncClient, chat_id: int, text: str) -> None:
    if len(text) > 4000:
        text = text[:4000] + "\n\n...（回答過長，請換個方式詢問）"
    await http.post(
        f"{TELEGRAM_API}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
    )


async def send_typing(http: httpx.AsyncClient, chat_id: int) -> None:
    await http.post(
        f"{TELEGRAM_API}/sendChatAction",
        json={"chat_id": chat_id, "action": "typing"},
    )


async def set_webhook(http: httpx.AsyncClient) -> None:
    if not WEBHOOK_URL:
        logger.warning("WEBHOOK_URL not set, skipping webhook registration")
        return
    url = f"{WEBHOOK_URL}/webhook"
    r = await http.post(f"{TELEGRAM_API}/setWebhook", json={"url": url})
    logger.info(f"Webhook set to {url}: {r.json()}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with httpx.AsyncClient(timeout=10) as http:
        await set_webhook(http)
    yield


app = FastAPI(lifespan=lifespan)


@app.post("/webhook")
async def webhook(request: Request):
    update = await request.json()
    message = update.get("message") or update.get("edited_message")

    if not message:
        return Response("ok")

    chat_id: int = message["chat"]["id"]
    text: str = message.get("text", "").strip()
    caption: str = message.get("caption", "").strip()
    photos = message.get("photo")

    async with httpx.AsyncClient(timeout=20) as http:
        # 指令處理
        if text == "/start":
            chat_histories.pop(chat_id, None)
            await send_message(
                http, chat_id,
                "👋 你好！我是**紫羅蘭**，你的《寶可夢 朱/紫》專屬攻略助理！\n\n"
                "你可以問我：\n"
                "• 招式怎麼取得（例：「水砲怎麼學？」）\n"
                "• 道館攻略（例：「第一個道館用哪隻？」）\n"
                "• 組隊建議（例：「水系隊伍推薦」）\n"
                "• 捕捉地點（例：「哪裡找長毛狗？」）\n"
                "• 📸 **直接傳遊戲截圖**，我會幫你分析！\n\n"
                "直接傳訊息給我就好！對話記憶功能已啟用 🧠",
            )
            return Response("ok")

        if text == "/clear":
            chat_histories.pop(chat_id, None)
            await send_message(http, chat_id, "🗑️ 對話記憶已清除！")
            return Response("ok")

        if text == "/help":
            await send_message(
                http, chat_id,
                "📖 **使用說明**\n\n"
                "直接用中文問我任何寶可夢朱/紫的問題！\n\n"
                "**範例問題：**\n"
                "• 草蜥蜴最終進化是什麼？\n"
                "• 假面梟有哪些好用的招式？\n"
                "• 主線建議道館順序\n"
                "• 怎麼捕捉傳說寶可夢\n"
                "• 推薦一隊平衡的隊伍配置\n\n"
                "**截圖功能：**\n"
                "直接傳遊戲截圖，可加說明文字（例：「這個怎麼打？」）\n\n"
                "**指令：**\n"
                "/clear — 清除對話記憶\n"
                "/help — 顯示此說明",
            )
            return Response("ok")

        TRIGGER = "@@"

        # 圖片訊息：caption 必須以 @@ 開頭
        if photos:
            if not caption.startswith(TRIGGER):
                return Response("ok")
            question = caption[len(TRIGGER):].strip() or "請分析這張寶可夢朱/紫的遊戲截圖，描述畫面內容並給出攻略建議。"
            await send_typing(http, chat_id)
            best_photo = max(photos, key=lambda p: p["file_size"])
            image_bytes = await download_photo(http, best_photo["file_id"])
            parts = [
                types.Part(inline_data=types.Blob(mime_type="image/jpeg", data=image_bytes)),
                types.Part(text=question),
            ]
            reply = get_gemini_reply(chat_id, parts)
            await send_message(http, chat_id, reply)
            return Response("ok")

        # 純文字訊息：必須以 @@ 開頭
        if text and text.startswith(TRIGGER):
            question = text[len(TRIGGER):].strip()
            if not question:
                return Response("ok")
            await send_typing(http, chat_id)
            reply = get_gemini_reply(chat_id, [types.Part(text=question)])
            await send_message(http, chat_id, reply)

    return Response("ok")


@app.get("/")
async def health():
    return {"status": "ok", "bot": "Pokemon Violet Bot"}
