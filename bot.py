import aiohttp
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import sqlite3
import time
import uuid
from contextlib import closing
from datetime import datetime, timedelta
from html import escape
from urllib.parse import parse_qsl, urlencode

import xui
from aiogram import Bot, Dispatcher, F
from aiohttp import web
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    MenuButtonDefault,
    MenuButtonWebApp,
    Message,
    PreCheckoutQuery,
    ReplyKeyboardRemove,
    WebAppInfo,
)
from aiogram.utils.markdown import hbold, hcode

# ==========================================
# ⚙️ НАСТРОЙКИ (задавай через переменные окружения!)
# ==========================================
# Windows (cmd):  set BOT_TOKEN=123:ABC
# Linux/Mac:      export BOT_TOKEN="123:ABC"
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
# Публичная HTTPS-ссылка на index.html (мини-приложение)
WEBAPP_URL = os.getenv("WEBAPP_URL", "")
# Токен платёжного провайдера из @BotFather -> Payments (ЮKassa и т.п.). Без него работают только Stars.
PROVIDER_TOKEN = os.getenv("PROVIDER_TOKEN", "")
# Числовой ID канала вида -1001234567890 (бот должен быть админом канала). 0 = проверка выключена.
_ch = os.getenv("CHANNEL_ID", "@azikavpn").strip()          # @username публичного канала или числовой ID (-100...)
CHANNEL_ID = int(_ch) if _ch.lstrip("-").isdigit() else _ch
SUPPORT_BOT = os.getenv("SUPPORT_BOT_USERNAME", "").lstrip("@")   # бот поддержки (support_bot.py)
CHANNEL_LINK = os.getenv("CHANNEL_LINK", "https://t.me/azikavpn")
# Твой числовой Telegram ID (виден в профиле кабинета). Только ему доступны /admin, /give и тест-тариф.
ADMIN_ID = int(os.getenv("ADMIN_ID", "6095977712"))
# Реферальная программа: сколько дней получает пригласивший, когда друг впервые оплатил подписку
REF_BONUS_DAYS = int(os.getenv("REF_BONUS_DAYS", "3"))
DISCOUNT_STEP = int(os.getenv("DISCOUNT_STEP", "5"))   # % скидки за каждого оплатившего «друга друга»
DISCOUNT_MAX = int(os.getenv("DISCOUNT_MAX", "30"))    # максимум скидки, %
# TEST_PUBLIC=1 — тестовый тариф (1 минута за 1 ₽) увидят и смогут купить все. По умолчанию только админ.
TEST_PUBLIC = os.getenv("TEST_PUBLIC", "0") == "1"
# Адрес твоего VPN-сервера (нужен для генерации ключа)
SERVER_HOST = os.getenv("SERVER_HOST", "127.0.0.1")
DB_PATH = "azika_vpn.db"

# Текст синей кнопки слева от поля ввода
BUTTON_TEXT = "Открыть VPN"


BOT_USERNAME = ""  # заполняется автоматически при запуске

# ключ: (название, цена в рублях, цена в Telegram Stars, срок в минутах, текст срока)
TARIFFS = {
    "30": ("1 месяц", 150, 100, 30 * 1440, "30 дней"),
    "90": ("3 месяца", 400, 270, 90 * 1440, "90 дней"),
    "365": ("12 месяцев", 1200, 800, 365 * 1440, "365 дней"),
    # VIP-сервер: отдельный сервер, максимум 10 человек
    "v30": ("VIP · 1 месяц", 300, 200, 30 * 1440, "30 дней"),
    "v90": ("VIP · 3 месяца", 800, 540, 90 * 1440, "90 дней"),
    "v365": ("VIP · 12 месяцев", 2400, 1600, 365 * 1440, "365 дней"),
    # Тестовый тариф: виден и доступен только админу (1 минута за 1 ₽ / 1 ⭐)
    "test": ("Тест — 1 минута", 1, 1, 1, "1 минуту"),
}
ADMIN_ONLY = {"test"}
# Тарифы с несколькими людьми: цена = база + доплата за каждого следующего человека (затем множитель срока).
#  • обычный сервер: на одном ключе от 1 до 10 человек
#  • VIP-сервер: на одном ключе от 1 до 5 человек
#  • семейный: от 2 до 10 человек, у каждого СВОЙ ключ (доплата меньше — выгоднее)
STD_MAX_PEOPLE, VIP_MAX_PEOPLE = 10, 5
FAMILY_MIN, FAMILY_MAX = 2, 10
STD_EXTRA = (int(os.getenv("STD_EXTRA_RUB", "100")), int(os.getenv("STD_EXTRA_STARS", "65")))
VIP_EXTRA = (int(os.getenv("VIP_EXTRA_RUB", "200")), int(os.getenv("VIP_EXTRA_STARS", "130")))
FAMILY_EXTRA = (int(os.getenv("FAMILY_EXTRA_RUB", "85")), int(os.getenv("FAMILY_EXTRA_STARS", "55")))
TERMS = [("30", "1 месяц", 30, 1.0, 1.0), ("90", "3 месяца", 90, 8 / 3, 2.7), ("365", "12 месяцев", 365, 8.0, 8.0)]
# (код ключа, префикс названия, база ₽, база ⭐, доплата, мин., макс.)
_CLASSES = {
    "std": ("k", "", TARIFFS["30"][1], TARIFFS["30"][2], STD_EXTRA, 2, STD_MAX_PEOPLE),
    "vip": ("vk", "VIP · ", TARIFFS["v30"][1], TARIFFS["v30"][2], VIP_EXTRA, 2, VIP_MAX_PEOPLE),
    "fam": ("f", "Семейный · ", TARIFFS["30"][1], TARIFFS["30"][2], FAMILY_EXTRA, FAMILY_MIN, FAMILY_MAX),
}
for _cls, (_code, _pre, _br, _bs, (_er, _es), _lo, _hi) in _CLASSES.items():
    for _n in range(_lo, _hi + 1):
        for _tk, _tn, _d, _mr, _ms in TERMS:
            _who = f"{_n} чел., у каждого свой ключ" if _cls == "fam" else f"{_n} чел. на ключе"
            TARIFFS[f"{_code}{_n}_{_tk}"] = (
                f"{_pre}{_who} · {_tn}",
                round((_br + _er * (_n - 1)) * _mr),
                round((_bs + _es * (_n - 1)) * _ms),
                _d * 1440,
                f"{_d} дней",
            )


def parse_tariff(key: str) -> tuple[str, int]:
    """(класс 'std'|'vip'|'fam', сколько человек). Для обычных тарифов — 1 человек."""
    import re as _re
    m = _re.fullmatch(r"(k|vk|f)(\d+)_\d+", key)
    if m:
        return {"k": "std", "vk": "vip", "f": "fam"}[m.group(1)], int(m.group(2))
    return ("vip" if key.startswith("v") else "std"), 1


def family_n(key: str) -> int:
    """Сколько ключей выдаётся (у семейного — по одному на каждого человека)."""
    cls, n = parse_tariff(key)
    return n if cls == "fam" else 1


VIP_SERVER_HOST = os.getenv("VIP_SERVER_HOST", "")   # адрес отдельного VIP-сервера (если пусто — как у обычного)
STD_SLOTS, VIP_SLOTS = 50, 10

TIER_INFO = (
    f"🌐 {{b}}Обычный сервер{{/b}} — до ~{STD_SLOTS} человек на сервере. Один ключ можно разделить на 1–{STD_MAX_PEOPLE} человек "
    "(каждый следующий добавляет к цене). Самая выгодная цена.\n\n"
    f"👑 {{b}}VIP-сервер{{/b}} — отдельный, максимум {VIP_SLOTS} человек на сервере и до {VIP_MAX_PEOPLE} на одном ключе. "
    "Почти без нагрузки: выше скорость, стабильный пинг, подходит для игр и 4K.\n\n"
    f"👨‍👩‍👧 {{b}}Семейный{{/b}} — от {FAMILY_MIN} до {FAMILY_MAX} человек, у каждого свой ключ, и выходит дешевле, чем докупать людей на один ключ. "
    "Выбор числа людей — в кабинете «Открыть VPN»."
)


def tariff_allowed(key: str, user_id: int) -> bool:
    return key in TARIFFS and (key not in ADMIN_ONLY or user_id == ADMIN_ID or TEST_PUBLIC)

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())


# ==========================================
# 🗄️ БАЗА ДАННЫХ
# ==========================================
def db():
    return closing(sqlite3.connect(DB_PATH))


