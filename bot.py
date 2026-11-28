"""
Modir - ربات مدیریت گروه تلگرام (اشتراکی، با دوره‌ی آزمایشی)
python-telegram-bot v21+ و PostgreSQL (asyncpg)

الزامی: BOT_TOKEN ، DATABASE_URL ، OWNER_ID
تماس:   CONTACT_PHONE ، CONTACT_NAME ، SUPPORT_URL
پرداخت: PLANS ، WALLET_USDT_TRC20 ، WALLET_TON ، WALLET_BTC ، USD_PAYMENT_INFO
سایر:   TRIAL_DAYS ، TZ_NAME
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
from zoneinfo import ZoneInfo

import asyncpg
from telegram import (
    BotCommand,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyParameters,
    Update,
)
from telegram.constants import ChatMemberStatus, ChatType, ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
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

OWNER_ID = int(os.environ.get("OWNER_ID", "0") or 0)
TRIAL_DAYS = int(os.environ.get("TRIAL_DAYS", "3") or 3)
TZ = ZoneInfo(os.environ.get("TZ_NAME", "Asia/Tehran"))
CONTACT_PHONE = os.environ.get("CONTACT_PHONE", "").strip()
CONTACT_NAME = os.environ.get("CONTACT_NAME", "پشتیبانی مدیر")
SUPPORT_URL = os.environ.get("SUPPORT_URL", "").strip()
USD_INFO = os.environ.get("USD_PAYMENT_INFO", "").strip()
WALLETS = [
    (label, os.environ[key].strip())
    for label, key in (
        ("USDT (شبکه‌ی TRC20)", "WALLET_USDT_TRC20"),
        ("TON", "WALLET_TON"),
        ("Bitcoin", "WALLET_BTC"),
    )
    if os.environ.get(key, "").strip()
]


def parse_plans() -> dict:
    """PLANS=روز:دلار,روز:دلار  مثلاً 30:5,90:13,365:45"""
    out = {}
    for part in os.environ.get("PLANS", "30:5,90:13,365:45").split(","):
        try:
            d, p = part.split(":")
            out[int(d)] = p.strip()
        except ValueError:
            continue
    return out


PLANS = parse_plans()
DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "0123456789" * 2)

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
    # اشتراک
    "trial_start": None,
    "paid_until": None,
    "reminded": None,
    # قفل گروه
    "lock_start": None,
    "lock_end": None,
    "locked": False,
    "lock_auto": False,
    "saved_perms": None,
    "suppress": None,
    "lock_err": False,
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
        CREATE TABLE IF NOT EXISTS modir_tickets (
            owner_msg_id BIGINT PRIMARY KEY,
            user_id BIGINT NOT NULL
        );
        """
    )
    app.bot_data["pool"] = pool
    app.bot_data["cache"] = {}
    await app.bot.set_my_commands(
        [
            BotCommand("help", "راهنما و لیست دستورات"),
            BotCommand("rules", "قوانین گروه"),
            BotCommand("status", "وضعیت اشتراک گروه (ادمین‌ها)"),
            BotCommand("buy", "خرید یا تمدید اشتراک (ادمین‌ها)"),
            BotCommand("support", "تماس با سازنده"),
            BotCommand("contact", "شماره تماس"),
            BotCommand("settings", "تنظیمات گروه (ادمین‌ها)"),
            BotCommand("lockschedule", "قفل و بازشدن زمان‌دار گروه"),
            BotCommand("lock", "قفل کردن گروه"),
            BotCommand("unlock", "باز کردن گروه"),
            BotCommand("setrules", "تعیین قوانین (ادمین‌ها)"),
            BotCommand("setwelcome", "پیام خوش‌آمدگویی (ادمین‌ها)"),
            BotCommand("warn", "اخطار به کاربر (ریپلای)"),
            BotCommand("unwarn", "کم کردن اخطار (ریپلای)"),
            BotCommand("ban", "بن کاربر (ریپلای)"),
            BotCommand("unban", "رفع بن (ریپلای)"),
            BotCommand("mute", "بی‌صدا کردن (ریپلای)"),
            BotCommand("unmute", "رفع بی‌صدایی (ریپلای)"),
            BotCommand("del", "حذف پیام (ریپلای)"),
            BotCommand("pin", "پین کردن پیام (ریپلای)"),
            BotCommand("unpin", "برداشتن پین (ریپلای)"),
            BotCommand("addword", "افزودن کلمه ممنوعه"),
            BotCommand("delword", "حذف کلمه ممنوعه"),
            BotCommand("words", "لیست کلمات ممنوعه"),
        ]
    )
    if app.job_queue:
        app.job_queue.run_repeating(expiry_job, interval=6 * 3600, first=60)
        app.job_queue.run_repeating(lock_job, interval=60, first=20)
    else:
        log.warning("job-queue نصب نیست؛ قفل زمان‌دار و یادآوری اشتراک کار نمی‌کند.")


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


