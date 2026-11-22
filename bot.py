import os
import re
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import Update, ChatPermissions
from telegram.constants import ChatMemberStatus, MessageEntityType
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("modir")

TOKEN = os.environ["BOT_TOKEN"]
MAX_WARNS = int(os.environ.get("MAX_WARNS", "3"))

RULES_TEXT = os.environ.get(
    "RULES_TEXT",
    "📜 قوانین گروه تفسیر آثار و علائم باستانی\n\n"
    "۱. احترام به همه اعضا و پرهیز از توهین، تمسخر و بحث‌های شخصی.\n"
    "۲. گفتگو فقط درباره آثار باستانی، نشانه‌ها و علائم و موضوعات مرتبط باشد.\n"
    "۳. هنگام ارسال عکس یا نقش، محل و توضیح مختصر بنویسید تا بررسی آسان‌تر شود.\n"
    "۴. نظر خود را همراه با دلیل و منبع مطرح کنید و ادعاهای بدون سند را قطعی بیان نکنید.\n"
    "۵. ارسال لینک، تبلیغات و پیام‌های تکراری ممنوع است.\n"
    "۶. مسائل سیاسی، مذهبی و قومیتی در گروه مطرح نشود.\n"
    "۷. حفاری غیرمجاز و خرید و فروش آثار تاریخی در گروه ممنوع است.\n\n"
    "رعایت نکردن قوانین به اخطار و در صورت تکرار به حذف از گروه منجر می‌شود.",
)
WELCOME_TEXT = os.environ.get(
    "WELCOME_TEXT",
    "سلام {name} 🌿\n"
    "به گروه «{group}» خوش آمدید.\n\n"
    "لطفاً قبل از شروع، قوانین را با دستور /rules بخوانید. "
    "خوشحال می‌شویم عکس‌ها و پرسش‌هایتان را با ما در میان بگذارید.",
)

LINK_RE = re.compile(r"(https?://|www\.|t\.me/|telegram\.me/|\b[a-z0-9-]+\.(com|ir|org|net|me|io)\b)", re.I)

warns = {}  # (chat_id, user_id) -> count  (با ری‌استارت پاک می‌شود)


async def is_admin(chat, user_id) -> bool:
    try:
        m = await chat.get_member(user_id)
        return m.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
    except Exception:
        return False


async def admin_only(update: Update) -> bool:
    if update.effective_chat.type == "private":
        await update.message.reply_text("این دستور فقط داخل گروه کار می‌کند.")
        return False
    if not await is_admin(update.effective_chat, update.effective_user.id):
        await update.message.reply_text("⛔ این دستور فقط برای ادمین‌هاست.")
        return False
    return True


async def get_target(update: Update):
    reply = update.message.reply_to_message
    if not reply or not reply.from_user:
        await update.message.reply_text("روی پیام کاربر ریپلای کنید و دستور را بفرستید.")
        return None
    user = reply.from_user
    if await is_admin(update.effective_chat, user.id) or user.is_bot:
        await update.message.reply_text("روی ادمین یا ربات نمی‌شود اعمال کرد.")
        return None
    return user


def mention(user) -> str:
    return user.mention_html(user.first_name or "کاربر")


# ---------- دستورات عمومی ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "سلام! من ربات مدیریت گروه هستم.\n"
        "مرا به گروه اضافه کنید و ادمین کنید. راهنما: /help"
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "دستورات (ریپلای روی پیام کاربر، فقط ادمین‌ها):\n"
        "/warn - اخطار\n"
        "/unwarn - کم کردن اخطار\n"
        "/ban - بن\n"
        "/unban - رفع بن\n"
        "/mute - بی‌صدا\n"
        "/unmute - رفع بی‌صدایی\n\n"
        "برای همه: /rules - قوانین\n"
        "ادمین‌ها: /settings - وضعیت تنظیمات"
    )


async def rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(RULES_TEXT)


async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return
    await update.message.reply_text(
        f"⚙️ تنظیمات:\n"
        f"• حذف لینک: فعال\n"
        f"• خوش‌آمدگویی: فعال\n"
        f"• حداکثر اخطار قبل از بن: {MAX_WARNS}"
    )