def init_db():
    with db() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                name TEXT,
                balance REAL DEFAULT 0.0,
                expire_date TEXT DEFAULT NULL
            )"""
        )
        try:
            conn.execute("ALTER TABLE users ADD COLUMN discount INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # колонка уже есть
        try:
            conn.execute("ALTER TABLE users ADD COLUMN tier TEXT DEFAULT 'std'")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE users ADD COLUMN slots INTEGER DEFAULT 1")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE users ADD COLUMN people INTEGER DEFAULT 1")
        except sqlite3.OperationalError:
            pass
        conn.execute("CREATE TABLE IF NOT EXISTS promos (code TEXT PRIMARY KEY, pct INTEGER, uses_left INTEGER)")
        conn.execute("CREATE TABLE IF NOT EXISTS promo_used (user_id INTEGER, code TEXT, PRIMARY KEY (user_id, code))")
        conn.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)")   # настройки из админ-бота (пауза продаж)
        try:
            conn.execute("ALTER TABLE promos ADD COLUMN paused INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        # Первый промокод: 99% скидки, 5 использований (добавлять новые: /addpromo КОД ПРОЦЕНТ ЧИСЛО_ИСПОЛЬЗОВАНИЙ)
        conn.execute("INSERT OR IGNORE INTO promos (code, pct, uses_left) VALUES ('AZIKA99', 99, 5)")
        # Таблица платежей защищает от двойного начисления
        conn.execute(
            """CREATE TABLE IF NOT EXISTS payments (
                charge_id TEXT PRIMARY KEY,
                user_id INTEGER,
                days INTEGER,
                amount INTEGER,
                currency TEXT,
                created TEXT
            )"""
        )
        # Рефералы: кто кого пригласил и получил ли пригласивший бонус
        conn.execute(
            """CREATE TABLE IF NOT EXISTS referrals (
                invited_id INTEGER PRIMARY KEY,
                referrer_id INTEGER,
                rewarded INTEGER DEFAULT 0,
                created TEXT
            )"""
        )
        # Какие напоминания о сроке уже отправлены
        conn.execute(
            """CREATE TABLE IF NOT EXISTS notified (
                user_id INTEGER,
                expire_ts INTEGER,
                kind TEXT,
                PRIMARY KEY (user_id, expire_ts, kind)
            )"""
        )
        conn.commit()


def db_add_user(user_id: int, name: str):
    with db() as conn:
        conn.execute("INSERT OR IGNORE INTO users (user_id, name) VALUES (?, ?)", (user_id, name))
        conn.commit()


def db_user_exists(user_id: int) -> bool:
    with db() as conn:
        return conn.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,)).fetchone() is not None


def db_add_referral(invited_id: int, referrer_id: int) -> bool:
    """Запоминает, кто пригласил. Работает один раз на человека и только если пригласивший уже в базе."""
    if invited_id == referrer_id or not db_user_exists(referrer_id):
        return False
    with db() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO referrals (invited_id, referrer_id, created) VALUES (?, ?, ?)",
            (invited_id, referrer_id, datetime.now().isoformat()),
        )
        conn.commit()
        return cur.rowcount > 0


def db_pending_referrer(invited_id: int) -> int | None:
    """Кто пригласил этого человека, если бонус за него ещё не выдан."""
    with db() as conn:
        row = conn.execute(
            "SELECT referrer_id FROM referrals WHERE invited_id = ? AND rewarded = 0", (invited_id,)
        ).fetchone()
    return row[0] if row else None


def db_mark_rewarded(invited_id: int):
    with db() as conn:
        conn.execute("UPDATE referrals SET rewarded = 1 WHERE invited_id = ?", (invited_id,))
        conn.commit()


def db_ref_stats(referrer_id: int) -> tuple[int, int]:
    """(сколько пригласил, сколько из них оплатили)."""
    with db() as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM referrals WHERE referrer_id = ?", (referrer_id,)
        ).fetchone()[0]
        paid = conn.execute(
            "SELECT COUNT(*) FROM referrals WHERE referrer_id = ? AND rewarded = 1", (referrer_id,)
        ).fetchone()[0]
    return total, paid


def db_referrer_of(user_id: int) -> int | None:
    with db() as conn:
        row = conn.execute("SELECT referrer_id FROM referrals WHERE invited_id = ?", (user_id,)).fetchone()
    return row[0] if row else None


def db_ref2_paid(user_id: int) -> int:
    """Сколько «друзей друзей» (приглашённых твоими друзьями) уже оплатили."""
    with db() as conn:
        return conn.execute(
            """SELECT COUNT(*) FROM referrals r2 JOIN referrals r1 ON r2.referrer_id = r1.invited_id
               WHERE r1.referrer_id = ? AND r2.rewarded = 1""",
            (user_id,),
        ).fetchone()[0]


def db_get_tier(user_id: int) -> str:
    with db() as conn:
        row = conn.execute("SELECT tier FROM users WHERE user_id = ?", (user_id,)).fetchone()
    return "vip" if row and row[0] == "vip" else "std"


def db_set_tier(user_id: int, tier: str):
    with db() as conn:
        conn.execute("UPDATE users SET tier = ? WHERE user_id = ?", (tier, user_id))
        conn.commit()


def sales_paused() -> str:
    """Текст для покупателей, если продажи приостановлены из админ-бота; пустая строка — продажи идут."""
    try:
        with db() as conn:
            rows = dict(conn.execute("SELECT k, v FROM kv WHERE k IN ('paused','pause_msg')").fetchall())
    except sqlite3.Error:
        return ""
    if rows.get("paused") == "1":
        return rows.get("pause_msg") or "⏸ Продажи временно приостановлены. Скоро вернёмся!"
    return ""


def promo_check(user_id: int, code: str) -> tuple[bool, int, str]:
    """(подходит ли промокод этому пользователю, процент скидки, сообщение)."""
    code = (code or "").strip().upper()
    if not code:
        return False, 0, "Введи промокод"
    with db() as conn:
        row = conn.execute("SELECT pct, uses_left, COALESCE(paused, 0) FROM promos WHERE code = ?", (code,)).fetchone()
        used = conn.execute("SELECT 1 FROM promo_used WHERE user_id = ? AND code = ?", (user_id, code)).fetchone()
    if not row:
        return False, 0, "Такого промокода нет"
    if used:
        return False, 0, "Ты уже использовал этот промокод"
    if row[2]:
        return False, 0, "Промокод временно отключён"
    if row[1] <= 0:
        return False, 0, "Промокод закончился"
    return True, int(row[0]), f"Промокод применён: −{row[0]}%"


def promo_consume(user_id: int, code: str):
    code = (code or "").strip().upper()
    with db() as conn:
        conn.execute("INSERT OR IGNORE INTO promo_used (user_id, code) VALUES (?, ?)", (user_id, code))
        conn.execute("UPDATE promos SET uses_left = MAX(0, uses_left - 1) WHERE code = ?", (code,))
        conn.commit()


def final_price(value: int, key: str, ref_pct: int, promo_pct: int) -> int:
    """Цена с учётом скидки за друзей и промокода (тестовый тариф без скидок)."""
    if key in ADMIN_ONLY:
        return value
    return max(1, round(value * (100 - ref_pct) / 100 * (100 - promo_pct) / 100))


def db_get_people(user_id: int) -> int:
    with db() as conn:
        row = conn.execute("SELECT people FROM users WHERE user_id = ?", (user_id,)).fetchone()
    return max(1, int(row[0] or 1)) if row else 1


def db_set_people(user_id: int, n: int):
    with db() as conn:
        conn.execute("UPDATE users SET people = ? WHERE user_id = ?", (n, user_id))
        conn.commit()


def db_get_slots(user_id: int) -> int:
    with db() as conn:
        row = conn.execute("SELECT slots FROM users WHERE user_id = ?", (user_id,)).fetchone()
    return max(1, int(row[0] or 1)) if row else 1


def db_set_slots(user_id: int, n: int):
    with db() as conn:
        conn.execute("UPDATE users SET slots = ? WHERE user_id = ?", (n, user_id))
        conn.commit()


def db_get_discount(user_id: int) -> int:
    with db() as conn:
        row = conn.execute("SELECT discount FROM users WHERE user_id = ?", (user_id,)).fetchone()
    return int(row[0] or 0) if row else 0


def db_add_discount(user_id: int) -> int:
    """+DISCOUNT_STEP % (не больше DISCOUNT_MAX). Возвращает новую скидку."""
    new = min(DISCOUNT_MAX, db_get_discount(user_id) + DISCOUNT_STEP)
    with db() as conn:
        conn.execute("UPDATE users SET discount = ? WHERE user_id = ?", (new, user_id))
        conn.commit()
    return new


def db_use_discount(user_id: int, used: int):
    with db() as conn:
        conn.execute("UPDATE users SET discount = MAX(0, COALESCE(discount,0) - ?) WHERE user_id = ?", (used, user_id))
        conn.commit()


def price_with_discount(value: int, key: str, pct: int) -> int:
    return value if key in ADMIN_ONLY or pct <= 0 else max(1, round(value * (100 - pct) / 100))


def db_get_user(user_id: int) -> dict:
    with db() as conn:
        row = conn.execute(
            "SELECT name, balance, expire_date FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
    if row:
        return {"name": row[0] or "Пользователь", "balance": row[1], "expire_date": row[2]}
    return {"name": "Пользователь", "balance": 0.0, "expire_date": None}


def parse_exp(expire_date: str | None) -> datetime | None:
    """Срок подписки. Старый формат (только дата) действует до конца этого дня."""
    if not expire_date:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(expire_date, fmt)
            return dt.replace(hour=23, minute=59, second=59) if fmt == "%Y-%m-%d" else dt
        except ValueError:
            continue
    return None


def is_active(expire_date: str | None) -> bool:
    dt = parse_exp(expire_date)
    return bool(dt and dt > datetime.now())


def fmt_exp(expire_date: str | None) -> str:
    dt = parse_exp(expire_date)
    return dt.strftime("%d.%m.%Y %H:%M") if dt else "не активна"


def db_add_subscription(user_id: int, minutes: int) -> str:
    """Продлевает подписку на N минут. Возвращает новый срок строкой."""
    u = db_get_user(user_id)
    now = datetime.now()
    cur = parse_exp(u["expire_date"])
    base = cur if cur and cur > now else now
    new_expire = base + timedelta(minutes=minutes)
    new_str = new_expire.strftime("%Y-%m-%d %H:%M:%S")
    ts = int(new_expire.timestamp())
    with db() as conn:
        conn.execute("UPDATE users SET expire_date = ? WHERE user_id = ?", (new_str, user_id))
        # Для коротких подписок лишние напоминания сразу помечаем отправленными
        left = new_expire - now
        if left <= timedelta(days=3):
            conn.execute("INSERT OR IGNORE INTO notified VALUES (?, ?, '3d')", (user_id, ts))
        if left <= timedelta(days=1):
            conn.execute("INSERT OR IGNORE INTO notified VALUES (?, ?, '1d')", (user_id, ts))
        conn.commit()
    return new_str


def db_mark_notified(user_id: int, ts: int, kind: str) -> bool:
    """True, если такое напоминание ещё не отправлялось (и теперь помечено)."""
    with db() as conn:
        cur = conn.execute("INSERT OR IGNORE INTO notified VALUES (?, ?, ?)", (user_id, ts, kind))
        conn.commit()
        return cur.rowcount > 0


def db_register_payment(charge_id: str, user_id: int, key: str, amount: int, currency: str) -> bool:
    """False, если такой платёж уже был обработан."""
    with db() as conn:
        try:
            conn.execute(
                "INSERT INTO payments VALUES (?, ?, ?, ?, ?, ?)",
                (charge_id, user_id, key, amount, currency, datetime.now().isoformat()),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False


def db_stats() -> tuple[int, int]:
    with db() as conn:
        rows = conn.execute("SELECT expire_date FROM users").fetchall()
    return len(rows), sum(1 for (e,) in rows if is_active(e))


def db_all_ids() -> list[int]:
    with db() as conn:
        return [r[0] for r in conn.execute("SELECT user_id FROM users").fetchall()]


class AdminStates(StatesGroup):
    waiting_msg = State()


class SupportStates(StatesGroup):
    waiting_question = State()


# ==========================================
# 🔑 ВЫДАЧА VPN-КЛЮЧА
# ==========================================
def _stub_key(user_id: int, i: int = 1) -> str:
    """Заглушка — используется, только если панель 3x-ui не подключена (нет XUI_URL/XUI_USER/XUI_PASS)."""
    host = (VIP_SERVER_HOST or SERVER_HOST) if db_get_tier(user_id) == "vip" else SERVER_HOST
    client_uuid = uuid.uuid5(uuid.NAMESPACE_DNS, f"azika-{user_id}" + (f"-{i}" if i > 1 else ""))
    return f"vless://{client_uuid}@{host}:443?security=reality&type=tcp#AzikaVpn" + (f"-{i}" if i > 1 else "")


def xui_init():
    with db() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS xui_keys2 (user_id INTEGER, srv TEXT, idx INTEGER, uuid TEXT, "
            "expiry_ms INTEGER, limit_ip INTEGER, link TEXT, PRIMARY KEY (user_id, srv, idx))"
        )
        conn.execute("CREATE TABLE IF NOT EXISTS key_resets (user_id INTEGER PRIMARY KEY, ts INTEGER)")
        conn.execute("CREATE TABLE IF NOT EXISTS sub_tokens (user_id INTEGER PRIMARY KEY, token TEXT UNIQUE)")
        conn.commit()


def panels_for(user_id: int) -> list:
    """Серверы, доступные пользователю (по типу тарифа: all — всем, std — только обычным, vip — только VIP)."""
    vip = db_get_tier(user_id) == "vip"
    return [p for p in xui.PANELS if p.tier == "all" or (p.tier == "vip" and vip) or (p.tier == "std" and not vip)]


def xui_cached(user_id: int) -> list[tuple[str, int, str]]:
    """[(srv, idx, link)] в порядке серверов из servers.json."""
    with db() as conn:
        rows = conn.execute("SELECT srv, idx, link FROM xui_keys2 WHERE user_id = ?", (user_id,)).fetchall()
    order = {p.sid: n for n, p in enumerate(xui.PANELS)}
    allowed = {p.sid for p in panels_for(user_id)}
    rows = [r for r in rows if r[0] in allowed]
    rows.sort(key=lambda r: (order.get(r[0], 99), r[1]))
    return rows


def get_vpn_keys(user_id: int) -> list[str]:
    """Все ссылки пользователя: по одной на каждый сервер (для семейного тарифа — по одной на человека на сервер)."""
    n = db_get_slots(user_id)
    if not xui.ENABLED:
        return [_stub_key(user_id, i) for i in range(1, n + 1)]
    return [l for _s, i, l in xui_cached(user_id) if i <= n]


def get_vpn_key(user_id: int) -> str:
    keys = get_vpn_keys(user_id)
    return keys[0] if keys else ""


_COUNTRY: dict[str, str] = {}   # host -> «🇵🇱 Польша»


def _flag(code: str) -> str:
    return "".join(chr(0x1F1E6 + ord(c) - 65) for c in code.upper()) if len(code) == 2 and code.isalpha() else "🌐"


def _panel_host(p) -> str:
    return p.host or SERVER_HOST


async def init_server_labels():
    """Определяет страну сервера по IP/домену (если название не задано вручную в servers.json / SERVER_LABEL)."""
    hosts = {_panel_host(p) for p in xui.PANELS if not p.label} or ({SERVER_HOST} if not xui.PANELS else set())
    for host in hosts:
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as sess:
                async with sess.get(f"http://ip-api.com/json/{host}?fields=status,country,countryCode&lang=ru") as r:
                    d = await r.json(content_type=None)
            if d.get("status") == "success":
                _COUNTRY[host] = f"{_flag(d.get('countryCode', ''))} {d.get('country', '')}".strip()
        except Exception as e:
            logging.warning("Не удалось определить страну для %s: %s", host, e)


def link_name(user_id: int, p, idx: int = 1) -> str:
    """Название ключа в приложении: «🇩🇪 Германия» (из servers.json или по IP сервера)."""
    base = p.label or _COUNTRY.get(_panel_host(p)) or "Azika VPN"
    return base if db_get_slots(user_id) == 1 else f"{base} {idx}"


def sub_token(user_id: int) -> str:
    with db() as conn:
        row = conn.execute("SELECT token FROM sub_tokens WHERE user_id = ?", (user_id,)).fetchone()
        if row:
            return row[0]
        tok = secrets.token_urlsafe(12)
        conn.execute("INSERT INTO sub_tokens (user_id, token) VALUES (?, ?)", (user_id, tok))
        conn.commit()
        return tok


def sub_url(user_id: int) -> str:
    """Ссылка-подписка: одна ссылка на все серверы пользователя (приложение само обновляет список)."""
    if not (xui.ENABLED and API_URL):
        return ""
    return f"{API_URL}/sub/{sub_token(user_id)}"


def key_block(user_id: int) -> str:
    """Текст про ключ/подписку для сообщений бота."""
    keys = get_vpn_keys(user_id)
    if not keys:
        return "⏳ Ключ создаётся — он появится в кабинете в течение минуты."
    url = sub_url(user_id)
    if url:
        txt = (
            f"🔗 {hbold('Твоя ссылка-подписка:')}\n{hcode(url)}\n\n"
            "📲 Скопируй её и добавь в приложение (Happ, v2rayN, Streisand, Hiddify): «Добавить подписку» "
            "или «Импорт из буфера». Все страны появятся списком, и они обновляются сами — при продлении "
            "или смене ключа ничего менять не нужно."
        )
        txt += (
            f"\n\n⚡ {hbold('Для Happ — с обходом российских сайтов (они идут напрямую, быстрее):')}\n"
            f"{hcode(url + '?fmt=json')}"
        )
        if len(keys) == 1:
            txt += f"\n\nЕсли приложение не принимает подписки, вставь ключ:\n{hcode(keys[0])}"
        return txt
    body = "\n\n".join(f"{i}. {k}" for i, k in enumerate(keys, 1)) if len(keys) > 1 else keys[0]
    return f"🔑 {hbold('Твой ключ доступа:' if len(keys) == 1 else 'Ключи доступа:')}\n{hcode(body)}\n\n📌 Нажми на ключ, чтобы скопировать."


async def xui_sync_user(user_id: int):
    """Создаёт/продлевает ключи пользователя на всех доступных серверах под его срок и лимит устройств."""
    if not xui.ENABLED:
        return
    exp = parse_exp(db_get_user(user_id)["expire_date"])
    if not exp:
        return
    expiry_ms = int(exp.timestamp() * 1000)
    n = db_get_slots(user_id)
    limit_ip = db_get_people(user_id) if n == 1 else 1
    with db() as conn:
        have = {(r[0], r[1]): r[2:] for r in conn.execute(
            "SELECT srv, idx, uuid, expiry_ms, limit_ip FROM xui_keys2 WHERE user_id = ?", (user_id,))}
    shared = {}   # один и тот же UUID на всех серверах
    for (srv, i), cur in have.items():
        shared.setdefault(i, cur[0])
    first_err = None
    for p in panels_for(user_id):
        for i in range(1, n + 1):
            cur = have.get((p.sid, i))
            if cur and cur[1] == expiry_ms and cur[2] == limit_ip:
                continue   # уже актуально
            try:
                uid_, link = await p.ensure_client(
                    f"u{user_id}-{i}", expiry_ms, limit_ip, _panel_host(p), link_name(user_id, p, i),
                    cur[0] if cur else shared.get(i))
            except Exception as e:
                logging.error("3x-ui[%s]: пользователь %s: %s", p.sid, user_id, e)
                first_err = first_err or e
                continue
            shared.setdefault(i, uid_)
            with db() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO xui_keys2 (user_id, srv, idx, uuid, expiry_ms, limit_ip, link) "
                    "VALUES (?,?,?,?,?,?,?)", (user_id, p.sid, i, uid_, expiry_ms, limit_ip, link))
                conn.commit()
    if first_err:
        raise first_err


async def xui_refresh_links():
    """Пересобирает ссылки по актуальным настройкам подключений (если в панели что-то поменяли: SNI, ключи, shortId)."""
    for p in xui.PANELS:
        try:
            ib = await p.find_inbound()
        except Exception as e:
            logging.error("3x-ui[%s]: %s", p.sid, e)
            continue
        with db() as conn:
            rows = conn.execute("SELECT user_id, idx, uuid, link FROM xui_keys2 WHERE srv = ?", (p.sid,)).fetchall()
        for uid_, idx, cuuid, old_link in rows:
            link = xui.build_link(ib, {"id": cuuid, "flow": "xtls-rprx-vision"}, _panel_host(p), link_name(uid_, p, idx))
            if link != old_link:
                with db() as conn:
                    conn.execute("UPDATE xui_keys2 SET link = ? WHERE user_id = ? AND srv = ? AND idx = ?",
                                 (link, uid_, p.sid, idx))
                    conn.commit()


async def xui_sync_loop():
    """Раз в минуту сверяет срок и лимиты всех пользователей с панелями (бонусные дни, админ-выдача и т.п.)."""
    if not xui.ENABLED:
        print("ℹ️ Панель 3x-ui не подключена — выдаются тестовые ключи-заглушки")
        return
    print("🌍 Серверы VPN: " + ", ".join(f"{p.sid} ({_panel_host(p)})" for p in xui.PANELS))
    while True:
        try:
            with db() as conn:
                ids = [r[0] for r in conn.execute("SELECT user_id FROM users WHERE expire_date IS NOT NULL")]
            for uid_ in ids:
                try:
                    await xui_sync_user(uid_)
                except Exception as e:
                    logging.error("3x-ui: пользователь %s: %s", uid_, e)
            await xui_refresh_links()
        except Exception as e:
            logging.error("3x-ui: цикл синхронизации: %s", e)
        await asyncio.sleep(60)


# ==========================================
# 🌐 МИНИ-ПРИЛОЖЕНИЕ И СИНЯЯ КНОПКА «ОТКРЫТЬ VPN»
# ==========================================
# Мини-сервер для оплаты прямо в приложении (динамические цены, промокоды). Нужен публичный https-адрес:
# например, `cloudflared tunnel --url http://localhost:8080` выдаст ссылку вида https://xxxx.trycloudflare.com
API_URL = os.getenv("API_URL", "").rstrip("/")
API_PORT = int(os.getenv("API_PORT", "8080"))
URL_BUDGET = int(os.getenv("URL_BUDGET", "2000"))  # максимальная длина ссылки на кабинет
INVOICE_LINKS: dict[str, str] = {}  # "s30" — Stars за тариф 30, "c30" — карта


async def build_invoice_links(pct: int = 0):
    """Создаёт ссылки на оплату (с учётом скидки pct) — по ним кабинет открывает платёж прямо внутри приложения."""
    suf = f"_{pct}" if pct else ""
    for key, (name, rub, stars, _, label) in TARIFFS.items():
        if "s" + key + suf in INVOICE_LINKS:
            continue
        rub_p, stars_p = price_with_discount(rub, key, pct), price_with_discount(stars, key, pct)
        payload = f"sub:{key}:{pct}"
        try:
            INVOICE_LINKS["s" + key + suf] = await bot.create_invoice_link(
                title=f"Azika VPN — {name}",
                description=f"Подписка на VPN на {label}" + (f" (скидка {pct}%)" if pct else ""),
                payload=payload,
                provider_token="",
                currency="XTR",
                prices=[LabeledPrice(label=f"Подписка {name}", amount=stars_p)],
            )
            if PROVIDER_TOKEN:
                INVOICE_LINKS["c" + key + suf] = await bot.create_invoice_link(
                    title=f"Azika VPN — {name}",
                    description=f"Подписка на VPN на {label}" + (f" (скидка {pct}%)" if pct else ""),
                    payload=payload,
                    provider_token=PROVIDER_TOKEN,
                    currency="RUB",
                    prices=[LabeledPrice(label=f"Подписка {name}", amount=rub_p * 100)],
                )
        except Exception as e:
            logging.warning("Не удалось создать ссылку на оплату для %s: %s", key, e)


def webapp_url(user_id: int) -> str:
    """Ссылка на мини-приложение с актуальными данными пользователя."""
    u = db_get_user(user_id)
    params = {"b": BOT_USERNAME, "v": int(time.time())}  # v — защита от кеша
    if is_active(u["expire_date"]):
        params["exp"] = int(parse_exp(u["expire_date"]).timestamp())
        params["key"] = sub_url(user_id) or get_vpn_key(user_id)
    if user_id == ADMIN_ID or TEST_PUBLIC:
        params["t"] = 1  # показать тестовый тариф (1 минута)
    invited, paid = db_ref_stats(user_id)
    params["ri"] = invited
    params["rp"] = paid
    params["rb"] = REF_BONUS_DAYS
    params["r2p"] = db_ref2_paid(user_id)
    if db_get_tier(user_id) == "vip":
        params["vip"] = 1
    pct = db_get_discount(user_id)
    params["dc"], params["ds"], params["dm"] = pct, DISCOUNT_STEP, DISCOUNT_MAX
    suf = f"_{pct}" if pct else ""
    params["pe"], params["pes"] = STD_EXTRA
    params["ve"], params["ves"] = VIP_EXTRA
    params["fe"], params["fes"] = FAMILY_EXTRA
    if PROVIDER_TOKEN:
        params["pc"] = 1
    if API_URL:
        params["api"] = API_URL
    if SUPPORT_BOT:
        params["sb"] = SUPPORT_BOT
    if db_get_slots(user_id) > 1:
        params["fm"] = db_get_slots(user_id)
    if db_get_people(user_id) > 1:
        params["pm"] = db_get_people(user_id)
    for key in TARIFFS:
        if "_" in key:   # варианты с несколькими людьми — в компактных списках выше
            continue
        if tariff_allowed(key, user_id):
            for prefix in ("s", "c"):
                link = INVOICE_LINKS.get(prefix + key + (suf if key not in ADMIN_ONLY else ""))
                if link:
                    params[prefix + key] = link
    # Ссылки на оплату вариантов с несколькими людьми — компактными списками (пока URL не слишком длинный).
    # Что не влезло — кабинет откроет оплату в чате с ботом.
    budget = URL_BUDGET - len(urlencode(params)) - len(WEBAPP_URL) - 200
    for prefix, code, cls in (("s", "k", "std"), ("s", "f", "fam"), ("s", "v", "vip"), ("c", "k", "std"), ("c", "f", "fam"), ("c", "v", "vip")):
        ckey, _, _, _, _, lo, hi = _CLASSES[cls]
        slugs, ok = [], True
        for n in range(lo, hi + 1):
            for tk, *_ in TERMS:
                link = INVOICE_LINKS.get(prefix + f"{ckey}{n}_{tk}" + suf, "")
                if not link.startswith("https://t.me/$"):
                    ok = False
                slugs.append(link[len("https://t.me/$"):])
        value = ".".join(slugs)
        if ok and len(value) + 6 <= budget:
            params[code + prefix] = value
            budget -= len(value) + 6
    sep = "&" if "?" in WEBAPP_URL else "?"
    return WEBAPP_URL + sep + urlencode(params)


async def set_menu_for_user(user_id: int):
    """Ставит этому пользователю синюю кнопку «Открыть VPN» рядом с полем ввода."""
    if not WEBAPP_URL:
        return
    await build_invoice_links(db_get_discount(user_id))
    try:
        await bot.set_chat_menu_button(
            chat_id=user_id,
            menu_button=MenuButtonWebApp(text=BUTTON_TEXT, web_app=WebAppInfo(url=webapp_url(user_id))),
        )
    except Exception as e:
        logging.warning("Не удалось поставить кнопку меню для %s: %s", user_id, e)


def open_kb(user_id: int) -> InlineKeyboardMarkup | None:
    if not WEBAPP_URL:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"🚀 {BUTTON_TEXT}", web_app=WebAppInfo(url=webapp_url(user_id)))]
        ]
    )


def add_open(kb: InlineKeyboardMarkup | None, user_id: int) -> InlineKeyboardMarkup | None:
    """Добавляет кнопку «Открыть VPN» первой строкой под сообщением (как у других ботов)."""
    top = open_kb(user_id)
    if top is None:
        return kb
    rows = list(top.inline_keyboard) + (list(kb.inline_keyboard) if kb else [])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ==========================================
# ⌨️ КЛАВИАТУРЫ (оплата и вопросы идут через чат)
# ==========================================
cancel_kb = InlineKeyboardMarkup(
    inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="support_cancel")]]
)

sub_kb = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="📺 Подписаться на канал", url=CHANNEL_LINK)],
        [InlineKeyboardButton(text="✅ Я подписался", callback_data="check_sub_data")],
    ]
)


def tariffs_kb(user_id: int) -> InlineKeyboardMarkup:
    rows = []
    for key, t in TARIFFS.items():
        if not tariff_allowed(key, user_id) or "_" in key:
            continue
        icon = "👑" if key.startswith("v") else "💎"
        rows.append([InlineKeyboardButton(text=f"{icon} {t[0]} — {t[1]} ₽", callback_data=f"plan_{key}")])
    return add_open(InlineKeyboardMarkup(inline_keyboard=rows), user_id)


def tiers_text() -> str:
    return TIER_INFO.replace("{b}", "<b>").replace("{/b}", "</b>")


def method_kb(key: str, user_id: int) -> InlineKeyboardMarkup:
    rub, stars = TARIFFS[key][1], TARIFFS[key][2]
    rows = []
    if PROVIDER_TOKEN:
        rows.append([InlineKeyboardButton(text=f"💳 Картой — {rub} ₽", callback_data=f"pay_card_{key}")])
    rows.append([InlineKeyboardButton(text=f"⭐ Telegram Stars — {stars} ⭐", callback_data=f"pay_stars_{key}")])
    rows.append([InlineKeyboardButton(text="🔙 Другой тариф", callback_data="buy_vpn")])
    return add_open(InlineKeyboardMarkup(inline_keyboard=rows), user_id)


admin_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="📢 Сделать рассылку", callback_data="adm_broadcast")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="adm_stats")],
    ]
)


# ==========================================
# 🔒 ПРОВЕРКА ПОДПИСКИ НА КАНАЛ
# ==========================================
async def is_subscribed(user_id: int) -> bool:
    if not CHANNEL_ID:  # проверка отключена
        return True
    try:
        member = await bot.get_chat_member(CHANNEL_ID, user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception as e:
        logging.warning("Не удалось проверить подписку (бот админ в канале?): %s", e)
        return False


# ==========================================
# 📡 ХЕНДЛЕРЫ
# ==========================================
WELCOME_TEXT = (
    f"🚀 {hbold('Azika VPN')}\n\n"
    f"Нажми синюю кнопку {hbold('«' + BUTTON_TEXT + '»')} слева от поля ввода — "
    "там покупка подписки, твой ключ, настройка и помощь.\n\n"
    "❓ Есть вопрос? Просто напиши его сюда — он автоматически уйдёт в поддержку."
)


@dp.message(CommandStart())
async def start_handler(message: Message, command: CommandObject, state: FSMContext):
    uid = message.from_user.id
    await state.clear()
    is_new = not db_user_exists(uid)
    db_add_user(uid, message.from_user.first_name)

    # Реферальная ссылка вида t.me/бот?start=ref_123456
    arg = command.args or ""
    if is_new and arg.startswith("ref_") and arg[4:].isdigit():
        referrer = int(arg[4:])
        if db_add_referral(uid, referrer):
            try:
                await bot.send_message(
                    referrer,
                    f"👥 {hbold('Новый друг!')}\n\nПо твоей ссылке пришёл "
                    f"{escape(message.from_user.full_name)}. Когда он оплатит подписку, "
                    f"ты получишь +{REF_BONUS_DAYS} дн.",
                    reply_markup=open_kb(referrer),
                )
                await set_menu_for_user(referrer)
            except Exception as e:
                logging.info("Не удалось уведомить пригласившего: %s", e)

    if not await is_subscribed(uid):
        await message.answer(
            f"🚀 {hbold('Привет! Добро пожаловать в Azika VPN!')}\n\n"
            "Чтобы получить доступ к быстрому и безопасному VPN, "
            "подпишись на наш официальный Telegram-канал!",
            reply_markup=sub_kb,
        )
        return

    await set_menu_for_user(uid)
    if WEBAPP_URL:
        # ReplyKeyboardRemove убирает старые кнопки «Личный кабинет» и т.п.
        await message.answer(WELCOME_TEXT, reply_markup=add_open(None, uid))
        # старые кнопки меню из прежней версии бота убираем коротким сообщением, которое сразу удаляется
        try:
            rm = await message.answer("🔄", reply_markup=ReplyKeyboardRemove())
            await rm.delete()
        except Exception:
            pass
    else:
        await message.answer("⚠️ Кабинет пока недоступен. Попробуй позже.", reply_markup=ReplyKeyboardRemove())

    # Переходы из мини-приложения: t.me/бот?start=buy_30 или ?start=support
    if arg.startswith("buy_") and tariff_allowed(arg[4:], uid):
        key = arg[4:]
        await message.answer(
            f"Тариф: {hbold(TARIFFS[key][0])}\nВыбери способ оплаты:", reply_markup=method_kb(key, uid)
        )
    elif arg == "support":
        if SUPPORT_BOT:
            await message.answer("💬 Напиши нам в бот поддержки — ответим прямо в Telegram.", reply_markup=support_kb())
        else:
            await state.set_state(SupportStates.waiting_question)
            await message.answer(
                "✍️ Опиши свой вопрос одним сообщением. Укажи, что именно не работает.",
                reply_markup=cancel_kb,
            )


@dp.callback_query(F.data == "check_sub_data")
async def check_subscription_callback(callback: CallbackQuery):
    uid = callback.from_user.id
    if await is_subscribed(uid):
        await callback.message.delete()
        await set_menu_for_user(uid)
        await callback.message.answer(
            "✅ Подписка подтверждена!\n\n" + WELCOME_TEXT, reply_markup=add_open(None, uid)
        )
        await callback.answer()
    else:
        await callback.answer("❌ Ты всё ещё не подписался на наш канал!", show_alert=True)


# ---------- ПОДДЕРЖКА (вопрос приходит на почту и в Telegram) ----------
_last_ticket: dict[int, float] = {}
TICKET_COOLDOWN = 30  # секунд между вопросами (защита от спама)


@dp.callback_query(F.data == "support_cancel")
async def support_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Отменено.")
    await callback.answer()


@dp.message(SupportStates.waiting_question)
async def support_receive(message: Message, state: FSMContext):
    await submit_ticket(message, state)


def support_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="💬 Написать в поддержку", url=f"https://t.me/{SUPPORT_BOT}")
    ]])


async def submit_ticket(message: Message, state: FSMContext | None = None):
    """Отправляет вопрос пользователя на почту поддержки и админу в Telegram."""
    if not message.text:
        await message.answer("Пожалуйста, отправь вопрос текстом.")
        return

    user = message.from_user
    now = time.monotonic()
    if now - _last_ticket.get(user.id, 0) < TICKET_COOLDOWN:
        await message.answer("⏳ Подожди немного перед следующим вопросом.")
        return
    _last_ticket[user.id] = now
    if state is not None:
        await state.clear()

    u = db_get_user(user.id)
    body = (
        "Новый вопрос из бота Azika VPN\n"
        "--------------------------------\n"
        f"Имя: {user.full_name}\n"
        f"Username: {'@' + user.username if user.username else 'нет'}\n"
        f"Telegram ID: {user.id}\n"
        f"Подписка до: {fmt_exp(u['expire_date'])}\n"
        f"Время: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        "--------------------------------\n"
        f"{message.text}\n"
    )

    # Дублируем вопрос админу в Telegram, чтобы ничего не потерялось
    tg_ok = False
    try:
        await bot.send_message(
            ADMIN_ID,
            f"🆘 {hbold('Новый вопрос')}\n"
            f"От: {escape(user.full_name)} "
            f"({'@' + escape(user.username) if user.username else 'без username'})\n"
            f"ID: {hcode(str(user.id))}\n\n{escape(message.text)}",
        )
        tg_ok = True
    except Exception as e:
        logging.error("Не удалось отправить вопрос админу: %s", e)

    if tg_ok:
        await message.answer(
            "✅ Вопрос отправлен! Мы ответим как можно скорее.", reply_markup=add_open(None, user.id)
        )
    else:
        await message.answer(
            "😔 Не получилось отправить вопрос. Попробуй ещё раз чуть позже."
        )


# ---------- ТАРИФЫ И ОПЛАТА ----------
@dp.callback_query(F.data == "buy_vpn")
async def buy_vpn_handler(callback: CallbackQuery):
    await callback.message.edit_text(
        f"💎 {hbold('Выбери тарифный план VPN:')}\n\n{tiers_text()}", reply_markup=tariffs_kb(callback.from_user.id)
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("plan_"))
async def choose_method(callback: CallbackQuery):
    key = callback.data.split("_", 1)[1]
    if not tariff_allowed(key, callback.from_user.id):
        return await callback.answer("Тариф не найден", show_alert=True)
    await callback.message.edit_text(
        f"Тариф: {hbold(TARIFFS[key][0])}\nВыбери способ оплаты:",
        reply_markup=method_kb(key, callback.from_user.id),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("pay_card_"))
async def pay_card(callback: CallbackQuery):
    if sales_paused():
        return await callback.answer(sales_paused(), show_alert=True)
    key = callback.data.split("_", 2)[2]
    if not tariff_allowed(key, callback.from_user.id) or not PROVIDER_TOKEN:
        return await callback.answer("Оплата картой недоступна", show_alert=True)
    name, rub, _, _, label = TARIFFS[key]
    pct = 0 if key in ADMIN_ONLY else db_get_discount(callback.from_user.id)
    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title=f"Azika VPN — {name}",
        description=f"Подписка на VPN на {label}" + (f" (скидка {pct}%)" if pct else ""),
        payload=f"sub:{key}:{pct}",
        provider_token=PROVIDER_TOKEN,
        currency="RUB",
        prices=[LabeledPrice(label=f"Подписка {name}", amount=price_with_discount(rub, key, pct) * 100)],  # в копейках
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("pay_stars_"))
async def pay_stars(callback: CallbackQuery):
    if sales_paused():
        return await callback.answer(sales_paused(), show_alert=True)
    key = callback.data.split("_", 2)[2]
    if not tariff_allowed(key, callback.from_user.id):
        return await callback.answer("Тариф не найден", show_alert=True)
    name, _, stars, _, label = TARIFFS[key]
    pct = 0 if key in ADMIN_ONLY else db_get_discount(callback.from_user.id)
    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title=f"Azika VPN — {name}",
        description=f"Подписка на VPN на {label}" + (f" (скидка {pct}%)" if pct else ""),
        payload=f"sub:{key}:{pct}",
        provider_token="",  # для Stars токен пустой
        currency="XTR",
        prices=[LabeledPrice(label=f"Подписка {name}", amount=price_with_discount(stars, key, pct))],
    )
    await callback.answer()


@dp.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    parts = query.invoice_payload.split(":")
    key = parts[1] if len(parts) > 1 and parts[0] == "sub" else ""
    pct = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
    ok = tariff_allowed(key, query.from_user.id) and pct <= db_get_discount(query.from_user.id)
    if ok and len(parts) > 3 and parts[3]:
        ok = promo_check(query.from_user.id, parts[3])[0]
    await query.answer(ok=ok, error_message=None if ok else "Тариф недоступен")


@dp.message(F.successful_payment)
async def successful_payment(message: Message):
    pay = message.successful_payment
    parts = pay.invoice_payload.split(":")
    key = parts[1] if len(parts) > 1 else ""
    used_pct = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
    used_promo = parts[3] if len(parts) > 3 else ""
    if key not in TARIFFS:
        return
    minutes, label = TARIFFS[key][3], TARIFFS[key][4]
    user_id = message.from_user.id

    # Защита от повторной обработки
    if not db_register_payment(
        pay.telegram_payment_charge_id, user_id, key, pay.total_amount, pay.currency
    ):
        return

    db_add_user(user_id, message.from_user.first_name)
    if used_pct:
        db_use_discount(user_id, used_pct)   # скидка списана
    if used_promo:
        promo_consume(user_id, used_promo)
    if key != "test":
        db_set_tier(user_id, "vip" if key.startswith("v") else "std")
    new_date = db_add_subscription(user_id, minutes)
    if key != "test":
        db_set_slots(user_id, family_n(key))
        _cls, _n = parse_tariff(key)
        db_set_people(user_id, _n if _cls != "fam" else 1)
    try:
        await xui_sync_user(user_id)
    except Exception as e:
        logging.error("3x-ui: не удалось создать ключ для %s: %s", user_id, e)
        try:
            await bot.send_message(ADMIN_ID, f"⚠️ 3x-ui: ключ для {user_id} не создан: {escape(str(e))}")
        except Exception:
            pass
    keys = get_vpn_keys(user_id) or ["⏳ Ключ создаётся — он появится в кабинете в течение минуты."]
    vpn_key = "\n\n".join(f"{i}. {k}" for i, k in enumerate(keys, 1)) if len(keys) > 1 else keys[0]
    await set_menu_for_user(user_id)  # обновляем данные в кабинете

    await message.answer(
        f"🎉 {hbold('Оплата прошла успешно!')}\n\n"
        f"📅 Подписка продлена на {hbold(label)} и активна до: {hcode(fmt_exp(new_date))}\n\n"
        + (f"👨‍👩‍👧 {hbold(f'Семейный тариф: {db_get_slots(user_id)} чел.')} — по одному ключу на человека (1 ключ = 1 устройство).\n\n" if db_get_slots(user_id) > 1 else "")
        + (f"👥 {hbold(f'Ключ рассчитан на {db_get_people(user_id)} чел.')} — им могут пользоваться одновременно.\n\n" if db_get_people(user_id) > 1 else "")
        + key_block(user_id) + "\n\n"
        f"Инструкция по настройке — в кабинете («{BUTTON_TEXT}»).",
        reply_markup=key_kb(user_id),
    )
    # Реферальный бонус: пригласивший получает дни за первую оплату друга
    referrer = db_pending_referrer(user_id)
    if referrer:
        db_mark_rewarded(user_id)
        # Второй уровень: тот, кто пригласил твоего пригласившего, получает скидку на подписку
        grand = db_referrer_of(referrer)
        if grand:
            new_disc = db_add_discount(grand)
            await set_menu_for_user(grand)
            try:
                await bot.send_message(
                    grand,
                    f"🎉 {hbold('Скидка за друга друга!')}\n\n"
                    f"Друг твоего друга оплатил подписку. Твоя скидка на следующую покупку: {hbold(str(new_disc) + '%')}"
                    + (" (максимум)." if new_disc >= DISCOUNT_MAX else "."),
                    reply_markup=open_kb(grand),
                )
            except Exception as e:
                logging.info("Не удалось уведомить о скидке: %s", e)
        ref_until = db_add_subscription(referrer, REF_BONUS_DAYS * 1440)
        await set_menu_for_user(referrer)
        try:
            await bot.send_message(
                referrer,
                f"🎁 {hbold('Бонус за друга!')}\n\n"
                f"{escape(message.from_user.full_name)} оплатил подписку. "
                f"Тебе начислено +{REF_BONUS_DAYS} дн., подписка активна до {hcode(fmt_exp(ref_until))}.",
                reply_markup=open_kb(referrer),
            )
        except Exception as e:
            logging.info("Не удалось уведомить о бонусе: %s", e)

    try:
        amount = pay.total_amount / 100 if pay.currency != "XTR" else pay.total_amount
        await bot.send_message(
            ADMIN_ID,
            f"💰 Новая оплата: {message.from_user.full_name} ({user_id}) — "
            f"{amount} {pay.currency}, {label}.",
        )
    except Exception:
        pass


# ---------- АДМИНКА ----------
@dp.message(Command("admin"))
async def admin_panel(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("🛠 Панель администратора Azika VPN:", reply_markup=admin_keyboard)


@dp.message(Command("give"))
async def admin_give(message: Message):
    """/give <user_id> <дни> — выдать подписку вручную (для тестов)."""
    if message.from_user.id != ADMIN_ID:
        return
    parts = (message.text or "").split()
    usage = "Использование: /give <user_id> <дни>  или  /give <user_id> <минуты>m (например 5m)"
    if len(parts) != 3 or not parts[1].lstrip("-").isdigit():
        return await message.answer(usage)
    val = parts[2]
    if val.isdigit():
        minutes = int(val) * 1440
    elif val.endswith("m") and val[:-1].isdigit():
        minutes = int(val[:-1])
    else:
        return await message.answer(usage)
    uid = int(parts[1])
    db_add_user(uid, "Пользователь")
    new_date = db_add_subscription(uid, minutes)
    await set_menu_for_user(uid)
    await message.answer(f"✅ Подписка выдана до {fmt_exp(new_date)}")


@dp.callback_query(F.data == "adm_stats")
async def admin_stats_callback(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return await callback.answer()
    total, active = db_stats()
    await callback.answer(f"📊 Пользователей: {total}\n✅ С активной подпиской: {active}", show_alert=True)


@dp.callback_query(F.data == "adm_broadcast")
async def admin_broadcast_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return await callback.answer()
    await state.set_state(AdminStates.waiting_msg)
    await callback.message.answer("📥 Отправь сообщение для рассылки (текст, фото и т.д.):")
    await callback.answer()


@dp.message(AdminStates.waiting_msg)
async def admin_broadcast_run(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    users = db_all_ids()
    await message.answer(f"⏳ Рассылка идёт на {len(users)} аккаунтов...")
    success = 0
    for uid in users:
        try:
            await message.copy_to(uid)
            success += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)
    await message.answer(f"🏁 Рассылка окончена! Доставлено: {success} из {len(users)}.")


# ---------- ЛЮБОЙ ДРУГОЙ ТЕКСТ (должен стоять последним) ----------
# ==========================================
# 🌐 МИНИ-СЕРВЕР ДЛЯ ОПЛАТЫ ВНУТРИ ПРИЛОЖЕНИЯ
# ==========================================
def verify_init_data(init_data: str) -> dict | None:
    """Проверяет подпись Telegram (initData) и возвращает данные пользователя."""
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        got = pairs.pop("hash")
        check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(hmac.new(secret, check.encode(), hashlib.sha256).hexdigest(), got):
            return None
        if time.time() - int(pairs.get("auth_date", "0")) > 86400:
            return None
        return json.loads(pairs["user"])
    except Exception:
        return None


def _quote(user_id: int, key: str, promo: str) -> dict:
    if not tariff_allowed(key, user_id):
        return {"ok": False, "msg": "Тариф недоступен"}
    name, rub, stars, _, label = TARIFFS[key]
    ref_pct = 0 if key in ADMIN_ONLY else db_get_discount(user_id)
    promo_pct, msg, promo_ok = 0, "", True
    if promo:
        promo_ok, promo_pct, msg = promo_check(user_id, promo)
        if key in ADMIN_ONLY:
            promo_ok, promo_pct, msg = False, 0, "На тестовый тариф промокод не действует"
    return {
        "ok": True, "name": name, "label": label,
        "rub": final_price(rub, key, ref_pct, promo_pct), "stars": final_price(stars, key, ref_pct, promo_pct),
        "base_rub": rub, "base_stars": stars, "ref_pct": ref_pct, "promo_pct": promo_pct,
        "promo_ok": promo_ok, "msg": msg,
    }


@web.middleware
async def cors_mw(request, handler):
    if request.method == "OPTIONS":
        resp = web.Response()
    else:
        try:
            resp = await handler(request)
        except web.HTTPException as e:
            resp = e
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, ngrok-skip-browser-warning"
    resp.headers["Access-Control-Allow-Methods"] = "POST, GET, OPTIONS"
    return resp


async def _read(request) -> tuple[dict | None, dict]:
    try:
        data = await request.json()
    except Exception:
        return None, {}
    return verify_init_data(str(data.get("initData", ""))), data


async def api_ping(request):
    return web.json_response({"ok": True})


async def api_quote(request):
    user, data = await _read(request)
    if not user:
        return web.json_response({"ok": False, "msg": "Открой приложение из Telegram"}, status=401)
    return web.json_response(_quote(user["id"], str(data.get("key", "")), str(data.get("promo", ""))))


async def api_invoice(request):
    user, data = await _read(request)
    if not user:
        return web.json_response({"ok": False, "msg": "Открой приложение из Telegram"}, status=401)
    uid, key, method = user["id"], str(data.get("key", "")), str(data.get("method", "stars"))
    if sales_paused():
        return web.json_response({"ok": False, "msg": sales_paused()})
    promo = str(data.get("promo", "")).strip().upper()
    qt = _quote(uid, key, promo)
    if not qt["ok"]:
        return web.json_response(qt)
    if promo and not qt["promo_ok"]:
        return web.json_response({"ok": False, "msg": qt["msg"]})
    payload = f"sub:{key}:{qt['ref_pct']}" + (f":{promo}" if promo and qt["promo_ok"] else "")
    desc = f"Подписка на VPN на {qt['label']}"
    if qt["promo_pct"]:
        desc += f" (промокод −{qt['promo_pct']}%)"
    try:
        if method == "card":
            if not PROVIDER_TOKEN:
                return web.json_response({"ok": False, "msg": "Оплата картой недоступна"})
            link = await bot.create_invoice_link(
                title=f"Azika VPN — {qt['name']}", description=desc, payload=payload,
                provider_token=PROVIDER_TOKEN, currency="RUB",
                prices=[LabeledPrice(label=f"Подписка {qt['name']}", amount=qt["rub"] * 100)],
            )
        else:
            link = await bot.create_invoice_link(
                title=f"Azika VPN — {qt['name']}", description=desc, payload=payload,
                provider_token="", currency="XTR",
                prices=[LabeledPrice(label=f"Подписка {qt['name']}", amount=qt["stars"])],
            )
    except Exception as e:
        logging.warning("create_invoice_link: %s", e)
        return web.json_response({"ok": False, "msg": "Не удалось создать счёт. Попробуй позже"})
    return web.json_response({"ok": True, "link": link, **qt})



def link_to_xray(link: str, fragment: bool = False) -> dict | None:
    """vless://-ссылка -> конфиг Xray для Happ (логика в xui.link_to_xray)."""
    return xui.link_to_xray(link, fragment)