# ───────────────────────── اشتراک ─────────────────────────
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


async def ensure_trial(ctx, chat_id: int) -> dict:
    """اولین بار که گروه از ربات استفاده کند، دوره‌ی آزمایشی شروع می‌شود."""
    s = await get_settings(ctx, chat_id)
    if not s.get("trial_start") and not s.get("paid_until"):
        s["trial_start"] = now_utc().isoformat()
        await save_settings(ctx, chat_id, s)
    return s


def premium_until(s: dict):
    ends = []
    if s.get("paid_until"):
        ends.append(datetime.fromisoformat(s["paid_until"]))
    if s.get("trial_start"):
        ends.append(datetime.fromisoformat(s["trial_start"]) + timedelta(days=TRIAL_DAYS))
    return max(ends) if ends else None


async def is_premium(ctx, chat_id: int) -> bool:
    s = await ensure_trial(ctx, chat_id)
    until = premium_until(s)
    return bool(until and until > now_utc())


async def activate(ctx, chat_id: int, days: int) -> datetime:
    s = await get_settings(ctx, chat_id)
    base = now_utc()
    if s.get("paid_until"):
        base = max(base, datetime.fromisoformat(s["paid_until"]))
    until = base + timedelta(days=days)
    s["paid_until"] = until.isoformat()
    s["reminded"] = None
    await save_settings(ctx, chat_id, s)
    return until


def lock_text() -> str:
    return (
        "🔒 این قابلیت مخصوص اشتراک است و اشتراک یا دوره‌ی آزمایشی این گروه تمام شده است.\n"
        "برای خرید یا تمدید: /buy (ادمین‌ها) ، برای تماس با سازنده: /support"
    )


def premium_only(fn):
    """قابلیت ویژه: فقط وقتی دوره‌ی آزمایشی یا اشتراک فعال است."""

    @wraps(fn)
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not await is_premium(ctx, update.effective_chat.id):
            await update.effective_message.reply_text(lock_text())
            return
        await fn(update, ctx)

    return wrapper


async def expiry_job(ctx: ContextTypes.DEFAULT_TYPE):
    """یادآوری ۲۴ ساعت قبل از پایان اشتراک + پاکسازی حافظه‌ی ضد اسپم."""
    now = now_utc()
    rows = await ctx.bot_data["pool"].fetch("SELECT chat_id, data FROM modir_chats")
    for r in rows:
        try:
            s = {**DEFAULTS, **json.loads(r["data"])}
            until = premium_until(s)
            if not until or not (now < until <= now + timedelta(hours=24)):
                continue
            if s.get("reminded") == until.isoformat():
                continue
            s = await get_settings(ctx, r["chat_id"])
            s["reminded"] = until.isoformat()
            await save_settings(ctx, r["chat_id"], s)
            await ctx.bot.send_message(
                r["chat_id"],
                "⏳ اشتراک یا دوره‌ی آزمایشی این گروه کمتر از ۲۴ ساعت دیگر تمام می‌شود.\n"
                "وضعیت: /status ، تمدید: /buy",
            )
        except Exception as e:  # noqa: BLE001
            log.warning("expiry reminder failed for %s: %s", r["chat_id"], e)
    mono = time.monotonic()
    for k in [k for k, v in flood.items() if not v or mono - v[-1] > 60]:
        del flood[k]


# ───────────────────── قفل و بازشدن زمان‌دار ─────────────────────
def parse_hhmm(t: str):
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", t.translate(DIGITS))
    if not m:
        return None
    h, mi = int(m[1]), int(m[2])
    return f"{h:02d}:{mi:02d}" if h < 24 and mi < 60 else None