# ---------- مدیریت کاربران ----------
async def warn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return
    user = await get_target(update)
    if not user:
        return
    chat = update.effective_chat
    key = (chat.id, user.id)
    warns[key] = warns.get(key, 0) + 1
    count = warns[key]
    if count >= MAX_WARNS:
        await chat.ban_member(user.id)
        warns.pop(key, None)
        await update.message.reply_html(
            f"🚫 {mention(user)} به دلیل {MAX_WARNS} اخطار بن شد."
        )
    else:
        await update.message.reply_html(
            f"⚠️ {mention(user)} اخطار گرفت ({count}/{MAX_WARNS})."
        )


async def unwarn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return
    user = await get_target(update)
    if not user:
        return
    key = (update.effective_chat.id, user.id)
    warns[key] = max(0, warns.get(key, 0) - 1)
    await update.message.reply_html(
        f"✅ اخطار {mention(user)} کم شد ({warns[key]}/{MAX_WARNS})."
    )


async def ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return
    user = await get_target(update)
    if not user:
        return
    await update.effective_chat.ban_member(user.id)
    await update.message.reply_html(f"🚫 {mention(user)} بن شد.")


async def unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return
    reply = update.message.reply_to_message
    if not reply or not reply.from_user:
        await update.message.reply_text("روی پیام کاربر ریپلای کنید.")
        return
    user = reply.from_user
    await update.effective_chat.unban_member(user.id, only_if_banned=True)
    await update.message.reply_html(f"✅ {mention(user)} از بن خارج شد.")


async def mute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return
    user = await get_target(update)
    if not user:
        return
    await update.effective_chat.restrict_member(
        user.id, ChatPermissions(can_send_messages=False)
    )
    await update.message.reply_html(f"🔇 {mention(user)} بی‌صدا شد.")


async def unmute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return
    reply = update.message.reply_to_message
    if not reply or not reply.from_user:
        await update.message.reply_text("روی پیام کاربر ریپلای کنید.")
        return
    user = reply.from_user
    await update.effective_chat.restrict_member(
        user.id,
        ChatPermissions(
            can_send_messages=True,
            can_send_audios=True,
            can_send_documents=True,
            can_send_photos=True,
            can_send_videos=True,
            can_send_video_notes=True,
            can_send_voice_notes=True,
            can_send_polls=True,
            can_send_other_messages=True,
            can_add_web_page_previews=True,
        ),
    )
    await update.message.reply_html(f"🔊 {mention(user)} آزاد شد.")


# ---------- خوش‌آمدگویی ----------
async def welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    for member in update.message.new_chat_members:
        if member.is_bot:
            continue
        text = WELCOME_TEXT.format(name=member.first_name or "دوست عزیز", group=chat.title)
        await update.message.reply_text(text)


# ---------- حذف لینک ----------
async def anti_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not msg or not user:
        return
    text = msg.text or msg.caption or ""
    entities = list(msg.entities or []) + list(msg.caption_entities or [])
    has_link = any(
        e.type in (MessageEntityType.URL, MessageEntityType.TEXT_LINK)
        for e in entities
    ) or bool(LINK_RE.search(text))
    if not has_link:
        return
    if await is_admin(chat, user.id):
        return
    try:
        await msg.delete()
        await chat.send_message(
            f"⚠️ {user.first_name}، ارسال لینک در گروه ممنوع است."
        )
    except Exception as e:
        log.warning("حذف پیام ناموفق بود (ربات ادمین نیست؟): %s", e)


async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    log.error("خطا: %s", context.error)


# ---------- وب‌سرور ساده برای Render (Free Web Service) ----------
class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


def run_health_server():
    port = int(os.environ.get("PORT", "10000"))
    HTTPServer(("0.0.0.0", port), Health).serve_forever()


def main():
    threading.Thread(target=run_health_server, daemon=True).start()

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("rules", rules))
    app.add_handler(CommandHandler("settings", settings))
    app.add_handler(CommandHandler("warn", warn))
    app.add_handler(CommandHandler("unwarn", unwarn))
    app.add_handler(CommandHandler("ban", ban))
    app.add_handler(CommandHandler("unban", unban))
    app.add_handler(CommandHandler("mute", mute))
    app.add_handler(CommandHandler("unmute", unmute))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome))
    app.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS & (filters.TEXT | filters.CAPTION) & ~filters.COMMAND,
            anti_link,
        )
    )
    app.add_error_handler(on_error)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