async def sub_handler(request):
    """GET /sub/<токен> — подписка: список ключей пользователя в base64 + служебные заголовки для приложений."""
    token = request.match_info.get("token", "")
    with db() as conn:
        row = conn.execute("SELECT user_id FROM sub_tokens WHERE token = ?", (token,)).fetchone()
    if not row:
        return web.Response(status=404, text="not found")
    uid = row[0]
    u = db_get_user(uid)
    exp = parse_exp(u["expire_date"])
    keys = get_vpn_keys(uid) if is_active(u["expire_date"]) else []
    as_json = request.query.get("fmt") == "json"
    if as_json:
        frag = request.query.get("frag") == "1"   # ?fmt=json&frag=1 — с фрагментацией TLS (опыт, по умолчанию выключена)
        body = json.dumps([c for c in (link_to_xray(k, frag) for k in keys) if c], ensure_ascii=False)
    else:
        body = base64.b64encode("\n".join(keys).encode()).decode()
    headers = {
        "Content-Type": "application/json; charset=utf-8" if as_json else "text/plain; charset=utf-8",
        "Cache-Control": "no-store",
        "profile-title": "Azika VPN",
        "profile-update-interval": "1",
        "subscription-userinfo": f"upload=0; download=0; total=0; expire={int(exp.timestamp()) if exp else 0}",
    }
    if BOT_USERNAME:
        headers["profile-web-page-url"] = f"https://t.me/{BOT_USERNAME}"
    if SUPPORT_BOT:
        headers["support-url"] = f"https://t.me/{SUPPORT_BOT}"
    return web.Response(text=body, headers=headers)


