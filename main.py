import os
import uuid
import random
import asyncio
import sqlite3
import yt_dlp
import subprocess
from shazamio import Shazam
from datetime import datetime
from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, types, F, BaseMiddleware
from aiogram.filters import Command
from aiogram.types import (
    FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton,
    CallbackQuery, InlineQuery, InlineQueryResultArticle, InputTextMessageContent
)
from aiogram.enums import ChatMemberStatus
import uvicorn

# ============================================================
#  SOZLAMALAR
# ============================================================
TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = 123456789  # <--- O'zingizning ID raqamingizni yozing!
BASE_WEBHOOK_URL = os.environ.get("RENDER_EXTERNAL_URL")
WEBHOOK_PATH = f"/webhook/{TOKEN}"

DOWNLOAD_DIR = "/tmp/tgbot"
MAX_FILE_SIZE = 50 * 1024 * 1024
DAILY_LIMIT = 3
REFERRAL_BONUS = 3
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

app = FastAPI()
bot = Bot(token=TOKEN)
dp = Dispatcher()

audio_cache = {}
file_cache = {}
maintenance_mode = False 

# ============================================================
#  MA'LUMOTLAR BAZASI
# ============================================================
def init_db():
    conn = sqlite3.connect('bot_data.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, first_name TEXT, username TEXT, referrer_id INTEGER, limit_used INTEGER DEFAULT 0, last_reset TEXT, is_banned INTEGER DEFAULT 0)''')
    c.execute('''CREATE TABLE IF NOT EXISTS channels (id INTEGER PRIMARY KEY AUTOINCREMENT, channel_id TEXT UNIQUE, name TEXT, url TEXT)''')
    conn.commit()
    conn.close()

init_db()

def db_query(query, params=()):
    conn = sqlite3.connect('bot_data.db')
    c = conn.cursor()
    c.execute(query, params)
    result = c.fetchall()
    conn.commit()
    conn.close()
    return result

def add_user(user_id, first_name, username, referrer_id=None):
    today = datetime.now().strftime("%Y-%m-%d")
    user = db_query("SELECT * FROM users WHERE user_id=?", (user_id,))
    if not user:
        db_query("INSERT INTO users (user_id, first_name, username, referrer_id, last_reset) VALUES (?, ?, ?, ?, ?)", (user_id, first_name, username, referrer_id, today))
        if referrer_id:
            db_query("UPDATE users SET limit_used = limit_used - ? WHERE user_id=?", (REFERRAL_BONUS, referrer_id))
        return True
    return False

def check_limit(user_id):
    today = datetime.now().strftime("%Y-%m-%d")
    user = db_query("SELECT limit_used, last_reset, is_banned FROM users WHERE user_id=?", (user_id,))
    if not user: return False, "Ro'yxatdan o'tmagansiz. /start bosing."
    limit_used, last_reset, is_banned = user[0]
    if is_banned: return False, "🚫 Siz botdan bloklangansiz."
    if last_reset != today:
        db_query("UPDATE users SET limit_used=0, last_reset=? WHERE user_id=?", (today, user_id))
        limit_used = 0
    if limit_used >= DAILY_LIMIT:
        return False, f"Kunlik limitingiz tugadi ({DAILY_LIMIT} ta).\nDo'stlaringizni taklif qilib +{REFERRAL_BONUS} tadan limit oling!"
    return True, "OK"

def increment_limit(user_id):
    db_query("UPDATE users SET limit_used = limit_used + 1 WHERE user_id=?", (user_id,))

# ============================================================
#  MIDDLEWARE
# ============================================================
async def get_not_subscribed(user_id: int) -> list:
    channels = db_query("SELECT channel_id, name, url FROM channels")
    missing = []
    for ch in channels:
        try:
            member = await bot.get_chat_member(chat_id=ch[0], user_id=user_id)
            if member.status not in {ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}:
                missing.append({"id": ch[0], "name": ch[1], "url": ch[2]})
        except Exception:
            missing.append({"id": ch[0], "name": ch[1], "url": ch[2]})
    return missing