def to_min(t: str) -> int:
    h, m = t.split(":")
    return int(h) * 60 + int(m)


def in_window(now: datetime, a: str, b: str) -> bool:
    n = now.hour * 60 + now.minute
    x, y = to_min(a), to_min(b)
    if x == y:
        return False
    return x <= n < y if x < y else (n >= x or n < y)


def window_key(now: datetime, a: str, b: str) -> str:
    """شناسه‌ی همان بازه‌ی قفل (برای پنجره‌ی شبانه مثل ۲۳:۰۰ تا ۰۷:۰۰)."""
    d = now.date()
    if to_min(a) > to_min(b) and now.hour * 60 + now.minute < to_min(b):
        d -= timedelta(days=1)
    return d.isoformat()


async def set_group_lock(ctx, chat_id: int, locked: bool, s: dict):
    bot = ctx.bot
    if locked:
        if not s.get("saved_perms"):
            chat = await bot.get_chat(chat_id)
            s["saved_perms"] = chat.permissions.to_dict() if chat.permissions else None
        await bot.set_chat_permissions(chat_id, ChatPermissions(can_send_messages=False))
        s["locked"] = True
    else:
        perms = None
        if s.get("saved_perms"):
            perms = ChatPermissions.de_json(s["saved_perms"], bot)
        await bot.set_chat_permissions(chat_id, perms or ChatPermissions.all_permissions())
        s["locked"] = False
        s["saved_perms"] = None
    await save_settings(ctx, chat_id, s)


async def lock_job(ctx: ContextTypes.DEFAULT_TYPE):
    rows = await ctx.bot_data["pool"].fetch(
        "SELECT chat_id FROM modir_chats WHERE data->>'lock_start' IS NOT NULL"
    )
    now = datetime.now(TZ)
    for r in rows:
        cid = r["chat_id"]
        try:
            s = await get_settings(ctx, cid)
            if not s.get("lock_start") or not s.get("lock_end"):
                continue
            key = window_key(now, s["lock_start"], s["lock_end"])
            want = (
                in_window(now, s["lock_start"], s["lock_end"])
                and s.get("suppress") != key
                and await is_premium(ctx, cid)
            )
            if want and not s["locked"]:
                s["lock_auto"] = True
                await set_group_lock(ctx, cid, True, s)
                await ctx.bot.send_message(
                    cid, f"🔒 گروه قفل شد و ساعت {s['lock_end']} دوباره باز می‌شود."
                )
            elif not want and s["locked"] and s.get("lock_auto"):
                await set_group_lock(ctx, cid, False, s)
                await ctx.bot.send_message(cid, "🔓 گروه باز شد. گفتگو آزاد است.")
            if s.get("lock_err"):
                s["lock_err"] = False
                await save_settings(ctx, cid, s)
        except TelegramError as e:
            log.warning("lock job error in %s: %s", cid, e)
            s = await get_settings(ctx, cid)
            if not s.get("lock_err"):
                s["lock_err"] = True
                await save_settings(ctx, cid, s)
                try:
                    await ctx.bot.send_message(
                        cid,
                        "⚠️ قفل زمان‌دار انجام نشد. ربات را ادمین کنید و دسترسی "
                        "«محدود کردن اعضا» را بدهید.",
                    )
                except TelegramError:
                    pass


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


# ───────────────────────── متن‌ها و منو ─────────────────────────
HELP_TEXT = (
    "🆓 رایگان: قفل لینک، خوش‌آمدگویی، /rules ، /del ، /pin\n"
    "⭐ ویژه (دوره‌ی آزمایشی یا اشتراک): اخطار و بن و سکوت، تنظیمات، فیلتر کلمات، "
    "ضد اسپم، قفل فوروارد، قفل گروه\n\n"
    "دستورات ریپلای (ادمین‌ها):\n"
    "/warn /unwarn /ban /unban /mute /unmute /del /pin /unpin\n\n"
    "قفل گروه (ادمین‌ها):\n"
    "/lock و /unlock - قفل و باز کردن فوری\n"
    "/lockschedule 23:00 07:00 - هر شب قفل و صبح باز\n"
    "/lockschedule off - خاموش کردن قفل زمان‌دار\n\n"
    "تنظیمات (ادمین‌ها):\n"
    "/settings ، /setrules متن ، /setwelcome متن ({name} و {title})\n"
    "/addword کلمه ، /delword کلمه ، /words\n\n"
    "اشتراک: /status وضعیت ، /buy خرید و تمدید\n"
    "تماس: /support پیام به سازنده ، /contact شماره تماس"
)

