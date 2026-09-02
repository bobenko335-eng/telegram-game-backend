import asyncio
import logging
import os
from contextlib import asynccontextmanager

from aiogram import Bot
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from bot import build_dispatcher, run_polling, send_to_telegram
from storage import storage
from ws_manager import ws_manager

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("app")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError(
        "TELEGRAM_BOT_TOKEN не задан. Установите его в переменных окружения "
        "(Railway -> Variables), не храните токен в коде."
    )

ALLOWED_ORIGINS = os.environ.get("ALLOWED_ORIGINS", "*").split(",")

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = build_dispatcher()

app_state: dict = {"bot_username": None, "polling_task": None}


@asynccontextmanager
async def lifespan(app: FastAPI):
    me = await bot.get_me()
    app_state["bot_username"] = me.username
    logger.info("Bot authorized as @%s", me.username)
    app_state["polling_task"] = asyncio.create_task(run_polling(bot, dp))
    yield
    task = app_state["polling_task"]
    if task:
        task.cancel()
    await bot.session.close()


app = FastAPI(title="Telegram Game Backend", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class SendMessageBody(BaseModel):
    game_user_id: str
    text: str


@app.get("/api/health")
async def health():
    return {"status": "ok"}


@app.get("/api/bot-info")
async def bot_info():
    return {"username": app_state["bot_username"]}


@app.post("/api/link/start")
async def link_start():
    game_user_id, link_token = storage.create_link()
    username = app_state["bot_username"]
    deep_link = f"https://t.me/{username}?start={link_token}" if username else None
    return {
        "game_user_id": game_user_id,
        "link_token": link_token,
        "deep_link": deep_link,
    }


@app.get("/api/link/status/{link_token}")
async def link_status(link_token: str):
    link = storage.get_link(link_token)
    if not link:
        raise HTTPException(status_code=404, detail="link not found")
    user = storage.get_user(link["game_user_id"])
    return {
        "status": link["status"],
        "game_user_id": link["game_user_id"],
        "profile": (user or {}).get("profile", {}),
    }


@app.get("/api/profile/{game_user_id}")
async def get_profile(game_user_id: str):
    user = storage.get_user(game_user_id)
    if not user:
        raise HTTPException(status_code=404, detail="user not found")
    return {
        "profile": user["profile"],
        "blocked": user["blocked"],
        "linked": user["telegram_id"] is not None,
    }


@app.post("/api/block/{game_user_id}")
async def block_user(game_user_id: str):
    if not storage.set_blocked(game_user_id, True):
        raise HTTPException(status_code=404, detail="user not found")
    return {"blocked": True}


@app.post("/api/unblock/{game_user_id}")
async def unblock_user(game_user_id: str):
    if not storage.set_blocked(game_user_id, False):
        raise HTTPException(status_code=404, detail="user not found")
    return {"blocked": False}


@app.get("/api/messages/{game_user_id}")
async def get_messages(game_user_id: str, since: float = 0.0, limit: int = 200):
    if not storage.get_user(game_user_id):
        raise HTTPException(status_code=404, detail="user not found")
    return {"messages": storage.get_messages(game_user_id, since=since, limit=limit)}


@app.get("/api/feed")
async def get_feed(since: float = 0.0, limit: int = 200):
    return {"messages": storage.get_feed(since=since, limit=limit)}


@app.post("/api/messages")
async def post_message(body: SendMessageBody):
    user = storage.get_user(body.game_user_id)
    if not user:
        raise HTTPException(status_code=404, detail="user not found")
    if not user.get("telegram_id"):
        raise HTTPException(status_code=409, detail="user not linked to Telegram yet")
    ok = await send_to_telegram(bot, body.game_user_id, body.text)
    if not ok:
        raise HTTPException(status_code=409, detail="message not delivered (blocked or not linked)")
    return {"delivered": True}


@app.websocket("/ws/{game_user_id}")
async def ws_endpoint(websocket: WebSocket, game_user_id: str):
    await ws_manager.connect(game_user_id, websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        await ws_manager.disconnect(game_user_id, websocket)


@app.websocket("/ws/feed")
async def ws_feed_endpoint(websocket: WebSocket):
    await ws_manager.connect_feed(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        await ws_manager.disconnect_feed(websocket)