async def start_api():
    app = web.Application(middlewares=[cors_mw])
    app.router.add_get("/api/ping", api_ping)
    app.router.add_route("*", "/api/quote", api_quote)
    app.router.add_route("*", "/api/invoice", api_invoice)
    app.router.add_get("/sub/{token}", sub_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, os.getenv("API_HOST", "0.0.0.0"), API_PORT).start()
    print(f"🌐 Сервер оплаты запущен на порту {API_PORT}" + (f" → {API_URL}" if API_URL else " (API_URL не задан — оплата будет через ссылки/чат)"))


# ---- Промокоды (только админ) ----
@dp.message(Command("addpromo"))
async def addpromo_cmd(message: Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        code, pct, uses = (command.args or "").split()
        code, pct, uses = code.upper(), int(pct), int(uses)
        assert 1 <= pct <= 99 and uses >= 1
    except Exception:
        return await message.answer("Формат: /addpromo КОД ПРОЦЕНТ(1-99) ЧИСЛО_ИСПОЛЬЗОВАНИЙ\nНапример: /addpromo SALE20 20 100")
    with db() as conn:
        conn.execute("INSERT OR REPLACE INTO promos (code, pct, uses_left) VALUES (?, ?, ?)", (code, pct, uses))
        conn.commit()
    await message.answer(f"✅ Промокод {hcode(code)}: −{pct}%, использований: {uses}")


@dp.message(Command("delpromo"))
async def delpromo_cmd(message: Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID:
        return
    code = (command.args or "").strip().upper()
    with db() as conn:
        conn.execute("DELETE FROM promos WHERE code = ?", (code,))
        conn.commit()
    await message.answer(f"Промокод {hcode(code)} удалён.")


@dp.message(Command("promos"))
async def promos_cmd(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    with db() as conn:
        rows = conn.execute("SELECT code, pct, uses_left FROM promos ORDER BY code").fetchall()
    await message.answer("🎟 Промокоды:\n" + ("\n".join(f"{hcode(c)} — −{p}%, осталось {u}" for c, p, u in rows) or "пока нет"))


# ==========================================
# 🔑 КЛЮЧ: ПОКАЗАТЬ И ЗАПРОСИТЬ НОВЫЙ
# ==========================================
KEY_RESET_COOLDOWN = 10 * 60   # новый ключ можно выпускать не чаще, чем раз в 10 минут


def key_kb(user_id: int):
    """Кнопки под сообщением с ключом: «Открыть VPN» + «Запросить новый ключ»."""
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔄 Запросить новый ключ", callback_data="newkey")
    ]])
    return add_open(kb, user_id)