START_TEXT = (
    "سلام! من ربات مدیریت گروه هستم 👋\n"
    f"مرا به گروه اضافه کنید و ادمین کنید. {TRIAL_DAYS} روز اول همه‌ی قابلیت‌ها رایگان است."
)


def main_menu(username: str) -> InlineKeyboardMarkup:
    add_url = (
        f"https://t.me/{username}?startgroup=true"
        "&admin=delete_messages+restrict_members+pin_messages"
    )
    rows = [
        [InlineKeyboardButton("➕ اضافه کردن ربات به گروه", url=add_url)],
        [InlineKeyboardButton("📋 لیست دستورات", callback_data="m:help")],
        [
            InlineKeyboardButton("💬 تماس با سازنده", callback_data="m:support"),
            InlineKeyboardButton("📞 شماره تماس", callback_data="m:phone"),
        ],
    ]
    if SUPPORT_URL:
        rows.append([InlineKeyboardButton("🛟 پشتیبانی در تلگرام", url=SUPPORT_URL)])
    return InlineKeyboardMarkup(rows)


async def send_contact_card(ctx, chat_id: int):
    if not CONTACT_PHONE:
        await ctx.bot.send_message(chat_id, "شماره تماس هنوز تنظیم نشده. از /support پیام بدهید.")
        return
    await ctx.bot.send_message(chat_id, f"📞 شماره تماس مستقیم:\n{CONTACT_PHONE}")
    try:
        await ctx.bot.send_contact(
            chat_id, phone_number=CONTACT_PHONE, first_name=CONTACT_NAME
        )
    except TelegramError as e:
        log.warning("send_contact failed: %s", e)


async def begin_support(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not OWNER_ID:
        await update.effective_message.reply_text("ارتباط با سازنده فعلاً در دسترس نیست.")
        return
    ctx.user_data["mode"] = {"type": "support"}
    await update.effective_message.reply_text(
        "💬 پیام خود را بنویسید (متن، عکس یا فایل). برای انصراف: /cancel"
    )


def invoice_text(chat_id: int, days: int) -> str:
    lines = [
        "💳 صورت‌حساب",
        f"گروه: {chat_id}",
        f"مدت: {days} روز",
        f"مبلغ: {html.escape(PLANS[days])} دلار",
        "",
    ]
    if WALLETS:
        lines.append("پرداخت با ارز دیجیتال (روی آدرس بزنید تا کپی شود):")
        for label, addr in WALLETS:
            lines.append(f"{html.escape(label)}:\n<code>{html.escape(addr)}</code>")
    if USD_INFO:
        lines.append("")
        lines.append(html.escape(USD_INFO))
    if not WALLETS and not USD_INFO:
        lines.append("روش پرداخت هنوز تنظیم نشده؛ از /support پیام بدهید.")
    lines.append("")
    lines.append(
        "بعد از پرداخت، شناسه‌ی تراکنش (TXID) یا عکس رسید را همین‌جا بفرستید "
        "تا بررسی و اشتراک فعال شود. برای انصراف: /cancel"
    )
    return "\n".join(lines)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, chat = update.effective_message, update.effective_chat
    if chat.type == ChatType.PRIVATE and ctx.args:
        arg = ctx.args[0]
        if arg == "support":
            await begin_support(update, ctx)
            return
        m = re.fullmatch(r"buy_(-?\d+)_(\d+)", arg)
        if m:
            cid, days = int(m[1]), int(m[2])
            if days not in PLANS:
                await msg.reply_text("این پلن معتبر نیست. دوباره /buy بزنید.")
                return
            ctx.user_data["mode"] = {"type": "pay", "chat_id": cid, "days": days}
            await msg.reply_text(invoice_text(cid, days), parse_mode=ParseMode.HTML)
            return
    await msg.reply_text(START_TEXT, reply_markup=main_menu(ctx.bot.username))


async def on_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.data == "m:help":
        back = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔙 بازگشت", callback_data="m:home")]]
        )
        await q.edit_message_text(HELP_TEXT, reply_markup=back)
    elif q.data == "m:support":
        await q.answer()
        await begin_support(update, ctx)
        return
    elif q.data == "m:phone":
        await q.answer()
        await send_contact_card(ctx, q.message.chat.id)
        return
    else:
        await q.edit_message_text(START_TEXT, reply_markup=main_menu(ctx.bot.username))
    await q.answer()


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(HELP_TEXT)


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.pop("mode", None)
    await update.effective_message.reply_text("لغو شد.")


