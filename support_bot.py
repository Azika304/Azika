"""
Azika VPN / Azika Shop - бот поддержки (бренд задаётся переменной BRAND).
Пользователь пишет боту -> сообщение приходит админу.
Админ отвечает через "Ответить" (reply) на это сообщение -> ответ уходит пользователю.
Переменные окружения: SUPPORT_BOT_TOKEN, ADMIN_ID
CONFIRM_FINAL=1 — режим «Это окончательный вопрос?»: бот здоровается, копит сообщения клиента и отправляет их админу
одним обращением после кнопки «Да, отправить» (или автоматически через 5 минут тишины). Для VPN по умолчанию выключен.
"""
import asyncio
import logging
import os
import sqlite3
import time
from contextlib import closing
from html import escape

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

logging.basicConfig(level=logging.INFO)

TOKEN = os.getenv("SUPPORT_BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "6095977712"))
DB_PATH = os.getenv("SUPPORT_DB", "support.db")
BRAND = os.getenv("BRAND", "Azika VPN")   # для магазина: BRAND="Azika Shop"
CONFIRM = os.getenv("CONFIRM_FINAL", "0") == "1"
AUTO_SEND_SEC = int(os.getenv("AUTO_SEND_SEC", "300"))   # через сколько секунд тишины отправить копилку админу
GREET_AFTER_SEC = 6 * 3600                                # здороваемся, если клиент не писал дольше 6 часов

bot = Bot(token=TOKEN or "0:0", default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()


def db():
    return closing(sqlite3.connect(DB_PATH))


def init_db():
    with db() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS msgmap (admin_msg_id INTEGER PRIMARY KEY, user_id INTEGER)")
        conn.execute("CREATE TABLE IF NOT EXISTS banned (user_id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE IF NOT EXISTS pending (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, chat_id INTEGER, "
                     "msg_id INTEGER, name TEXT, uname TEXT, ts INTEGER)")
        conn.execute("CREATE TABLE IF NOT EXISTS seen (user_id INTEGER PRIMARY KEY, ts INTEGER)")
        conn.execute("CREATE TABLE IF NOT EXISTS src (user_id INTEGER PRIMARY KEY, src TEXT, ts INTEGER)")
        conn.commit()


def remember(admin_msg_id: int, user_id: int):
    with db() as conn:
        conn.execute("INSERT OR REPLACE INTO msgmap (admin_msg_id, user_id) VALUES (?, ?)", (admin_msg_id, user_id))
        conn.commit()


def user_for(admin_msg_id: int) -> int | None:
    with db() as conn:
        row = conn.execute("SELECT user_id FROM msgmap WHERE admin_msg_id = ?", (admin_msg_id,)).fetchone()
    return row[0] if row else None


def is_banned(user_id: int) -> bool:
    with db() as conn:
        return conn.execute("SELECT 1 FROM banned WHERE user_id = ?", (user_id,)).fetchone() is not None


SRC_LABEL = {"shop": "🛒 бот покупки (@azikashop_bot)", "seller": "🧑‍💼 бот продавцов (@azika_sellers_bot)",
             "vpn": "🔐 бот Azika VPN", "app": "📱 мини-приложение магазина"}


def src_of(user_id: int) -> str:
    with db() as conn:
        r = conn.execute("SELECT src FROM src WHERE user_id=?", (user_id,)).fetchone()
    return SRC_LABEL.get(r[0], "") if r else ""


def src_line(user_id: int) -> str:
    s = src_of(user_id)
    return f"\n📍 Откуда: <b>{s}</b>" if s else ""


@dp.message(CommandStart())
async def start(message: Message, command: CommandObject = None):
    arg = (command.args or "").strip().lower() if command else ""
    if message.from_user.id != ADMIN_ID:
        with db() as conn:   # /start уже поздоровался — повторное приветствие при первом сообщении не нужно
            conn.execute("INSERT OR REPLACE INTO seen(user_id,ts) VALUES(?,?)", (message.from_user.id, int(time.time())))
            if arg in SRC_LABEL:
                conn.execute("INSERT OR REPLACE INTO src(user_id,src,ts) VALUES(?,?,?)", (message.from_user.id, arg, int(time.time())))
            conn.commit()
    if message.from_user.id == ADMIN_ID:
        await message.answer(
            "👋 Ты админ поддержки.\n\nСообщения пользователей приходят сюда. "
            "Чтобы ответить — нажми «Ответить» (reply) на сообщение пользователя.\n\n"
            "/ban ID — заблокировать, /unban ID — разблокировать."
        )
        return
    await message.answer(
        f"👋 Привет! Это поддержка <b>{escape(BRAND)}</b>.\n\n"
        "✍️ Пишите свой вопрос одним сообщением (можно приложить скриншот) — мы ответим здесь как можно скорее.\n\n"
        + ("Чтобы быстрее помочь, укажите номер заказа и что именно пошло не так."
           if "shop" in BRAND.lower() else
           "Чтобы быстрее помочь, укажите: какое у вас устройство и что именно не работает.")
    )


@dp.message(Command("ban"))
async def ban(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        uid = int((message.text or "").split()[1])
    except Exception:
        return await message.answer("Формат: /ban ID")
    with db() as conn:
        conn.execute("INSERT OR IGNORE INTO banned (user_id) VALUES (?)", (uid,))
        conn.commit()
    await message.answer(f"🚫 {uid} заблокирован.")


@dp.message(Command("unban"))
async def unban(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        uid = int((message.text or "").split()[1])
    except Exception:
        return await message.answer("Формат: /unban ID")
    with db() as conn:
        conn.execute("DELETE FROM banned WHERE user_id = ?", (uid,))
        conn.commit()
    await message.answer(f"✅ {uid} разблокирован.")


@dp.message(F.chat.id == ADMIN_ID)
async def admin_reply(message: Message):
    """Админ отвечает reply-ем на сообщение пользователя - ответ уходит пользователю."""
    if not message.reply_to_message:
        await message.answer("Чтобы ответить пользователю, нажми «Ответить» (reply) на его сообщение.")
        return
    uid = user_for(message.reply_to_message.message_id)
    if not uid:
        await message.answer("Не нашёл, кому это адресовано. Отвечай reply-ем на сообщение пользователя.")
        return
    try:
        await bot.copy_message(uid, message.chat.id, message.message_id)
        await message.answer("✅ Отправлено")
    except Exception as e:
        await message.answer(f"⚠️ Не удалось отправить: {escape(str(e))}")


ASK_KB = InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="✅ Да, отправить", callback_data="sup:yes"),
    InlineKeyboardButton(text="✏️ Дописать", callback_data="sup:more")]])