def key_text(user_id: int) -> str:
    return key_block(user_id)


@dp.message(Command("key"))
async def cmd_key(message: Message):
    uid = message.from_user.id
    if not is_active(db_get_user(uid)["expire_date"]):
        await message.answer("У тебя нет активной подписки. Оформи её в кабинете 👇", reply_markup=open_kb(uid))
        return
    await message.answer(key_text(uid), reply_markup=key_kb(uid))


@dp.callback_query(F.data == "newkey")
async def newkey_ask(callback: CallbackQuery):
    uid = callback.from_user.id
    if not xui.ENABLED or not is_active(db_get_user(uid)["expire_date"]):
        await callback.answer("Нужна активная подписка.", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да, выпустить новый", callback_data="newkey_yes"),
        InlineKeyboardButton(text="Отмена", callback_data="newkey_no"),
    ]])
    await callback.message.answer(
        f"⚠️ {hbold('Выпустить новый ключ?')}\n\nСтарый ключ и старая ссылка-подписка сразу перестанут работать, "
        "их нужно будет заменить в приложении на новые.",
        reply_markup=kb,
    )
    await callback.answer()


@dp.callback_query(F.data == "newkey_no")
async def newkey_no(callback: CallbackQuery):
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer("Отменено")


@dp.callback_query(F.data == "newkey_yes")
async def newkey_do(callback: CallbackQuery):
    uid = callback.from_user.id
    if not xui.ENABLED or not is_active(db_get_user(uid)["expire_date"]):
        await callback.answer("Нужна активная подписка.", show_alert=True)
        return
    now = int(time.time())
    with db() as conn:
        row = conn.execute("SELECT ts FROM key_resets WHERE user_id = ?", (uid,)).fetchone()
    if row and now - row[0] < KEY_RESET_COOLDOWN:
        wait = (KEY_RESET_COOLDOWN - (now - row[0]) + 59) // 60
        await callback.answer(f"Новый ключ можно выпустить через {wait} мин.", show_alert=True)
        return
    await callback.answer("Выпускаю новый ключ…")
    try:
        with db() as conn:
            olds = conn.execute("SELECT srv, idx FROM xui_keys2 WHERE user_id = ?", (uid,)).fetchall()
        pmap = {p.sid: p for p in xui.PANELS}
        for srv, idx in olds:
            try:
                if srv in pmap:
                    await pmap[srv].delete_client(f"u{uid}-{idx}")
            except Exception as e:
                logging.warning("3x-ui[%s]: не удалось удалить старый ключ %s-%s: %s", srv, uid, idx, e)
        with db() as conn:
            conn.execute("DELETE FROM xui_keys2 WHERE user_id = ?", (uid,))
            conn.execute("DELETE FROM sub_tokens WHERE user_id = ?", (uid,))
            conn.execute("INSERT OR REPLACE INTO key_resets (user_id, ts) VALUES (?, ?)", (uid, now))
            conn.commit()
        await xui_sync_user(uid)
    except Exception as e:
        logging.error("3x-ui: не удалось выпустить новый ключ для %s: %s", uid, e)
        await callback.message.answer("😔 Не получилось выпустить ключ. Попробуй позже или напиши в поддержку.")
        return
    await set_menu_for_user(uid)
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.message.answer(f"✅ {hbold('Новый ключ готов')}\n\n" + key_text(uid), reply_markup=key_kb(uid))


