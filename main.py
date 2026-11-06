import os
import uuid
import random
import yt_dlp
from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, types, F, BaseMiddleware
from aiogram.filters import Command
from aiogram.types import (
    FSInputFile,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)
from aiogram.enums import ChatMemberStatus
import uvicorn

# ============================================================
#  SOZLAMALAR (Token Render'dan olinadi)
# ============================================================
TOKEN = os.environ.get("BOT_TOKEN")
WEBHOOK_PATH = f"/webhook/{TOKEN}"
BASE_WEBHOOK_URL = os.environ.get("RENDER_EXTERNAL_URL")

CHANNELS = [
    {
        "id": "@khamarov_77",
        "name": "📢 Khamarov kanali",
        "url": "https://t.me/khamarov_77",
    },
]

# Render serverida yozish uchun vaqtinchalik papka
DOWNLOAD_DIR = "/tmp/tgbot"
MAX_FILE_SIZE = 50 * 1024 * 1024

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

OK_STATUSES = {
    ChatMemberStatus.MEMBER,
    ChatMemberStatus.ADMINISTRATOR,
    ChatMemberStatus.CREATOR,
}

app = FastAPI()
bot = Bot(token=TOKEN)
dp = Dispatcher()

# ============================================================
#  EMOTSIYALAR VA MATNLAR
# ============================================================
WELCOME_TEXTS = [
    "👋 Assalomu alaykum, <b>{name}</b>!\n\n📥 Menga video havolasini yuboring — men uni sizga yetkazaman! 🚀",
    "🌟 Salom, <b>{name}</b>! Xush kelibsiz!\n\n🎬 Instagram, TikTok, YouTube — qaysi biri bo'lsa ham, yuboring!",
    "😊 Assalomu alaykum, <b>{name}</b>!\n\n📲 Havolani tashlang — videoni tezda yuklab beraman! ⚡",
]
LOADING_TEXTS = [
    "⏳ Yuklab olinmoqda, biroz kuting...",
    "🔄 Video tayyorlanmoqda, sabr qiling...",
    "📥 Havoladan video olinmoqda...",
]
SUCCESS_CAPTIONS = [
    "✅ Mana, siz so'ragan video! 🎬",
    "🎉 Tayyor! Yoqimli tomosha! 🍿",
    "✨ Videongiz tayyor! Marhamat! 🎊",
    "🔥 Mana! Sifatli video tayyor!",
]
ERROR_TEXTS = [
    "😔 Afsus, yuklab bo'lmadi.\n\nSabablari: havola noto'g'ri, video yopiq yoki sayt ishlamayapti.",
    "😢 Xatolik yuz berdi...\n\nBoshqa havola bilan sinab ko'ring.",
]

def pick(lst):
    return random.choice(lst)

# ============================================================
#  OBUNA TEKSHIRISH VA MIDDLEWARE
# ============================================================
async def get_not_subscribed(user_id: int) -> list:
    missing = []
    for ch in CHANNELS:
        try:
            member = await bot.get_chat_member(chat_id=ch["id"], user_id=user_id)
            if member.status not in OK_STATUSES:
                missing.append(ch)
        except Exception:
            missing.append(ch)
    return missing