async def cmd_contact(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await send_contact_card(ctx, update.effective_chat.id)


async def cmd_support(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == ChatType.PRIVATE:
        await begin_support(update, ctx)
        return
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("💬 پیام به سازنده", url=f"https://t.me/{ctx.bot.username}?start=support")]]
    )
    await update.effective_message.reply_text(
        "برای تماس با سازنده روی دکمه بزنید:", reply_markup=kb
    )


async def cmd_rules(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type not in GROUP_TYPES:
        await update.effective_message.reply_text("این دستور فقط داخل گروه کار می‌کند.")
        return
    s = await get_settings(ctx, update.effective_chat.id)
    await update.effective_message.reply_text("📜 قوانین گروه:\n\n" + s["rules"])


# ───────────────────── اشتراک: دستورات و پرداخت ─────────────────────
@group_admin
async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = await ensure_trial(ctx, update.effective_chat.id)
    until = premium_until(s)
    now = now_utc()
    if until and until > now:
        left = (until - now).days
        text = f"✅ اشتراک/دوره‌ی آزمایشی فعال است تا {until:%Y-%m-%d} ({left} روز مانده).\nشناسه‌ی گروه: {update.effective_chat.id}"
    else:
        text = lock_text()
    await update.effective_message.reply_text(text)


@group_admin
async def cmd_buy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if not PLANS:
        await update.effective_message.reply_text("پلن‌ها هنوز تنظیم نشده. از /support پیام بدهید.")
        return
    rows = [
        [
            InlineKeyboardButton(
                f"{d} روزه - {p} دلار",
                url=f"https://t.me/{ctx.bot.username}?start=buy_{chat.id}_{d}",
            )
        ]
        for d, p in sorted(PLANS.items())
    ]
    await update.effective_message.reply_text(
        "💳 خرید اشتراک (پرداخت با ارز دیجیتال یا دلار)\nپلن را انتخاب کنید:",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def cmd_activate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """فقط مالک: /activate شناسه_گروه روز  یا داخل گروه: /activate روز"""
    user, chat, msg = update.effective_user, update.effective_chat, update.effective_message
    if not OWNER_ID or not user or user.id != OWNER_ID:
        return
    args = ctx.args
    try:
        if chat.type in GROUP_TYPES and len(args) == 1:
            chat_id, days = chat.id, int(args[0])
        else:
            chat_id, days = int(args[0]), int(args[1])
    except (ValueError, IndexError):
        await msg.reply_text("مثال: /activate شناسه_گروه 30\nیا داخل گروه: /activate 30")
        return
    until = await activate(ctx, chat_id, days)
    await msg.reply_text(f"✅ اشتراک گروه {chat_id} تا {until:%Y-%m-%d} فعال شد.")


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not OWNER_ID or not user or user.id != OWNER_ID:
        return
    rows = await ctx.bot_data["pool"].fetch("SELECT data FROM modir_chats")
    now = now_utc()
    active = paid = 0
    for r in rows:
        s = {**DEFAULTS, **json.loads(r["data"])}
        until = premium_until(s)
        if until and until > now:
            active += 1
        if s.get("paid_until") and datetime.fromisoformat(s["paid_until"]) > now:
            paid += 1
    await update.effective_message.reply_text(
        f"📊 گروه‌ها: {len(rows)}\nفعال (آزمایشی یا اشتراک): {active}\nاشتراک پولی فعال: {paid}"
    )


async def to_owner(ctx, msg, user, title: str, markup=None):
    head = f"{title}\nاز: {user.mention_html()} (id: <code>{user.id}</code>)"
    sent = await ctx.bot.send_message(
        OWNER_ID, head, parse_mode=ParseMode.HTML, reply_markup=markup
    )
    copied = await msg.copy(OWNER_ID, reply_parameters=ReplyParameters(message_id=sent.message_id))
    for mid in (sent.message_id, copied.message_id):
        await ctx.bot_data["pool"].execute(
            "INSERT INTO modir_tickets (owner_msg_id, user_id) VALUES ($1, $2) "
            "ON CONFLICT DO NOTHING",
            mid,
            user.id,
        )


async def on_private(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, user = update.effective_message, update.effective_user
    if not user:
        return
    # پاسخ مالک به پیام یک مشتری (ریپلای روی پیام ارسالی ربات)
    if user.id == OWNER_ID:
        ref = msg.reply_to_message
        if not ref:
            return
        uid = await ctx.bot_data["pool"].fetchval(
            "SELECT user_id FROM modir_tickets WHERE owner_msg_id=$1", ref.message_id
        )
        if not uid:
            return
        try:
            await msg.copy(uid)
            await msg.reply_text("✅ برای کاربر ارسال شد.")
        except TelegramError:
            await msg.reply_text("ارسال نشد؛ کاربر ربات را بلاک کرده است.")
        return

    mode = ctx.user_data.get("mode")
    if not mode or not OWNER_ID:
        await msg.reply_text(
            "برای تماس با سازنده یا دیدن راهنما، از منوی زیر استفاده کنید:",
            reply_markup=main_menu(ctx.bot.username),
        )
        return
    try:
        if mode["type"] == "support":
            await to_owner(ctx, msg, user, "💬 پیام پشتیبانی")
            await msg.reply_text("✅ پیام شما برای سازنده ارسال شد. پاسخ را همین‌جا می‌گیرید.")
        elif mode["type"] == "pay":
            cid, days = mode["chat_id"], mode["days"]
            kb = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "✅ تأیید و فعال‌سازی", callback_data=f"ap:{cid}:{days}:{user.id}"
                        ),
                        InlineKeyboardButton("❌ رد", callback_data=f"rj:{user.id}:{cid}"),
                    ]
                ]
            )
            await to_owner(
                ctx, msg, user,
                f"💳 درخواست اشتراک\nگروه: <code>{cid}</code>\nمدت: {days} روز ({PLANS.get(days, '?')} دلار)",
                kb,
            )
            await msg.reply_text("✅ رسید دریافت شد. بعد از بررسی، اشتراک فعال و اطلاع داده می‌شود.")
        ctx.user_data.pop("mode", None)
    except TelegramError as e:
        log.warning("forward to owner failed: %s", e)
        await msg.reply_text("ارسال انجام نشد. بعداً دوباره امتحان کنید.")