@dp.message(F.text)
async def fallback_text(message: Message):
    """Любое обычное сообщение боту — ведём в бот поддержки (или, если он не задан, отправляем админу)."""
    uid = message.from_user.id
    db_add_user(uid, message.from_user.first_name)
    if not await is_subscribed(uid):
        await message.answer("Сначала подпишись на канал 👇", reply_markup=sub_kb)
        return
    await set_menu_for_user(uid)
    if (message.text or "").startswith("/"):   # неизвестная команда — просто подсказываем, куда нажать
        await message.answer(WELCOME_TEXT, reply_markup=add_open(None, uid))
        return
    if SUPPORT_BOT:
        await message.answer("💬 По любым вопросам пиши в наш бот поддержки:", reply_markup=support_kb())
        return
    await submit_ticket(message)


# ==========================================
# ⏰ НАПОМИНАНИЯ О СРОКЕ ПОДПИСКИ
# ==========================================
REMIND_EVERY = 60  # секунд между проверками


async def send_reminder(uid: int, kind: str, exp: datetime):
    until = exp.strftime("%d.%m.%Y %H:%M")
    if kind == "3d":
        text = (
            f"⏰ {hbold('Подписка скоро закончится')}\n\n"
            f"Она действует до {hcode(until)} (осталось около 3 дней). "
            "Продли заранее, чтобы VPN не отключился."
        )
    elif kind == "1d":
        text = (
            f"⚠️ {hbold('Осталось меньше суток')}\n\n"
            f"Подписка действует до {hcode(until)}. Продли её сейчас."
        )
    else:
        text = (
            f"❌ {hbold('Подписка закончилась')}\n\n"
            f"Срок истёк {hcode(until)}. Чтобы снова пользоваться VPN, продли подписку."
        )
    try:
        await bot.send_message(
            uid, text + f"\n\nНажми «{BUTTON_TEXT}», чтобы продлить.", reply_markup=open_kb(uid)
        )
    except Exception as e:
        logging.info("Не удалось отправить напоминание %s: %s", uid, e)
    if kind == "expired":
        await set_menu_for_user(uid)