def subscription_keyboard(missing: list) -> InlineKeyboardMarkup:
    buttons = []
    for ch in missing:
        buttons.append([InlineKeyboardButton(text=ch["name"], url=ch["url"])])
    buttons.append([InlineKeyboardButton(text="✅ Tekshirish", callback_data="check_sub")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

SUBSCRIPTION_TEXT = "🔒 <b>Botdan foydalanish uchun kanalga a'zo bo'ling:</b>\n\n👇 Pastdagi tugmani bosing va a'zo bo'ling\n✅ So'ng <b>Tekshirish</b> tugmasini bosing!"

class SubscriptionMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        if isinstance(event, types.Message):
            if event.text and event.text.startswith("/start"):
                return await handler(event, data)
            user_id = event.from_user.id
        elif isinstance(event, CallbackQuery):
            if event.data == "check_sub":
                return await handler(event, data)
            user_id = event.from_user.id
        else:
            return await handler(event, data)

        missing = await get_not_subscribed(user_id)
        if missing:
            if isinstance(event, types.Message):
                await event.answer(SUBSCRIPTION_TEXT, parse_mode="HTML", reply_markup=subscription_keyboard(missing))
            else:
                await event.answer("❗ Avval kanalga a'zo bo'ling!", show_alert=True)
            return
        return await handler(event, data)

dp.message.middleware(SubscriptionMiddleware())
dp.callback_query.middleware(SubscriptionMiddleware())

# ============================================================
#  YUKLASH
# ============================================================
def _download_sync(url: str) -> str:
    uid = uuid.uuid4().hex[:8]
    outtmpl = os.path.join(DOWNLOAD_DIR, f"{uid}_%(id)s.%(ext)s")
    opts = {
        "format": "best[filesize<45M]/best[height<=480]/best",
        "outtmpl": outtmpl,
        "quiet": True,
        "noplaylist": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        path = ydl.prepare_filename(info)
        if not os.path.exists(path):
            base = os.path.splitext(path)[0]
            for ext in (".mp4", ".webm", ".mkv", ".mov"):
                if os.path.exists(base + ext):
                    return base + ext
        return path

# ============================================================
#  HANDLERLAR
# ============================================================
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    missing = await get_not_subscribed(message.from_user.id)
    if missing:
        await message.answer(SUBSCRIPTION_TEXT, parse_mode="HTML", reply_markup=subscription_keyboard(missing))
        return
    name = message.from_user.first_name or "do'stim"
    await message.answer(pick(WELCOME_TEXTS).format(name=name) + "\n\n⚠️ <b>50 MB</b> gacha video yuboriladi.", parse_mode="HTML")

@dp.callback_query(F.data == "check_sub")
async def check_sub(cq: CallbackQuery):
    missing = await get_not_subscribed(cq.from_user.id)
    if missing:
        await cq.answer("❌ Hali a'zo bo'lmadingiz!", show_alert=True)
        return
    await cq.answer("✅ Rahmat!")
    try:
        await cq.message.edit_text("✅ Obuna tasdiqlandi!\n\n📥 Endi video havolasini yuboring! 🎬")
    except Exception:
        pass

@dp.message(F.text)
async def handle_link(message: types.Message):
    text = message.text.strip()
    if not (text.startswith("http://") or text.startswith("https://")):
        await message.answer("⚠️ Iltimos, havola (link) yuboring!")
        return

    status = await message.answer(pick(LOADING_TEXTS))
    file_path = None
    try:
        import asyncio
        file_path = await asyncio.to_thread(_download_sync, text)
        if not file_path or not os.path.exists(file_path):
            await status.edit_text(pick(ERROR_TEXTS))
            return
        if os.path.getsize(file_path) > MAX_FILE_SIZE:
            await status.edit_text("😔 Fayl 50 MB dan katta. Kichikroq video yuboring.")
            return

        await message.answer_video(video=FSInputFile(file_path), caption=pick(SUCCESS_CAPTIONS), supports_streaming=True)
        await status.delete()
    except Exception:
        await status.edit_text(pick(ERROR_TEXTS))
    finally:
        if file_path and os.path.exists(file_path):
            try: os.remove(file_path)
            except: pass

# ============================================================
#  WEBHOOK VA FASTAPI SOZLAMALARI
# ============================================================
@app.post(WEBHOOK_PATH)
async def webhook_handler(request: Request):
    update = types.Update.model_validate(await request.json())
    await dp.feed_update(bot, update)
    return {"status": "ok"}

@app.get("/")
async def health_check():
    return {"status": "healthy"}

@app.on_event("startup")
async def on_startup():
    if BASE_WEBHOOK_URL:
        webhook_url = f"{BASE_WEBHOOK_URL}{WEBHOOK_PATH}"
        await bot.set_webhook(webhook_url)
        print(f"✅ Webhook o'rnatildi: {webhook_url}")

@app.on_event("shutdown")
async def on_shutdown():
    await bot.delete_webhook()

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