def subscription_keyboard(missing: list) -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(text=ch["name"], url=ch["url"])] for ch in missing]
    buttons.append([InlineKeyboardButton(text="✅ Tekshirish", callback_data="check_sub")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

class MainMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        global maintenance_mode
        user_id = event.from_user.id
        if user_id == ADMIN_ID: return await handler(event, data)
        if maintenance_mode:
            if isinstance(event, types.Message): await event.answer("🛠 Botda texnik ishlar ketmoqda.")
            return
        if isinstance(event, types.Message) and event.text and event.text.startswith("/start"): return await handler(event, data)
        if isinstance(event, (types.Message, CallbackQuery)):
            missing = await get_not_subscribed(user_id)
            if missing:
                if isinstance(event, types.Message): await event.answer("🔒 <b>Botdan foydalanish uchun obuna bo'ling:</b>", parse_mode="HTML", reply_markup=subscription_keyboard(missing))
                else: await event.answer("❗ Avval kanallarga a'zo bo'ling!", show_alert=True)
                return
        return await handler(event, data)

dp.message.middleware(MainMiddleware())
dp.callback_query.middleware(MainMiddleware())

# ============================================================
#  YUKLASH MANTIG'I
# ============================================================
def _download_video_sync(url: str) -> str:
    uid = uuid.uuid4().hex[:8]
    outtmpl = os.path.join(DOWNLOAD_DIR, f"{uid}_%(id)s.%(ext)s")
    opts = {"format": "best[filesize<45M]/best[height<=480]/best", "outtmpl": outtmpl, "quiet": True, "noplaylist": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        path = ydl.prepare_filename(info)
        if not os.path.exists(path):
            base = os.path.splitext(path)[0]
            for ext in (".mp4", ".webm", ".mkv", ".mov"):
                if os.path.exists(base + ext): return base + ext
        return path

def _download_from_youtube_sync(query: str) -> str:
    uid = uuid.uuid4().hex[:8]
    outtmpl = os.path.join(DOWNLOAD_DIR, f"yt_{uid}_%(id)s.%(ext)s")
    opts = {
        "format": "bestaudio/best",
        "outtmpl": outtmpl,
        "quiet": True,
        "noplaylist": True,
        "default_search": "ytsearch1",
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}],
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(query, download=True)
        path = ydl.prepare_filename(info)
        if not os.path.exists(path):
            base = os.path.splitext(path)[0]
            if os.path.exists(base + ".mp3"): return base + ".mp3"
        return path

# ============================================================
#  SHAZAM (VIDEODAN ANIQLASH)
# ============================================================
def _recognize_music_sync(video_path: str) -> str:
    audio_path = os.path.join(DOWNLOAD_DIR, f"{uuid.uuid4().hex[:8]}.mp3")
    try:
        subprocess.run(["ffmpeg", "-i", video_path, "-q:a", "0", "-map", "a", audio_path, "-y"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
        shazam = Shazam()
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        out = loop.run_until_complete(shazam.recognize(audio_path))
        loop.close()

        if 'track' in out:
            title = out['track'].get('title', 'Noma\'lum')
            subtitle = out['track'].get('subtitle', 'Noma\'lum')
            return f"{subtitle} - {title}"
        return None
    except Exception as e:
        print(f"Shazam xatosi: {e}")
        return None
    finally:
        if os.path.exists(audio_path): os.remove(audio_path)

# ============================================================
#  HANDLERLAR
# ============================================================
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    args = message.text.split()
    referrer_id = int(args[1]) if len(args) > 1 and args[1].isdigit() else None
    if referrer_id == message.from_user.id: referrer_id = None
    add_user(message.from_user.id, message.from_user.first_name, message.from_user.username, referrer_id)

    missing = await get_not_subscribed(message.from_user.id)
    if missing:
        await message.answer("🔒 <b>Botdan foydalanish uchun obuna bo'ling:</b>", parse_mode="HTML", reply_markup=subscription_keyboard(missing))
        return
        
    bot_username = (await bot.get_me()).username
    ref_link = f"https://t.me/{bot_username}?start={message.from_user.id}"
    text = (f"👋 Salom, <b>{message.from_user.first_name}</b>!\n\n"
            f"📥 Menga havola yoki qo'shiq nomini yozing, men uni topib beraman.\n"
            f"🎵 Video ostidagi tugma orqali musiqani to'liq versiyasini yuklab olishingiz mumkin.\n\n"
            f"🎁 <b>Do'stlaringizni taklif qiling!</b>\nHar bir taklif uchun +{REFERRAL_BONUS} ta limit oling:\n<code>{ref_link}</code>")
    await message.answer(text, parse_mode="HTML")

@dp.callback_query(F.data == "check_sub")
async def check_sub(cq: CallbackQuery):
    missing = await get_not_subscribed(cq.from_user.id)
    if missing: return await cq.answer("❌ Hali hamma kanalga a'zo bo'lmadingiz!", show_alert=True)
    await cq.answer("✅ Rahmat!")
    await cq.message.edit_text("✅ Obuna tasdiqlandi!\n\n📥 Endi havola yoki qo'shiq nomini yuboring! 🎬")

@dp.message(F.text & ~F.text.startswith("/"))
async def handle_link(message: types.Message):
    text = message.text.strip()
    
    # Agar havola bo'lsa (video yuklash)
    if text.startswith("http://") or text.startswith("https://"):
        ok, msg = check_limit(message.from_user.id)
        if not ok: return await message.answer(f"⚠️ {msg}")

        status = await message.answer("⏳ Video yuklanmoqda, kuting...")
        file_path = None
        try:
            file_path = await asyncio.to_thread(_download_video_sync, text)
            if not file_path or not os.path.exists(file_path): return await status.edit_text("😔 Yuklab bo'lmadi.")
            if os.path.getsize(file_path) > MAX_FILE_SIZE: return await status.edit_text("😔 Video 50 MB dan katta.")

            session_id = str(uuid.uuid4())[:8]
            audio_cache[session_id] = text
            file_cache[session_id] = file_path
            increment_limit(message.from_user.id)

            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🎵 Musiqa (To'liq versiya)", callback_data=f"audio_{session_id}"),
                 InlineKeyboardButton(text="📁 HD Fayl", callback_data=f"doc_{session_id}")]
            ])
            
            bot_username = (await bot.get_me()).username
            caption = f'<a href="{text}">📥 @{bot_username} orqali yuklab olindi</a>'
            
            await message.answer_video(video=FSInputFile(file_path), caption=caption, parse_mode="HTML", reply_markup=keyboard, supports_streaming=True)
            await status.delete()
        except Exception as e:
            await status.edit_text("😔 Xatolik yuz berdi.")
            await bot.send_message(ADMIN_ID, f"⚠️ Xatolik: {e}")
            
    # Agar oddiy matn bo'lsa (qo'shiq qidirish)
    else:
        ok, msg = check_limit(message.from_user.id)
        if not ok: return await message.answer(f"⚠️ {msg}")

        status = await message.answer(f"🔍 <b>{text}</b> qidirilmoqda...", parse_mode="HTML")
        
        try:
            # YouTube'dan qidirib topamiz
            file_path = await asyncio.to_thread(_download_from_youtube_sync, text)
            
            if file_path and os.path.exists(file_path) and os.path.getsize(file_path) <= MAX_FILE_SIZE:
                await message.answer_audio(
                    audio=FSInputFile(file_path), 
                    caption=f"🎵 <b>{text}</b>\n<i>(YouTube'dan topilgan natija)</i>", 
                    parse_mode="HTML"
                )
                await status.delete()
                increment_limit(message.from_user.id)
            else:
                await status.edit_text("😔 Afsus, bu qo'shiqni topa olmadim yoki fayl 50 MB dan katta.")
        except Exception as e:
            print(f"Qidiruv xatosi: {e}")
            await status.edit_text("😔 Qidiruvda xatolik yuz berdi.")

@dp.callback_query(F.data.startswith("audio_"))
async def process_audio(cq: CallbackQuery):
    url = audio_cache.get(cq.data.split("_")[1])
    if not url: return await cq.answer("❌ Havola eskirgan.", show_alert=True)
    
    await cq.answer("⏳ Qo'shiq aniqlanmoqda...")
    try: await cq.message.edit_reply_markup(reply_markup=None)
    except: pass
    
    status_msg = await cq.message.reply("🎧 Ovoz tinglanmoqda va qo'shiq nomi aniqlanmoqda...")
    video_path = None
    file_path = None
    
    try:
        # 1. Videoni yuklab olamiz
        video_path = await asyncio.to_thread(_download_video_sync, url)
        if not video_path: return await status_msg.edit_text("😔 Videoni yuklab bo'lmadi.")

        # 2. Shazam orqali qo'shiqni aniqlaymiz
        search_query = await asyncio.to_thread(_recognize_music_sync, video_path)
        
        if not search_query:
            return await status_msg.edit_text("😔 Afsus, bu musiqani Shazam bazasidan topa olmadim.")

        await status_msg.edit_text(f"🎵 <b>{search_query}</b> topildi!\n📥 To'liq versiyasi yuklanmoqda...", parse_mode="HTML")
        
        # 3. YouTube'dan to'liq qo'shiqni yuklaymiz
        file_path = await asyncio.to_thread(_download_from_youtube_sync, search_query)

        if file_path and os.path.exists(file_path) and os.path.getsize(file_path) <= MAX_FILE_SIZE:
            await cq.message.answer_audio(audio=FSInputFile(file_path), caption=f"🎵 <b>{search_query}</b>\n<i>(YouTube'dan to'liq versiya)</i>", parse_mode="HTML")
            await status_msg.delete()
        else:
            await status_msg.edit_text("😔 To'liq qo'shiqni yuklab bo'lmadi (fayl katta yoki topilmadi).")
            
    except Exception as e:
        print(f"Audio xatosi: {e}")
        await status_msg.edit_text("😔 Xatolik yuz berdi.")
    finally:
        if video_path and os.path.exists(video_path): os.remove(video_path)
        if file_path and os.path.exists(file_path): os.remove(file_path)

@dp.callback_query(F.data.startswith("doc_"))
async def process_document(cq: CallbackQuery):
    file_path = file_cache.get(cq.data.split("_")[1])
    if not file_path or not os.path.exists(file_path): return await cq.answer("❌ Fayl topilmadi.", show_alert=True)
    
    await cq.answer("📁 Yuborilmoqda...")
    try: await cq.message.edit_reply_markup(reply_markup=None)
    except: pass
    try:
        await cq.message.answer_document(document=FSInputFile(file_path), caption="📁 Mana HD faylingiz!")
        os.remove(file_path)
        file_cache.pop(cq.data.split("_")[1], None)
    except Exception: await cq.message.reply("😔 Xatolik.")

# ============================================================
#  INLINE REJIM
# ============================================================
@dp.inline_query()
async def inline_handler(query: InlineQuery):
    text = query.query.strip()
    if not text: return await query.answer([], switch_pm_text="Havola yoki qo'shiq nomini yuboring...", switch_pm_parameter="start")
    
    bot_username = (await bot.get_me()).username
    link = f"https://t.me/{bot_username}?start=inline_{text}"
    
    result = InlineQueryResultArticle(
        id="1", title="🎬 Yuklab olish",
        description="Natijani olish uchun bosing",
        input_message_content=InputTextMessageContent(message_text=f"Natijani olish uchun shu yerga bosing: {link}")
    )
    await query.answer([result], cache_time=1)

# ============================================================
#  ADMIN PANEL
# ============================================================
@dp.message(Command("admin"))
async def admin_panel(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    users_count = db_query("SELECT COUNT(*) FROM users")[0][0]
    await message.answer(f"👑 <b>Admin Panel</b>\n\n👥 Foydalanuvchilar: {users_count} ta\n\nBuyruqlar:\n/stats\n/broadcast\n/maintenance\n/addchannel\n/delchannel\n/ban\n/unban", parse_mode="HTML")

@dp.message(Command("stats"))
async def stats(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    users = db_query("SELECT COUNT(*) FROM users")[0][0]
    await message.answer(f"📊 Foydalanuvchilar soni: {users} ta\nKunlik limit: {DAILY_LIMIT} ta")

@dp.message(Command("maintenance"))
async def toggle_maintenance(message: types.Message):
    global maintenance_mode
    if message.from_user.id != ADMIN_ID: return
    maintenance_mode = not maintenance_mode
    await message.answer(f"🛠 Texnik ishlar: {'Yoqildi' if maintenance_mode else 'O\'chirildi'}")

@dp.message(Command("addchannel"))
async def add_channel(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    try:
        parts = message.text.split(" ", 2)
        if len(parts) < 3: return await message.answer("Format: /addchannel @username Kanal_Nomi")
        ch_id, ch_name = parts[1], parts[2]
        db_query("INSERT INTO channels (channel_id, name, url) VALUES (?, ?, ?)", (ch_id, f"📢 {ch_name}", f"https://t.me/{ch_id.replace('@', '')}"))
        await message.answer(f"✅ {ch_name} qo'shildi!")
    except Exception: await message.answer("Xatolik. Kanal allaqachon qo'shilgan.")

@dp.message(Command("delchannel"))
async def del_channel(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    parts = message.text.split(" ")
    if len(parts) < 2: return await message.answer("Format: /delchannel @username")
    db_query("DELETE FROM channels WHERE channel_id=?", (parts[1],))
    await message.answer("✅ Kanal o'chirildi.")

@dp.message(Command("ban"))
async def ban_user(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    args = message.text.split()
    if len(args) < 2: return await message.answer("Format: /ban 123456789")
    db_query("UPDATE users SET is_banned=1 WHERE user_id=?", (int(args[1]),))
    await message.answer(f"🚫 Foydalanuvchi {args[1]} bloklandi.")

@dp.message(Command("unban"))
async def unban_user(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    args = message.text.split()
    if len(args) < 2: return await message.answer("Format: /unban 123456789")
    db_query("UPDATE users SET is_banned=0 WHERE user_id=?", (int(args[1]),))
    await message.answer(f"✅ Foydalanuvchi {args[1]} blokdan chiqarildi.")

@dp.message(Command("broadcast"))
async def broadcast(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    if not message.reply_to_message: return await message.answer("Biror xabarga reply qilib /broadcast yozing.")
    users = db_query("SELECT user_id FROM users")
    count = 0
    for u in users:
        try:
            await message.reply_to_message.copy_to(u[0])
            count += 1
            await asyncio.sleep(0.05)
        except: pass
    await message.answer(f"✅ Xabar {count} ta odamga yuborildi.")

# ============================================================
#  WEBHOOK VA FASTAPI
# ============================================================
@app.post(WEBHOOK_PATH)
async def webhook_handler(request: Request):
    update = types.Update.model_validate(await request.json())
    await dp.feed_update(bot, update)
    return {"status": "ok"}

@app.get("/")
async def health_check(): return {"status": "healthy"}

@app.on_event("startup")
async def on_startup():
    if BASE_WEBHOOK_URL: await bot.set_webhook(f"{BASE_WEBHOOK_URL}{WEBHOOK_PATH}")

@app.on_event("shutdown")
async def on_shutdown(): await bot.delete_webhook()

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