async def notify_loop():
    """Раз в минуту проверяет сроки и присылает напоминания: за 3 дня, за сутки и при окончании."""
    while True:
        try:
            now = datetime.now()
            with db() as conn:
                rows = conn.execute(
                    "SELECT user_id, expire_date FROM users WHERE expire_date IS NOT NULL"
                ).fetchall()
            for uid, exp_str in rows:
                exp = parse_exp(exp_str)
                if not exp:
                    continue
                left = exp - now
                if left <= timedelta(0):
                    if -left > timedelta(days=1):  # давно закончилась — не беспокоим
                        continue
                    kind = "expired"
                elif left <= timedelta(days=1):
                    kind = "1d"
                elif left <= timedelta(days=3):
                    kind = "3d"
                else:
                    continue
                if db_mark_notified(uid, int(exp.timestamp()), kind):
                    await send_reminder(uid, kind, exp)
                    await asyncio.sleep(0.05)
        except Exception as e:
            logging.error("Ошибка в напоминаниях: %s", e)
        await asyncio.sleep(REMIND_EVERY)


# ==========================================
# 🚀 ЗАПУСК
# ==========================================
_bg_tasks: set = set()


async def refresh_all_menus():
    """При запуске выдаём кнопку «Открыть VPN» всем, кто уже пользовался ботом."""
    for uid in db_all_ids():
        if await is_subscribed(uid):
            await set_menu_for_user(uid)
        await asyncio.sleep(0.05)