async def deliver(user_id: int) -> int:
    """Отправляет админу всё, что клиент накопил, одним обращением. Возвращает число сообщений."""
    with db() as conn:
        rows = conn.execute("SELECT chat_id,msg_id,name,uname FROM pending WHERE user_id=? ORDER BY id", (user_id,)).fetchall()
        conn.execute("DELETE FROM pending WHERE user_id=?", (user_id,))
        conn.commit()
    if not rows:
        return 0
    name, uname = rows[-1][2], rows[-1][3]
    header = await bot.send_message(
        ADMIN_ID, f"💬 <b>{escape(name or '')}</b> ({escape(uname)})\nID: <code>{user_id}</code> · сообщений: {len(rows)}{src_line(user_id)}\n\n"
                  "↩️ Ответь reply-ем на это сообщение.")
    remember(header.message_id, user_id)
    for chat_id, msg_id, _, _ in rows:
        try:
            copied = await bot.copy_message(ADMIN_ID, chat_id, msg_id, reply_to_message_id=header.message_id)
            remember(copied.message_id, user_id)
        except Exception as e:
            logging.warning("copy to admin: %s", e)
    return len(rows)


async def auto_send_loop():
    """Если клиент не нажал кнопку — через AUTO_SEND_SEC тишины отправляем всё админу сами."""
    while True:
        await asyncio.sleep(30)
        try:
            with db() as conn:
                ids = [r[0] for r in conn.execute("SELECT user_id FROM pending GROUP BY user_id HAVING MAX(ts)<?",
                                                  (int(time.time()) - AUTO_SEND_SEC,)).fetchall()]
            for uid in ids:
                if await deliver(uid):
                    try:
                        await bot.send_message(uid, "✅ Отправили ваше обращение в поддержку. Ответим здесь как можно скорее.")
                    except Exception:
                        pass
        except Exception as e:
            logging.warning("auto_send: %s", e)


@dp.callback_query(F.data.startswith("sup:"))
async def sup_cb(callback: CallbackQuery):
    if callback.data == "sup:yes":
        n = await deliver(callback.from_user.id)
        text = "✅ Отправили в поддержку. Ответим здесь как можно скорее." if n else "Обращение уже отправлено ✅"
    else:
        text = "✏️ Хорошо, пишите дальше — я добавлю всё в одно обращение. Когда закончите, нажмите «Да, отправить» ниже."
    try:
        await callback.message.edit_text(text, reply_markup=ASK_KB if callback.data == "sup:more" else None)
    except Exception:
        pass
    await callback.answer()


@dp.message()
async def from_user(message: Message):
    user = message.from_user
    if is_banned(user.id):
        return
    if CONFIRM:
        now = int(time.time())
        uname = f"@{user.username}" if user.username else "без username"
        with db() as conn:
            seen = conn.execute("SELECT ts FROM seen WHERE user_id=?", (user.id,)).fetchone()
            first = conn.execute("SELECT 1 FROM pending WHERE user_id=?", (user.id,)).fetchone() is None
            conn.execute("INSERT INTO pending(user_id,chat_id,msg_id,name,uname,ts) VALUES(?,?,?,?,?,?)",
                         (user.id, message.chat.id, message.message_id, user.full_name, uname, now))
            conn.execute("INSERT OR REPLACE INTO seen(user_id,ts) VALUES(?,?)", (user.id, now))
            conn.commit()
        if first:   # спрашиваем один раз на пачку сообщений
            hello = (f"👋 Привет, {escape(user.first_name or '')}! Это поддержка <b>{escape(BRAND)}</b>.\n\n"
                     if not seen or now - seen[0] > GREET_AFTER_SEC else "")
            await message.answer(hello + "Принял ваше сообщение. Это <b>окончательный вопрос</b>? Если хотите что-то добавить — "
                                         "просто допишите, я соберу всё в одно обращение.", reply_markup=ASK_KB)
        return
    uname = f"@{user.username}" if user.username else "без username"
    header = await bot.send_message(
        ADMIN_ID,
        f"💬 <b>{escape(user.full_name)}</b> ({escape(uname)})\nID: <code>{user.id}</code>{src_line(user.id)}\n\n"
        "↩️ Ответь reply-ем на это сообщение.",
    )
    remember(header.message_id, user.id)
    copied = await bot.copy_message(ADMIN_ID, message.chat.id, message.message_id, reply_to_message_id=header.message_id)
    remember(copied.message_id, user.id)
    await message.answer("✅ Получили! Ответим здесь как можно скорее.")


async def main():
    if not TOKEN:
        raise SystemExit("Задай SUPPORT_BOT_TOKEN")
    init_db()
    if CONFIRM:
        asyncio.create_task(auto_send_loop())
    await bot.delete_webhook(drop_pending_updates=True)
    print("Бот поддержки запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
