"""
Modir - ربات مدیریت گروه تلگرام
python-telegram-bot v21+ و PostgreSQL (asyncpg)

متغیرهای لازم: BOT_TOKEN ، DATABASE_URL
"""
import html
import json
import logging
import os
import re
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from functools import wraps

import asyncpg
from telegram import (
    BotCommand,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatMemberStatus, ChatType, ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("modir")

GROUP_TYPES = (ChatType.GROUP, ChatType.SUPERGROUP)

DEFAULTS = {
    "lock_links": True,
    "lock_forward": True,
    "anti_flood": True,
    "welcome": True,
    "word_filter": False,
    "bad_words": [],
    "welcome_text": "👋 {name} به گروه «{title}» خوش آمدید!\nلطفاً قوانین را با /rules بخوانید.",
    "max_warns": 3,
    "rules": "قوانین هنوز تنظیم نشده است. ادمین‌ها با /setrules آن را بنویسند.",
}
TOGGLES = {
    "lock_links": "🔗 قفل لینک",
    "lock_forward": "↪️ قفل فوروارد",
    "anti_flood": "⚡ ضد اسپم",
    "welcome": "👋 خوش‌آمدگویی",
    "word_filter": "🚫 فیلتر کلمات",
}

LINK_RE = re.compile(r"(https?://|www\.|t\.me/|telegram\.me/)", re.I)
FLOOD_MESSAGES = 7  # تعداد پیام
FLOOD_SECONDS = 10  # در این بازه
FLOOD_MUTE_MIN = 5  # مدت سکوت

flood = defaultdict(deque)
_admin_cache: dict = {}  # (chat_id, user_id) -> (زمان، ادمین بودن)


# ───────────────────────── دیتابیس ─────────────────────────
async def init_db(app: Application):
    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"])
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS modir_chats (
            chat_id BIGINT PRIMARY KEY,
            data JSONB NOT NULL DEFAULT '{}'::jsonb
        );
        CREATE TABLE IF NOT EXISTS modir_warns (
            chat_id BIGINT,
            user_id BIGINT,
            n INT NOT NULL DEFAULT 0,
            PRIMARY KEY (chat_id, user_id)
        );
        """
    )
    app.bot_data["pool"] = pool
    app.bot_data["cache"] = {}
    # لیست دستورات منوی «/» تلگرام (بدون نیاز به BotFather)
    await app.bot.set_my_commands(
        [
            BotCommand("help", "راهنما و لیست دستورات"),
            BotCommand("rules", "قوانین گروه"),
            BotCommand("settings", "تنظیمات گروه (ادمین‌ها)"),
            BotCommand("setrules", "تعیین قوانین (ادمین‌ها)"),
            BotCommand("setwelcome", "تعیین پیام خوش‌آمدگویی (ادمین‌ها)"),
            BotCommand("warn", "اخطار به کاربر (ریپلای)"),
            BotCommand("unwarn", "کم کردن اخطار (ریپلای)"),
            BotCommand("ban", "بن کاربر (ریپلای)"),
            BotCommand("unban", "رفع بن (ریپلای)"),
            BotCommand("mute", "بی‌صدا کردن (ریپلای)"),
            BotCommand("unmute", "رفع بی‌صدایی (ریپلای)"),
            BotCommand("addword", "افزودن کلمه ممنوعه"),
            BotCommand("delword", "حذف کلمه ممنوعه"),
            BotCommand("words", "لیست کلمات ممنوعه"),
        ]
    )


async def close_db(app: Application):
    await app.bot_data["pool"].close()


async def get_settings(ctx, chat_id: int) -> dict:
    cache = ctx.bot_data["cache"]
    if chat_id not in cache:
        raw = await ctx.bot_data["pool"].fetchval(
            "SELECT data FROM modir_chats WHERE chat_id=$1", chat_id
        )
        cache[chat_id] = {**DEFAULTS, **(json.loads(raw) if raw else {})}
    return cache[chat_id]


async def save_settings(ctx, chat_id: int, s: dict):
    ctx.bot_data["cache"][chat_id] = s
    await ctx.bot_data["pool"].execute(
        """
        INSERT INTO modir_chats (chat_id, data) VALUES ($1, $2::jsonb)
        ON CONFLICT (chat_id) DO UPDATE SET data = EXCLUDED.data
        """,
        chat_id,
        json.dumps(s),
    )


async def change_warn(ctx, chat_id: int, user_id: int, delta: int) -> int:
    return await ctx.bot_data["pool"].fetchval(
        """
        INSERT INTO modir_warns (chat_id, user_id, n) VALUES ($1, $2, GREATEST($3, 0))
        ON CONFLICT (chat_id, user_id)
        DO UPDATE SET n = GREATEST(modir_warns.n + $3, 0)
        RETURNING n
        """,
        chat_id,
        user_id,
        delta,
    )


async def reset_warn(ctx, chat_id: int, user_id: int):
    await ctx.bot_data["pool"].execute(
        "DELETE FROM modir_warns WHERE chat_id=$1 AND user_id=$2", chat_id, user_id
    )


# ───────────────────────── ابزارها ─────────────────────────
async def user_is_admin(chat, user_id: int) -> bool:
    key = (chat.id, user_id)
    cached = _admin_cache.get(key)
    if cached and time.monotonic() - cached[0] < 60:
        return cached[1]
    member = await chat.get_member(user_id)
    ok = member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
    _admin_cache[key] = (time.monotonic(), ok)
    return ok


async def is_admin(update: Update) -> bool:
    msg, chat = update.effective_message, update.effective_chat
    # ادمین ناشناس: پیام از طرف خود گروه ارسال می‌شود
    if msg and msg.sender_chat and msg.sender_chat.id == chat.id:
        return True
    user = update.effective_user
    return bool(user) and await user_is_admin(chat, user.id)


def group_admin(fn):
    """فقط داخل گروه و فقط برای ادمین‌ها."""

    @wraps(fn)
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        msg, chat = update.effective_message, update.effective_chat
        if chat.type not in GROUP_TYPES:
            await msg.reply_text("این دستور فقط داخل گروه کار می‌کند.")
            return
        if not await is_admin(update):
            await msg.reply_text("⛔ این دستور فقط برای ادمین‌هاست.")
            return
        try:
            await fn(update, ctx)
        except TelegramError as e:
            log.warning("telegram error: %s", e)
            await msg.reply_text(
                "ربات دسترسی لازم را ندارد. آن را ادمین کنید و دسترسی "
                "«محدود کردن اعضا» و «حذف پیام‌ها» را بدهید."
            )

    return wrapper


async def get_target(update: Update):
    msg = update.effective_message
    reply = msg.reply_to_message
    if not reply or not reply.from_user:
        await msg.reply_text("روی پیام کاربر ریپلای کنید.")
        return None
    user = reply.from_user
    if await user_is_admin(update.effective_chat, user.id):
        await msg.reply_text("این دستور روی ادمین‌ها اجرا نمی‌شود.")
        return None
    return user


def has_link(msg) -> bool:
    entities = list(msg.entities or []) + list(msg.caption_entities or [])
    if any(e.type in ("url", "text_link") for e in entities):
        return True
    return bool(LINK_RE.search(msg.text or msg.caption or ""))


async def apply_warn(ctx, chat, user, reason: str) -> str:
    s = await get_settings(ctx, chat.id)
    n = await change_warn(ctx, chat.id, user.id, 1)
    if n >= s["max_warns"]:
        await chat.ban_member(user.id)
        await reset_warn(ctx, chat.id, user.id)
        return f"🚫 {user.mention_html()} به دلیل {reason} و رسیدن به حداکثر اخطار بن شد."
    return f"⚠️ {user.mention_html()} اخطار گرفت ({n}/{s['max_warns']}) - {reason}"


# ───────────────────────── دستورات ─────────────────────────
HELP_TEXT = (
    "دستورات (ریپلای روی پیام کاربر، فقط ادمین‌ها):\n"
    "/warn - اخطار\n"
    "/unwarn - کم کردن اخطار\n"
    "/ban - بن\n"
    "/unban - رفع بن\n"
    "/mute - بی‌صدا\n"
    "/unmute - رفع بی‌صدایی\n\n"
    "برای همه: /rules - قوانین\n"
    "ادمین‌ها: /settings - تنظیمات ، /setrules متن - تعیین قوانین\n"
    "/setwelcome متن - پیام خوش‌آمدگویی ({name} و {title})\n"
    "/addword کلمه - افزودن کلمه ممنوعه ، /delword کلمه - حذف ، /words - لیست"
)


START_TEXT = "سلام! من ربات مدیریت گروه هستم 👋\nمرا به گروه اضافه کنید و ادمین کنید."


def main_menu(username: str) -> InlineKeyboardMarkup:
    add_url = (
        f"https://t.me/{username}?startgroup=true"
        "&admin=delete_messages+restrict_members"
    )
    rows = [
        [InlineKeyboardButton("➕ اضافه کردن ربات به گروه", url=add_url)],
        [InlineKeyboardButton("📋 لیست دستورات", callback_data="m:help")],
    ]
    support = os.environ.get("SUPPORT_URL")  # اختیاری، مثلاً https://t.me/آیدی_شما
    if support:
        rows.append([InlineKeyboardButton("🛟 پشتیبانی", url=support)])
    return InlineKeyboardMarkup(rows)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        START_TEXT, reply_markup=main_menu(ctx.bot.username)
    )


async def on_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.data == "m:help":
        back = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔙 بازگشت", callback_data="m:home")]]
        )
        await q.edit_message_text(HELP_TEXT, reply_markup=back)
    else:
        await q.edit_message_text(START_TEXT, reply_markup=main_menu(ctx.bot.username))
    await q.answer()


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(HELP_TEXT)


async def cmd_rules(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type not in GROUP_TYPES:
        await update.effective_message.reply_text("این دستور فقط داخل گروه کار می‌کند.")
        return
    s = await get_settings(ctx, update.effective_chat.id)
    await update.effective_message.reply_text("📜 قوانین گروه:\n\n" + s["rules"])


@group_admin
async def cmd_setrules(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    text = " ".join(ctx.args) if ctx.args else None
    if not text and msg.reply_to_message:
        text = msg.reply_to_message.text
    if not text:
        await msg.reply_text("بنویسید: /setrules متن قوانین (یا روی یک پیام ریپلای کنید)")
        return
    s = await get_settings(ctx, update.effective_chat.id)
    s["rules"] = text
    await save_settings(ctx, update.effective_chat.id, s)
    await msg.reply_text("✅ قوانین ذخیره شد.")


@group_admin
async def cmd_setwelcome(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    parts = (msg.text or "").split(None, 1)
    if len(parts) < 2:
        await msg.reply_text(
            "بنویسید: /setwelcome متن خوش‌آمدگویی\n"
            "می‌توانید از {name} (نام کاربر) و {title} (نام گروه) استفاده کنید."
        )
        return
    s = await get_settings(ctx, update.effective_chat.id)
    s["welcome_text"] = parts[1]
    await save_settings(ctx, update.effective_chat.id, s)
    await msg.reply_text("✅ پیام خوش‌آمدگویی ذخیره شد.")


@group_admin
async def cmd_addword(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    new = [w.lower() for w in ctx.args]
    if not new:
        await msg.reply_text("بنویسید: /addword کلمه1 کلمه2")
        return
    s = await get_settings(ctx, update.effective_chat.id)
    s["bad_words"] = sorted(set(s["bad_words"]) | set(new))
    await save_settings(ctx, update.effective_chat.id, s)
    await msg.reply_text(
        f"✅ {len(new)} کلمه اضافه شد. فیلتر کلمات را از /settings روشن کنید."
    )


@group_admin
async def cmd_delword(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    gone = {w.lower() for w in ctx.args}
    if not gone:
        await msg.reply_text("بنویسید: /delword کلمه")
        return
    s = await get_settings(ctx, update.effective_chat.id)
    s["bad_words"] = [w for w in s["bad_words"] if w not in gone]
    await save_settings(ctx, update.effective_chat.id, s)
    await msg.reply_text("✅ حذف شد.")


@group_admin
async def cmd_words(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = await get_settings(ctx, update.effective_chat.id)
    words = "، ".join(s["bad_words"]) or "خالی است"
    await update.effective_message.reply_text(f"🚫 کلمات ممنوعه:\n{words}")


def settings_kb(s: dict) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"{label}: {'✅' if s[k] else '❌'}", callback_data=f"t:{k}")]
        for k, label in TOGGLES.items()
    ]
    rows.append(
        [InlineKeyboardButton(f"⚠️ حداکثر اخطار: {s['max_warns']}", callback_data="w:cycle")]
    )
    return InlineKeyboardMarkup(rows)


@group_admin
async def cmd_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = await get_settings(ctx, update.effective_chat.id)
    await update.effective_message.reply_text("⚙️ تنظیمات گروه:", reply_markup=settings_kb(s))


async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    chat = q.message.chat
    if not await user_is_admin(chat, q.from_user.id):
        await q.answer("فقط ادمین‌ها می‌توانند.", show_alert=True)
        return
    s = await get_settings(ctx, chat.id)
    kind, key = q.data.split(":", 1)
    if kind == "t" and key in TOGGLES:
        s[key] = not s[key]
    elif kind == "w":
        s["max_warns"] = 2 if s["max_warns"] >= 5 else s["max_warns"] + 1
    await save_settings(ctx, chat.id, s)
    await q.edit_message_reply_markup(settings_kb(s))
    await q.answer("ذخیره شد")


@group_admin
async def cmd_warn(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = await get_target(update)
    if user:
        text = await apply_warn(ctx, update.effective_chat, user, "تصمیم ادمین")
        await update.effective_chat.send_message(text, parse_mode=ParseMode.HTML)


@group_admin
async def cmd_unwarn(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = await get_target(update)
    if user:
        s = await get_settings(ctx, update.effective_chat.id)
        n = await change_warn(ctx, update.effective_chat.id, user.id, -1)
        await update.effective_chat.send_message(
            f"✅ یک اخطار {user.mention_html()} کم شد ({n}/{s['max_warns']}).",
            parse_mode=ParseMode.HTML,
        )


@group_admin
async def cmd_ban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = await get_target(update)
    if user:
        await update.effective_chat.ban_member(user.id)
        await update.effective_chat.send_message(
            f"🚫 {user.mention_html()} بن شد.", parse_mode=ParseMode.HTML
        )


@group_admin
async def cmd_unban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    reply = update.effective_message.reply_to_message
    if not reply or not reply.from_user:
        await update.effective_message.reply_text("روی پیام کاربر ریپلای کنید.")
        return
    user = reply.from_user
    await update.effective_chat.unban_member(user.id, only_if_banned=True)
    await update.effective_chat.send_message(
        f"✅ بن {user.mention_html()} برداشته شد.", parse_mode=ParseMode.HTML
    )


@group_admin
async def cmd_mute(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = await get_target(update)
    if user:
        await update.effective_chat.restrict_member(
            user.id, ChatPermissions(can_send_messages=False)
        )
        await update.effective_chat.send_message(
            f"🔇 {user.mention_html()} بی‌صدا شد.", parse_mode=ParseMode.HTML
        )


@group_admin
async def cmd_unmute(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    reply = update.effective_message.reply_to_message
    if not reply or not reply.from_user:
        await update.effective_message.reply_text("روی پیام کاربر ریپلای کنید.")
        return
    user = reply.from_user
    await update.effective_chat.restrict_member(user.id, ChatPermissions.all_permissions())
    await update.effective_chat.send_message(
        f"🔊 {user.mention_html()} از بی‌صدایی درآمد.", parse_mode=ParseMode.HTML
    )


# ───────────────────── کنترل خودکار پیام‌ها ─────────────────────
async def moderate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if not msg or not user or chat.type not in GROUP_TYPES:
        return
    if msg.sender_chat:  # ادمین ناشناس یا پست کانال
        return
    try:
        if await user_is_admin(chat, user.id):
            return
        s = await get_settings(ctx, chat.id)

        if s["lock_forward"] and msg.forward_origin:
            await msg.delete()
            return

        if s["lock_links"] and has_link(msg):
            await msg.delete()
            text = await apply_warn(ctx, chat, user, "ارسال لینک")
            await chat.send_message(text, parse_mode=ParseMode.HTML)
            return

        if s["word_filter"] and s["bad_words"]:
            low = (msg.text or msg.caption or "").lower()
            if any(w in low for w in s["bad_words"]):
                await msg.delete()
                text = await apply_warn(ctx, chat, user, "ارسال کلمه ممنوعه")
                await chat.send_message(text, parse_mode=ParseMode.HTML)
                return

        if s["anti_flood"]:
            now = time.monotonic()
            dq = flood[(chat.id, user.id)]
            dq.append(now)
            while dq and now - dq[0] > FLOOD_SECONDS:
                dq.popleft()
            if len(dq) >= FLOOD_MESSAGES:
                dq.clear()
                until = datetime.now(timezone.utc) + timedelta(minutes=FLOOD_MUTE_MIN)
                await chat.restrict_member(
                    user.id, ChatPermissions(can_send_messages=False), until_date=until
                )
                await chat.send_message(
                    f"🔇 {user.mention_html()} به دلیل ارسال پیاپی، {FLOOD_MUTE_MIN} دقیقه بی‌صدا شد.",
                    parse_mode=ParseMode.HTML,
                )
    except TelegramError as e:
        log.warning("moderation error in %s: %s", chat.id, e)


async def on_new_members(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat, msg = update.effective_chat, update.effective_message
    s = await get_settings(ctx, chat.id)
    if not s["welcome"]:
        return
    for member in msg.new_chat_members:
        if member.is_bot:
            continue
        text = (
            html.escape(s["welcome_text"])
            .replace("{name}", member.mention_html())
            .replace("{title}", html.escape(chat.title or ""))
        )
        await chat.send_message(text, parse_mode=ParseMode.HTML)


# ───────────────────────── اجرا ─────────────────────────
def main():
    app = (
        Application.builder()
        .token(os.environ["BOT_TOKEN"])
        .post_init(init_db)
        .post_shutdown(close_db)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("rules", cmd_rules))
    app.add_handler(CommandHandler("setrules", cmd_setrules))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("warn", cmd_warn))
    app.add_handler(CommandHandler("unwarn", cmd_unwarn))
    app.add_handler(CommandHandler("ban", cmd_ban))
    app.add_handler(CommandHandler("unban", cmd_unban))
    app.add_handler(CommandHandler("mute", cmd_mute))
    app.add_handler(CommandHandler("unmute", cmd_unmute))
    app.add_handler(CommandHandler("setwelcome", cmd_setwelcome))
    app.add_handler(CommandHandler("addword", cmd_addword))
    app.add_handler(CommandHandler("delword", cmd_delword))
    app.add_handler(CommandHandler("words", cmd_words))
    app.add_handler(CallbackQueryHandler(on_menu, pattern=r"^m:"))
    app.add_handler(CallbackQueryHandler(on_callback, pattern=r"^[tw]:"))
    app.add_handler(
        MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS & filters.ChatType.GROUPS, on_new_members)
    )
    app.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS & ~filters.COMMAND & ~filters.StatusUpdate.ALL,
            moderate,
        ),
        group=1,
    )
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