async def tunnel_watcher():
    """Сам находит адрес туннеля cloudflared (tunnel.bat) — вручную вставлять ссылку не нужно.
    Если адрес поменялся (туннель перезапускали), обновляет кнопку «Открыть VPN» у всех."""
    global API_URL
    if os.getenv("API_URL"):
        return  # адрес задан вручную (постоянный) — следить не нужно
    last_try_msg = False
    while True:
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.get("http://127.0.0.1:20241/quicktunnel", timeout=aiohttp.ClientTimeout(total=3)) as r:
                    host = (await r.json(content_type=None)).get("hostname", "")
            new = f"https://{host}" if host else ""
            if new and new != API_URL:
                API_URL = new
                print(f"🔗 Найден адрес туннеля: {API_URL} — обновляю кнопки…")
                task = asyncio.create_task(refresh_all_menus())
                _bg_tasks.add(task)
                task.add_done_callback(_bg_tasks.discard)
        except Exception:
            if not last_try_msg and not API_URL:
                print("ℹ️ Туннель пока не найден (запусти tunnel.bat или start_all.bat). Проверяю каждые 10 секунд…")
                last_try_msg = True
        await asyncio.sleep(10)


async def main():
    global BOT_USERNAME
    if not BOT_TOKEN:
        raise SystemExit("Задай переменную окружения BOT_TOKEN")
    if not WEBAPP_URL:
        print("⚠️ WEBAPP_URL не задан — кнопка «Открыть VPN» не появится!")
    init_db()
    xui_init()
    await init_server_labels()
    BOT_USERNAME = (await bot.get_me()).username or ""
    await bot.delete_webhook(drop_pending_updates=True)
    # Для тех, у кого ещё нет персональной кнопки, остаётся стандартное меню
    try:
        await bot.set_chat_menu_button(menu_button=MenuButtonDefault())
    except Exception as e:
        logging.warning("Не удалось сбросить общую кнопку меню: %s", e)
    await build_invoice_links()
    await start_api()
    for coro in (refresh_all_menus(), notify_loop(), tunnel_watcher(), xui_sync_loop()):
        task = asyncio.create_task(coro)
        _bg_tasks.add(task)
        task.add_done_callback(_bg_tasks.discard)
    print("Бот успешно запущен и готов к работе!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