async def on_payment_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not OWNER_ID or q.from_user.id != OWNER_ID:
        await q.answer("فقط مالک ربات.", show_alert=True)
        return
    parts = q.data.split(":")
    try:
        if parts[0] == "ap":
            cid, days, uid = int(parts[1]), int(parts[2]), int(parts[3])
            until = await activate(ctx, cid, days)
            await q.edit_message_reply_markup(reply_markup=None)
            await q.message.reply_text(f"✅ اشتراک گروه {cid} تا {until:%Y-%m-%d} فعال شد.")
            for target, text in (
                (cid, f"✅ اشتراک این گروه تا {until:%Y-%m-%d} فعال شد. ممنون از حمایت شما!"),
                (uid, f"✅ پرداخت تأیید شد و اشتراک گروه تا {until:%Y-%m-%d} فعال است."),
            ):
                try:
                    await ctx.bot.send_message(target, text)
                except TelegramError:
                    pass
        else:
            uid, cid = int(parts[1]), int(parts[2])
            await q.edit_message_reply_markup(reply_markup=None)
            await q.message.reply_text("❌ درخواست رد شد.")
            try:
                await ctx.bot.send_message(
                    uid, "❌ پرداخت تأیید نشد. اگر اشتباهی شده از /support پیام بدهید."
                )
            except TelegramError:
                pass
        await q.answer()
    except (ValueError, IndexError):
        await q.answer("داده‌ی نامعتبر.", show_alert=True)


# ───────────────────────── تنظیمات گروه ─────────────────────────
@group_admin
@premium_only
async def cmd_setrules(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    parts = (msg.text or "").split(None, 1)
    text = parts[1] if len(parts) > 1 else None
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
@premium_only
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
@premium_only
async def cmd_addword(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    new = [w.lower() for w in ctx.args]
    if not new:
        await msg.reply_text("بنویسید: /addword کلمه1 کلمه2")
        return
    s = await get_settings(ctx, update.effective_chat.id)
    s["bad_words"] = sorted(set(s["bad_words"]) | set(new))
    await save_settings(ctx, update.effective_chat.id, s)
    await msg.reply_text(f"✅ {len(new)} کلمه اضافه شد. فیلتر کلمات را از /settings روشن کنید.")


@group_admin
@premium_only
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
@premium_only
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
@premium_only
async def cmd_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = await get_settings(ctx, update.effective_chat.id)
    await update.effective_message.reply_text("⚙️ تنظیمات گروه:", reply_markup=settings_kb(s))


async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    chat = q.message.chat
    if not await user_is_admin(chat, q.from_user.id):
        await q.answer("فقط ادمین‌ها می‌توانند.", show_alert=True)
        return
    if not await is_premium(ctx, chat.id):
        await q.answer("اشتراک یا دوره‌ی آزمایشی این گروه تمام شده است.", show_alert=True)
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


# ───────────────────── قفل گروه (دستورات) ─────────────────────
@group_admin
@premium_only
async def cmd_lockschedule(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, chat = update.effective_message, update.effective_chat
    s = await get_settings(ctx, chat.id)
    args = ctx.args
    if args and args[0].lower() in ("off", "خاموش"):
        s["lock_start"] = s["lock_end"] = None
        s["suppress"] = None
        if s["locked"] and s["lock_auto"]:
            await set_group_lock(ctx, chat.id, False, s)
        else:
            await save_settings(ctx, chat.id, s)
        await msg.reply_text("✅ قفل زمان‌دار خاموش شد.")
        return
    a = parse_hhmm(args[0]) if len(args) == 2 else None
    b = parse_hhmm(args[1]) if len(args) == 2 else None
    if not a or not b or a == b:
        await msg.reply_text(
            "مثال: /lockschedule 23:00 07:00\n"
            "یعنی هر شب ساعت ۲۳:۰۰ قفل و ساعت ۰۷:۰۰ باز شود (به وقت تهران).\n"
            "خاموش کردن: /lockschedule off"
        )
        return
    s["lock_start"], s["lock_end"], s["suppress"] = a, b, None
    await save_settings(ctx, chat.id, s)
    await msg.reply_text(
        f"✅ هر روز ساعت {a} گروه قفل و ساعت {b} باز می‌شود.\n"
        "ربات باید ادمین باشد و دسترسی «محدود کردن اعضا» داشته باشد."
    )


@group_admin
@premium_only
async def cmd_lock(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    s = await get_settings(ctx, chat.id)
    if s["locked"]:
        await update.effective_message.reply_text("گروه از قبل قفل است.")
        return
    s["lock_auto"] = False
    await set_group_lock(ctx, chat.id, True, s)
    await update.effective_message.reply_text("🔒 گروه قفل شد. برای باز کردن: /unlock")


@group_admin
@premium_only
async def cmd_unlock(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    s = await get_settings(ctx, chat.id)
    s["lock_auto"] = False
    if s.get("lock_start") and s.get("lock_end"):
        now = datetime.now(TZ)
        if in_window(now, s["lock_start"], s["lock_end"]):
            s["suppress"] = window_key(now, s["lock_start"], s["lock_end"])
    await set_group_lock(ctx, chat.id, False, s)
    await update.effective_message.reply_text("🔓 گروه باز شد.")


# ───────────────────── دستورات ادمین ─────────────────────
@group_admin
@premium_only
async def cmd_warn(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = await get_target(update)
    if user:
        text = await apply_warn(ctx, update.effective_chat, user, "تصمیم ادمین")
        await update.effective_chat.send_message(text, parse_mode=ParseMode.HTML)


@group_admin
@premium_only
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
@premium_only
async def cmd_ban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = await get_target(update)
    if user:
        await update.effective_chat.ban_member(user.id)
        await update.effective_chat.send_message(
            f"🚫 {user.mention_html()} بن شد.", parse_mode=ParseMode.HTML
        )


@group_admin
@premium_only
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
@premium_only
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
@premium_only
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


@group_admin
async def cmd_del(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg.reply_to_message:
        await msg.reply_text("روی پیامی که می‌خواهید حذف شود ریپلای کنید.")
        return
    await msg.reply_to_message.delete()
    await msg.delete()


@group_admin
async def cmd_pin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg.reply_to_message:
        await msg.reply_text("روی پیامی که می‌خواهید پین شود ریپلای کنید.")
        return
    await msg.reply_to_message.pin(disable_notification=True)


@group_admin
async def cmd_unpin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if msg.reply_to_message:
        await msg.reply_to_message.unpin()
    else:
        await update.effective_chat.unpin_all_messages()
        await msg.reply_text("✅ همه‌ی پین‌ها برداشته شد.")


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
        prem = await is_premium(ctx, chat.id)

        if prem and s["lock_forward"] and msg.forward_origin:
            await msg.delete()
            return

        # قفل لینک رایگان است
        if s["lock_links"] and has_link(msg):
            await msg.delete()
            text = await apply_warn(ctx, chat, user, "ارسال لینک")
            await chat.send_message(text, parse_mode=ParseMode.HTML)
            return

        if prem and s["word_filter"] and s["bad_words"]:
            low = (msg.text or msg.caption or "").lower()
            if any(w in low for w in s["bad_words"]):
                await msg.delete()
                text = await apply_warn(ctx, chat, user, "ارسال کلمه ممنوعه")
                await chat.send_message(text, parse_mode=ParseMode.HTML)
                return

        if prem and s["anti_flood"]:
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


async def on_added(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """وقتی ربات به یک گروه اضافه می‌شود، دوره‌ی آزمایشی شروع می‌شود."""
    cm = update.my_chat_member
    chat = cm.chat
    if chat.type not in GROUP_TYPES:
        return
    old, new = cm.old_chat_member.status, cm.new_chat_member.status
    joined = old in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED) and new in (
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.ADMINISTRATOR,
    )
    if not joined:
        return
    await ensure_trial(ctx, chat.id)
    try:
        await chat.send_message(
            f"✅ مدیر فعال شد! {TRIAL_DAYS} روز همه‌ی قابلیت‌ها رایگان است.\n"
            "ربات را ادمین کنید (حذف پیام و محدود کردن اعضا). راهنما: /help ، وضعیت: /status"
        )
    except TelegramError as e:
        log.warning("welcome message failed in %s: %s", chat.id, e)


async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE):
    log.error("unhandled error", exc_info=ctx.error)


# ───────────────────────── اجرا ─────────────────────────
def main():
    app = (
        Application.builder()
        .token(os.environ["BOT_TOKEN"])
        .post_init(init_db)
        .post_shutdown(close_db)
        .build()
    )
    commands = [
        ("start", cmd_start), ("help", cmd_help), ("cancel", cmd_cancel),
        ("rules", cmd_rules), ("support", cmd_support), ("contact", cmd_contact),
        ("status", cmd_status), ("buy", cmd_buy), ("activate", cmd_activate),
        ("stats", cmd_stats), ("setrules", cmd_setrules), ("settings", cmd_settings),
        ("setwelcome", cmd_setwelcome), ("lockschedule", cmd_lockschedule),
        ("lock", cmd_lock), ("unlock", cmd_unlock), ("warn", cmd_warn),
        ("unwarn", cmd_unwarn), ("ban", cmd_ban), ("unban", cmd_unban),
        ("mute", cmd_mute), ("unmute", cmd_unmute), ("del", cmd_del),
        ("pin", cmd_pin), ("unpin", cmd_unpin), ("addword", cmd_addword),
        ("delword", cmd_delword), ("words", cmd_words),
    ]
    for name, fn in commands:
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(CallbackQueryHandler(on_menu, pattern=r"^m:"))
    app.add_handler(CallbackQueryHandler(on_callback, pattern=r"^[tw]:"))
    app.add_handler(CallbackQueryHandler(on_payment_cb, pattern=r"^(ap|rj):"))
    app.add_handler(ChatMemberHandler(on_added, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, on_private)
    )
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
    app.add_error_handler(on_error)
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
