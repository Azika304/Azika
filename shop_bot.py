"""Azika Shop — Telegram-бот + Mini App для пополнения игровых аккаунтов.

Оплата: Telegram Stars. Выдача: вручную админом (кнопки «Выполнено» / «Отказ + возврат Stars»).
Есть: промокоды, реферальная программа с бонусным балансом, профиль, фото товаров (шлёт админ боту).
Запуск: BOT_TOKEN=... WEBAPP_URL=... ADMIN_ID=... python shop_bot.py
"""
import asyncio
import hashlib
import hmac
import json
import logging
import math
import os
import re
import sqlite3
import time
from contextlib import closing
from html import escape
from urllib.parse import parse_qsl, quote, urlencode

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    MenuButtonDefault,
    MenuButtonWebApp,
    BotCommand,
    Message,
    PreCheckoutQuery,
    WebAppInfo,
)
import aiohttp
from aiohttp import web

logging.basicConfig(level=logging.INFO)

# ==========================================
# ⚙️ НАСТРОЙКИ (переменные окружения)
# ==========================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
WEBAPP_URL = os.getenv("WEBAPP_URL", "")          # адрес shop.html (GitHub Pages)
API_URL = os.getenv("API_URL", "").rstrip("/")    # например https://92-243-74-80.sslip.io:8443/shop
API_HOST = os.getenv("API_HOST", "127.0.0.1")
API_PORT = int(os.getenv("API_PORT", "8081"))
ADMIN_ID = int(os.getenv("ADMIN_ID", "6095977712"))
SUPPORT = os.getenv("SUPPORT", "@azikashop_support_bot")
DB_PATH = os.getenv("SHOP_DB", "/opt/azika_shop/shop.db")
IMG_DIR = os.getenv("SHOP_IMG_DIR", os.path.join(os.path.dirname(DB_PATH) or ".", "img"))
SHOP_NAME = os.getenv("SHOP_NAME", "Azika Shop")
REF_PERCENT = int(os.getenv("REF_PERCENT", "5"))  # % от оплаты друга -> бонусы пригласившему
STAR_RUB = float(os.getenv("STAR_RUB", "1.59"))      # сколько ₽ стоит 1⭐ (для пересчёта баллов: 1 балл = 1 ₽)
CASHBACK_PERCENT = int(os.getenv("CASHBACK_PERCENT", "5"))  # кэшбэк покупателю, % от оплаченных Stars
CB_LEVELS = [(0, 1), (int(os.getenv("CB_L2_FROM", "1000")), 3), (int(os.getenv("CB_L3_FROM", "3000")), 5)]   # (потрачено ⭐, % кэшбэка)
CASHBACK_MIN = int(os.getenv("CASHBACK_MIN", "100"))        # минимальная оплата (⭐) для кэшбэка
ORDER_TTL_MIN = int(os.getenv("ORDER_TTL_MIN", "30"))   # сколько минут даётся на оплату счёта
_FEES = [int(x) for x in os.getenv("SELLER_FEES", "10,8,6,4").split(",")]        # комиссия по уровням продавца, %
_LIMS = [int(x) for x in os.getenv("SELLER_LIMITS", "3,10,30,100").split(",")]   # лимит объявлений по уровням
# (название, сделок, отзывов, мин. рейтинг, комиссия %, лимит объявлений)
SELLER_LEVELS = [("Новичок", 0, 0, 0.0, _FEES[0], _LIMS[0]), ("Продавец", 5, 0, 0.0, _FEES[1], _LIMS[1]),
                 ("Профи", 20, 5, 4.5, _FEES[2], _LIMS[2]), ("Топ-продавец", 50, 10, 4.7, _FEES[3], _LIMS[3])]
SELLER_FEE = max(_FEES)
PAYOUT_MIN = int(os.getenv("PAYOUT_MIN", "100"))         # минимальный вывод продавца, ⭐
SELLER_BOT_TOKEN = os.getenv("SELLER_BOT_TOKEN", "")     # токен отдельного бота «Azika Sellers» (необязательно)
AD_PRICE = int(os.getenv("AD_PRICE_PER_DAY", "30"))       # реклама товара, ⭐ в сутки
SELLER_PROMO_MAX = int(os.getenv("SELLER_PROMO_MAX", "50"))  # максимальная скидка промокода продавца, %
SELLER_BOT_USERNAME = ""
SELLER_WEBAPP_URL = os.getenv("SELLER_WEBAPP_URL", "")    # адрес seller.html (GitHub Pages) — мини-приложение Azika Seller
AUTO_CONFIRM_DAYS = int(os.getenv("AUTO_CONFIRM_DAYS", "3"))  # через сколько дней заказ продавца подтверждается автоматически
VERIFY_MIN_REVIEWS = int(os.getenv("VERIFY_MIN_REVIEWS", "10"))   # когда предлагать админу выдать галочку
VERIFY_MIN_AVG = float(os.getenv("VERIFY_MIN_AVG", "4.7"))
SELLER_READY = bool(SELLER_BOT_TOKEN)   # станет False, если токен бота продавцов не подошёл
BOT_USERNAME = ""

bot = Bot(token=BOT_TOKEN or "0:stub", default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
seller_bot = Bot(token=SELLER_BOT_TOKEN or "0:stub", default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp2 = Dispatcher()   # диспетчер бота продавцов

# ==========================================
# 🎮 КАТАЛОГ  (цены в Telegram Stars — ПОМЕНЯЙ под свои: /setprice <id> <stars>)
# kind: supercell — тег (#ABC123) или Supercell ID (e-mail); numeric — числовой ID
# ==========================================
GAMES = [
    {"id": "bs", "name": "Brawl Stars", "emoji": "🌟", "color": "#f5a524", "kind": "supercell", "icon": "gem",
     "field": "Тег игрока (#ABC123) или Supercell ID (e-mail)",
     "packs": [("bs_g30", "30 гемов", 170), ("bs_g80", "80 гемов", 430), ("bs_g170", "170 гемов", 850),
               ("bs_g360", "360 гемов", 1700), ("bs_g950", "950 гемов", 4300), ("bs_g2000", "2000 гемов", 8500),
               ("bs_pass", "Brawl Pass", 430), ("bs_plus", "Brawl Pass Plus", 650)]},
    {"id": "cr", "name": "Clash Royale", "emoji": "👑", "color": "#2f7bff", "kind": "supercell", "icon": "gem",
     "field": "Тег игрока (#ABC123) или Supercell ID (e-mail)",
     "packs": [("cr_g80", "80 гемов", 150), ("cr_g500", "500 гемов", 750), ("cr_g1200", "1200 гемов", 1700),
               ("cr_g2500", "2500 гемов", 3300), ("cr_g6500", "6500 гемов", 8000), ("cr_g14000", "14000 гемов", 16000),
               ("cr_pass", "Pass Royale", 430)]},
    {"id": "coc", "name": "Clash of Clans", "emoji": "🏰", "color": "#22b34a", "kind": "supercell", "icon": "gem",
     "field": "Тег игрока (#ABC123) или Supercell ID (e-mail)",
     "packs": [("coc_g80", "80 гемов", 150), ("coc_g500", "500 гемов", 750), ("coc_g1200", "1200 гемов", 1700),
               ("coc_g2500", "2500 гемов", 3300), ("coc_g6500", "6500 гемов", 8000), ("coc_g14000", "14000 гемов", 16000),
               ("coc_pass", "Gold Pass", 430)]},
    {"id": "pubg", "name": "PUBG Mobile", "emoji": "🔫", "color": "#e8a33d", "kind": "numeric", "icon": "uc",
     "field": "ID игрока (цифры из профиля)",
     "packs": [("pubg_60", "60 UC", 90), ("pubg_325", "325 UC", 430), ("pubg_660", "660 UC", 850),
               ("pubg_1800", "1800 UC", 2150), ("pubg_3850", "3850 UC", 4300), ("pubg_8100", "8100 UC", 8500)]},
    {"id": "ff", "name": "Free Fire", "emoji": "🔥", "color": "#ff5a1f", "kind": "numeric", "icon": "diamond",
     "field": "ID игрока (цифры из профиля)",
     "packs": [("ff_100", "100 алмазов", 90), ("ff_310", "310 алмазов", 260), ("ff_520", "520 алмазов", 430),
               ("ff_1060", "1060 алмазов", 850), ("ff_2180", "2180 алмазов", 1700), ("ff_5600", "5600 алмазов", 4300),
               ("ff_week", "Недельная подписка", 170), ("ff_month", "Месячная подписка", 850)]},
    {"id": "so2", "name": "Standoff 2", "emoji": "🎯", "color": "#7c5cff", "kind": "numeric", "icon": "coin",
     "field": "ID игрока (цифры из профиля)",
     "packs": [("so2_100", "100 голды", 90), ("so2_500", "500 голды", 430), ("so2_1000", "1000 голды", 850),
               ("so2_2500", "2500 голды", 2100), ("so2_5000", "5000 голды", 4200), ("so2_10000", "10000 голды", 8300)]},
]

# 🎁 ПОДАРОЧНЫЕ КАРТЫ. Номинал указан в $ (цена считается от курса: /setrate 95), код выдаётся вручную в чате бота.
# kind: gift — вместо ID игрока покупатель оставляет e-mail или @username, куда прислать код.
GIFT_FIELD = "Контакт: @username или e-mail"
GIFT_USD_RUB = float(os.getenv("GIFT_USD_RUB", "95"))       # курс по умолчанию, меняется командой /setrate
GIFT_COST_K = float(os.getenv("GIFT_COST_K", "1.07"))        # во сколько раз закупка дороже номинала (комиссия поставщика)
GIFT_MARGIN_PCT = float(os.getenv("GIFT_MARGIN_PCT", "8"))   # твоя наценка, % от закупки
GIFT_MARGIN_MIN = int(os.getenv("GIFT_MARGIN_MIN", "25"))    # но не меньше, ⭐


def gift_stars_from_cost(rub: float) -> int:
    """Цена карты в ⭐: закупка + наценка (GIFT_MARGIN_PCT %, но не меньше GIFT_MARGIN_MIN ⭐), округление вверх до 5."""
    cs = rub / STAR_RUB
    return int(math.ceil((cs + max(GIFT_MARGIN_MIN, cs * GIFT_MARGIN_PCT / 100)) / 5.0) * 5)


GIFTS = [
    ("gc_apple", "Apple Gift Card (США)", "🍎", "#8e8e93", [10, 25, 50, 100], "App Store, iTunes, Apple Music, iCloud (аккаунт США)"),
    ("gc_gplay", "Google Play (США)", "▶️", "#34a853", [5, 10, 25, 50, 100], "Приложения, игры и подписки на Android (аккаунт США)"),
    ("gc_steam", "Steam Wallet", "🎮", "#2a475e", [5, 10, 20, 50, 100], "Пополнение кошелька Steam (регион USD)"),
    ("gc_psn", "PlayStation Store (США)", "🕹", "#0070d1", [10, 25, 50, 100], "Игры и PS Plus для аккаунта США"),
    ("gc_xbox", "Xbox Gift Card (США)", "🟩", "#107c10", [10, 25, 50, 100], "Игры, Game Pass, Microsoft Store"),
    ("gc_roblox", "Roblox Gift Card", "🧱", "#e2231a", [10, 25, 50, 100], "Robux и Premium"),
    ("gc_spotify", "Spotify Premium", "🎧", "#1db954", [10, 30, 60], "Подарочные карты Spotify (США)"),
]
GIFT_FACE: dict[str, int] = {}     # pack_id -> номинал в $
GIFT_DESC: dict[str, str] = {}     # game_id -> описание
for _gid, _name, _emoji, _color, _noms, _desc in GIFTS:
    _packs = []
    for _n in _noms:
        _pid = f"{_gid}_{_n}"
        GIFT_FACE[_pid] = _n
        _packs.append((_pid, f"${_n}", gift_stars_from_cost(_n * GIFT_USD_RUB * GIFT_COST_K)))
    GIFT_DESC[_gid] = _desc
    GAMES.append({"id": _gid, "name": _name, "emoji": _emoji, "color": _color, "kind": "gift", "icon": "card", "field": GIFT_FIELD, "packs": _packs})

# Себестоимость товаров в ₽ (ПРИБЛИЗИТЕЛЬНО, под рыночные цены перекупов — поменяй своими: /setcost ID РУБЛИ).
# Цена в ⭐ = себестоимость / STAR_RUB + твоя наценка 50..100⭐ (больше пакет — больше наценка), округление вверх до 5.
COST = {
    "bs_g30": 250, "bs_g80": 560, "bs_g170": 1090, "bs_g360": 2130, "bs_g950": 5200, "bs_g2000": 10500,
    "bs_pass": 700, "bs_plus": 1000,
    "cr_g80": 110, "cr_g500": 480, "cr_g1200": 920, "cr_g2500": 1850, "cr_g6500": 4500, "cr_g14000": 9000, "cr_pass": 450,
    "coc_g80": 110, "coc_g500": 480, "coc_g1200": 920, "coc_g2500": 1850, "coc_g6500": 4500, "coc_g14000": 9000, "coc_pass": 450,
    "pubg_60": 80, "pubg_325": 400, "pubg_660": 800, "pubg_1800": 2000, "pubg_3850": 4000, "pubg_8100": 8000,
    "ff_100": 80, "ff_310": 240, "ff_520": 400, "ff_1060": 800, "ff_2180": 1650, "ff_5600": 4000, "ff_week": 160, "ff_month": 800,
    "so2_100": 110, "so2_500": 560, "so2_1000": 950, "so2_2500": 2300, "so2_5000": 4500, "so2_10000": 8800,
}
MARGIN_MIN, MARGIN_MAX = 50, 100   # твоя прибыль с заказа в ⭐


def margin_for(cost_stars: float) -> int:
    return round(MARGIN_MIN + (MARGIN_MAX - MARGIN_MIN) * min(1.0, cost_stars / 1500))


def stars_from_cost(rub: float) -> int:
    cs = rub / STAR_RUB
    return int(math.ceil((cs + margin_for(cs)) / 5.0) * 5)
GAME_BY_ID = {g["id"]: g for g in GAMES}
PACKS = {}   # pack_id -> (game, label, default_stars)
for _g in GAMES:
    for _pid, _label, _stars in _g["packs"]:
        PACKS[_pid] = (_g, _label, _stars)
PASS_WORDS = ("pass", "plus", "week", "month")

LIVE = ("paid", "delivered", "dispute")            # деньги покупателя «в пути»: оплачено, но продавец ещё не получил
CHAT_OPEN = ("paid", "delivered", "dispute", "done")
STATUS_TXT = {
    "delivered": "передан, ждёт подтверждения", "dispute": "спор — решает поддержка",
    "new": "ожидает оплаты", "paid": "оплачен, ждёт выполнения", "done": "выполнен ✅",
    "refunded": "отменён, Stars возвращены", "expired": "отменён: не оплачен вовремя",
}


# ==========================================
# 🗄 БАЗА ДАННЫХ
# ==========================================
def db():
    return sqlite3.connect(DB_PATH, timeout=10)


def _addcol(c, table, col, ddl):
    if col not in [r[1] for r in c.execute(f"PRAGMA table_info({table})")]:
        c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")


def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    os.makedirs(IMG_DIR, exist_ok=True)
    with closing(db()) as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(user_id INTEGER PRIMARY KEY, name TEXT, username TEXT, joined INTEGER);
        CREATE TABLE IF NOT EXISTS orders(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, pack TEXT, game TEXT, label TEXT,
            player TEXT, comment TEXT, stars INTEGER, status TEXT DEFAULT 'new',
            charge_id TEXT, created INTEGER, paid_at INTEGER, done_at INTEGER);
        CREATE INDEX IF NOT EXISTS ix_orders_user ON orders(user_id);
        CREATE TABLE IF NOT EXISTS prices(pack TEXT PRIMARY KEY, stars INTEGER);
        CREATE TABLE IF NOT EXISTS costs(pack TEXT PRIMARY KEY, rub REAL);
        CREATE TABLE IF NOT EXISTS hidden(key TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS sellers(user_id INTEGER PRIMARY KEY, about TEXT DEFAULT '', status TEXT DEFAULT 'pending',
            balance INTEGER DEFAULT 0, earned INTEGER DEFAULT 0, created INTEGER);
        CREATE TABLE IF NOT EXISTS offers(id INTEGER PRIMARY KEY AUTOINCREMENT, seller_id INTEGER, game TEXT, title TEXT,
            descr TEXT DEFAULT '', stars INTEGER, status TEXT DEFAULT 'pending', created INTEGER, sold INTEGER DEFAULT 0);
        CREATE INDEX IF NOT EXISTS ix_offers_seller ON offers(seller_id);
        CREATE TABLE IF NOT EXISTS reviews(order_id INTEGER PRIMARY KEY, user_id INTEGER, seller_id INTEGER DEFAULT 0,
            target TEXT, rating INTEGER, text TEXT DEFAULT '', created INTEGER);
        CREATE INDEX IF NOT EXISTS ix_reviews_seller ON reviews(seller_id);
        CREATE TABLE IF NOT EXISTS ads(id INTEGER PRIMARY KEY AUTOINCREMENT, offer_id INTEGER, seller_id INTEGER, days INTEGER,
            cost INTEGER, created INTEGER);
        CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER, from_id INTEGER, to_id INTEGER,
            text TEXT, created INTEGER, read INTEGER DEFAULT 0);
        CREATE INDEX IF NOT EXISTS ix_msg_order ON messages(order_id);
        CREATE TABLE IF NOT EXISTS chat_state(bot TEXT, user_id INTEGER, order_id INTEGER, PRIMARY KEY(bot,user_id));
        CREATE TABLE IF NOT EXISTS payouts(id INTEGER PRIMARY KEY AUTOINCREMENT, seller_id INTEGER, stars INTEGER,
            details TEXT, status TEXT DEFAULT 'pending', created INTEGER);
        CREATE TABLE IF NOT EXISTS images(key TEXT PRIMARY KEY, ver INTEGER);
        CREATE TABLE IF NOT EXISTS promos(code TEXT PRIMARY KEY, pct INTEGER, max_uses INTEGER DEFAULT 0,
            used INTEGER DEFAULT 0, expires INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS promo_uses(code TEXT, user_id INTEGER, order_id INTEGER, PRIMARY KEY(code,user_id));
        """)
        c.execute("CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT)")   # настройки из админ-бота (пауза продаж и т.п.)
        _addcol(c, "promos", "paused", "INTEGER DEFAULT 0")
        _addcol(c, "sellers", "lvl_set", "INTEGER DEFAULT 0")
        _addcol(c, "sellers", "verify_req", "INTEGER DEFAULT 0")   # когда продавец подал заявку на галочку
        _addcol(c, "sellers", "verify_rej", "INTEGER DEFAULT 0")   # когда заявку отклонили
        _addcol(c, "users", "ref_by", "INTEGER DEFAULT 0")
        _addcol(c, "users", "bonus", "INTEGER DEFAULT 0")
        _addcol(c, "users", "ref_earned", "INTEGER DEFAULT 0")
        _addcol(c, "users", "photo", "TEXT DEFAULT ''")
        _addcol(c, "orders", "promo", "TEXT DEFAULT ''")
        _addcol(c, "orders", "base", "INTEGER DEFAULT 0")
        _addcol(c, "orders", "bonus", "INTEGER DEFAULT 0")
        _addcol(c, "orders", "cashback", "INTEGER DEFAULT 0")
        _addcol(c, "orders", "seller_id", "INTEGER DEFAULT 0")
        _addcol(c, "promos", "seller_id", "INTEGER DEFAULT 0")
        _addcol(c, "offers", "boost_until", "INTEGER DEFAULT 0")
        _addcol(c, "offers", "boost_at", "INTEGER DEFAULT 0")
        _addcol(c, "orders", "delivered_at", "INTEGER DEFAULT 0")
        _addcol(c, "sellers", "verified", "INTEGER DEFAULT 0")
        _addcol(c, "sellers", "lvl", "INTEGER DEFAULT 1")
        _addcol(c, "sellers", "verify_asked", "INTEGER DEFAULT 0")
        _addcol(c, "sellers", "review", "INTEGER DEFAULT 0")   # админ вызвал продавца на проверку: товары скрыты
        c.execute("CREATE TABLE IF NOT EXISTS admin_msgs(id INTEGER PRIMARY KEY AUTOINCREMENT, seller_id INTEGER, from_admin INTEGER, text TEXT, created INTEGER, read INTEGER DEFAULT 0)")
        c.execute("CREATE INDEX IF NOT EXISTS ix_adm_msg ON admin_msgs(seller_id)")
        c.execute("CREATE TABLE IF NOT EXISTS seen(user_id INTEGER PRIMARY KEY, ts INTEGER)")   # когда человек последний раз был в приложении
        c.commit()


def user_exists(uid: int) -> bool:
    with closing(db()) as c:
        return c.execute("SELECT 1 FROM users WHERE user_id=?", (uid,)).fetchone() is not None


def ensure_user(uid: int, name: str, username: str, ref: int = 0) -> bool:
    """Создаёт пользователя. Возвращает True, если он новый. ref — id пригласившего (только для новых)."""
    new = not user_exists(uid)
    with closing(db()) as c:
        if new:
            ref_by = ref if ref and ref != uid and user_exists(ref) else 0
            c.execute("INSERT INTO users(user_id,name,username,joined,ref_by,bonus,ref_earned) VALUES(?,?,?,?,?,0,0)",
                      (uid, name or "", username or "", int(time.time()), ref_by))
        else:
            c.execute("UPDATE users SET name=?, username=? WHERE user_id=?", (name or "", username or "", uid))
        c.commit()
    return new


def get_ref_by(uid: int) -> int:
    with closing(db()) as c:
        r = c.execute("SELECT ref_by FROM users WHERE user_id=?", (uid,)).fetchone()
    return int(r[0] or 0) if r else 0


def user_bonus(uid: int) -> int:
    with closing(db()) as c:
        r = c.execute("SELECT bonus FROM users WHERE user_id=?", (uid,)).fetchone()
    return int(r[0] or 0) if r else 0


def is_offer_id(pid: str) -> bool:
    return bool(re.fullmatch(r"o\d{1,9}", pid or ""))


OFFER_COLS = "id,seller_id,game,title,descr,stars,status,created,sold,boost_until"


def offer_get(pid) -> dict | None:
    try:
        oid = int(str(pid).lstrip("o"))
    except ValueError:
        return None
    with closing(db()) as c:
        r = c.execute(f"SELECT {OFFER_COLS} FROM offers WHERE id=?", (oid,)).fetchone()
    return dict(zip(OFFER_COLS.split(","), r)) if r else None


def seller_row(uid: int) -> dict | None:
    with closing(db()) as c:
        r = c.execute("SELECT user_id,about,status,balance,earned,created,verified FROM sellers WHERE user_id=?", (uid,)).fetchone()
    return dict(zip(("user_id", "about", "status", "balance", "earned", "created", "verified"), r)) if r else None


def pack_info(pid: str):
    """(игра, название, цена по умолчанию) для товара магазина или объявления продавца."""
    if is_offer_id(pid):
        o = offer_get(pid)
        return (GAME_BY_ID[o["game"]], o["title"], o["stars"]) if o and o["game"] in GAME_BY_ID else None
    return PACKS.get(pid)


def rating_of(seller_id: int) -> tuple[float, int]:
    with closing(db()) as c:
        r = c.execute("SELECT AVG(rating),COUNT(*) FROM reviews WHERE seller_id=?", (seller_id,)).fetchone()
    return (round(r[0], 1) if r[0] else 0.0), int(r[1])


def seller_level(uid: int) -> dict:
    """Уровень продавца по числу подтверждённых сделок, отзывам и рейтингу: чем выше — тем ниже комиссия и больше объявлений."""
    with closing(db()) as c:
        deals = c.execute("SELECT COUNT(*) FROM orders WHERE seller_id=? AND status='done'", (uid,)).fetchone()[0]
    avg, cnt = rating_of(uid)
    lv = 0
    for i, (_, d, r, a, _, _) in enumerate(SELLER_LEVELS):
        if deals >= d and cnt >= r and (not a or avg >= a):
            lv = i
    with closing(db()) as c:   # уровень, выданный вручную из админ-бота, не ниже заданного
        r = c.execute("SELECT COALESCE(lvl_set,0) FROM sellers WHERE user_id=?", (uid,)).fetchone()
    if r and r[0]:
        lv = max(lv, min(int(r[0]), len(SELLER_LEVELS)) - 1)
    n = SELLER_LEVELS[lv]
    return {"level": lv + 1, "name": n[0], "fee": n[4], "limit": n[5], "deals": deals, "reviews": cnt, "avg": avg,
            "levels": [{"level": i + 1, "name": l[0], "deals": l[1], "reviews": l[2], "avg": l[3], "fee": l[4], "limit": l[5]}
                       for i, l in enumerate(SELLER_LEVELS)]}


def seller_fee(uid: int) -> int:
    return seller_level(uid)["fee"] if uid else 0


async def check_levelup(sid: int):
    """Сообщает продавцу о смене уровня (после сделки или нового отзыва)."""
    L = seller_level(sid)
    with closing(db()) as c:
        old = c.execute("SELECT lvl FROM sellers WHERE user_id=?", (sid,)).fetchone()
        if not old or old[0] == L["level"]:
            return
        c.execute("UPDATE sellers SET lvl=? WHERE user_id=?", (L["level"], sid))
        c.commit()
    if L["level"] > old[0]:
        await notify_seller(sid, f"🎉 <b>Новый уровень: {L['name']}!</b>\nКомиссия магазина теперь <b>{L['fee']}%</b>, лимит объявлений — <b>{L['limit']}</b>.")
    else:
        await notify_seller(sid, f"⚠️ Уровень изменился: <b>{L['name']}</b> (комиссия {L['fee']}%, лимит объявлений {L['limit']}). Рейтинг влияет на уровень.")


def seller_name(uid: int) -> str:
    with closing(db()) as c:
        r = c.execute("SELECT name,username FROM users WHERE user_id=?", (uid,)).fetchone()
    return (r[0] or (("@" + r[1]) if r[1] else "")) if r else ""


def usd_rub() -> float:
    try:
        return float(kv_get("gift_usd_rub", "") or GIFT_USD_RUB)
    except ValueError:
        return GIFT_USD_RUB


def cost_of(pack_id: str) -> float | None:
    with closing(db()) as c:
        r = c.execute("SELECT rub FROM costs WHERE pack=?", (pack_id,)).fetchone()
    if r:
        return float(r[0])
    if pack_id in GIFT_FACE:   # карты: номинал × курс × комиссия поставщика
        return GIFT_FACE[pack_id] * usd_rub() * GIFT_COST_K
    return COST.get(pack_id)


def price_of(pack_id: str) -> int:
    if is_offer_id(pack_id):
        o = offer_get(pack_id)
        return int(o["stars"]) if o else 0
    with closing(db()) as c:
        r = c.execute("SELECT stars FROM prices WHERE pack=?", (pack_id,)).fetchone()
    if r:
        return int(r[0])
    cost = cost_of(pack_id)
    if pack_id in GIFT_FACE and cost:
        return gift_stars_from_cost(cost)
    return stars_from_cost(cost) if cost else PACKS[pack_id][2]


def hidden_keys() -> set:
    with closing(db()) as c:
        return {r[0] for r in c.execute("SELECT key FROM hidden")}


def image_versions() -> dict:
    with closing(db()) as c:
        return dict(c.execute("SELECT key,ver FROM images").fetchall())


def pack_icon(game: dict, pid: str) -> str:
    return "pass" if any(w in pid for w in PASS_WORDS) else game["icon"]


def catalog() -> list:
    hid = hidden_keys()
    imgs = image_versions()
    out = []
    with closing(db()) as c:
        over = dict(c.execute("SELECT pack,stars FROM prices").fetchall())

    def img(key):
        return f"/api/img/{key}?v={imgs[key]}" if key in imgs else ""

    with closing(db()) as c:
        rows = c.execute(f"SELECT {OFFER_COLS} FROM offers o WHERE status='active' AND seller_id IN "
                         "(SELECT user_id FROM sellers WHERE status='approved' AND COALESCE(review,0)=0) ORDER BY (boost_until>?) DESC, boost_at DESC, id DESC",
                         (int(time.time()),)).fetchall()
        offers_all = [dict(zip(OFFER_COLS.split(","), r)) for r in rows]
        deals = dict(c.execute("SELECT seller_id,COUNT(*) FROM orders WHERE status='done' AND seller_id>0 GROUP BY seller_id").fetchall())
    sellers_info = {}
    for o in offers_all:
        sid = o["seller_id"]
        if sid not in sellers_info:
            avg, cnt = rating_of(sid)
            sellers_info[sid] = {"id": sid, "name": seller_name(sid) or f"Продавец {sid}", "avg": avg, "count": cnt,
                                 "deals": int(deals.get(sid, 0)), "verified": bool((seller_row(sid) or {}).get("verified"))}

    for g in GAMES:
        if g["id"] in hid:
            continue
        visible = [(pid, lb, st) for pid, lb, st in g["packs"] if pid not in hid]
        n = len(visible)
        packs = [{"id": pid, "label": lb, "stars": int(over.get(pid, price_of(pid) if pid in GIFT_FACE else st)), "icon": pack_icon(g, pid), "gift": pid in GIFT_FACE,
                  "tier": min(4, 1 + i * 4 // max(n, 1)), "img": img(pid)}
                 for i, (pid, lb, st) in enumerate(visible)]
        offers = [o for o in offers_all if o["game"] == g["id"]]
        if packs or offers:
            out.append({"id": g["id"], "name": g["name"], "emoji": g["emoji"], "color": g["color"],
                        "field": g["field"], "kind": g["kind"], "img": img(g["id"]), "packs": packs, "desc": GIFT_DESC.get(g["id"], ""),
                        "offers": [{"id": f"o{o['id']}", "label": o["title"], "desc": o["descr"], "stars": o["stars"],
                                    "icon": g["icon"], "tier": 3, "img": img(f"o{o['id']}"), "seller": sellers_info[o["seller_id"]],
                                    "ad": o["boost_until"] > time.time()}
                                   for o in offers]})
    return out


ONLINE_SEC = 75
_seen_cache: dict = {}


def touch_seen(uid: int):
    now = int(time.time())
    if now - _seen_cache.get(uid, 0) < 20:
        return
    _seen_cache[uid] = now
    try:
        with closing(db()) as c:
            c.execute("INSERT INTO seen(user_id,ts) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET ts=excluded.ts", (uid, now))
            c.commit()
    except Exception as e:
        logging.debug("seen: %s", e)


def seen_of(uid: int) -> dict:
    with closing(db()) as c:
        r = c.execute("SELECT ts FROM seen WHERE user_id=?", (uid,)).fetchone()
    ts = int(r[0]) if r else 0
    return {"seen": ts, "online": bool(ts) and time.time() - ts < ONLINE_SEC}


def on_review(uid: int) -> bool:
    if not uid:
        return False
    with closing(db()) as c:
        r = c.execute("SELECT COALESCE(review,0) FROM sellers WHERE user_id=?", (uid,)).fetchone()
    return bool(r and r[0])


def pack_available(pack_id: str) -> bool:
    if is_offer_id(pack_id):
        o = offer_get(pack_id)
        sl = seller_row(o["seller_id"]) if o else None
        return bool(o and sl and o["status"] == "active" and sl["status"] == "approved" and not on_review(o["seller_id"])
                    and o["game"] in GAME_BY_ID and o["game"] not in hidden_keys())
    if pack_id not in PACKS:
        return False
    hid = hidden_keys()
    return pack_id not in hid and PACKS[pack_id][0]["id"] not in hid


ORDER_COLS = "id,user_id,pack,game,label,player,comment,stars,status,charge_id,created,paid_at,done_at,promo,base,bonus,cashback,seller_id,delivered_at"


def row_to_order(r) -> dict:
    return dict(zip(ORDER_COLS.split(","), r))


def get_order(oid: int) -> dict | None:
    with closing(db()) as c:
        r = c.execute(f"SELECT {ORDER_COLS} FROM orders WHERE id=?", (oid,)).fetchone()
    return row_to_order(r) if r else None


def user_orders(user_id: int, limit: int = 20) -> list:
    with closing(db()) as c:
        rows = c.execute(
            f"SELECT {ORDER_COLS} FROM orders WHERE user_id=? AND status!='expired' ORDER BY id DESC LIMIT ?",
            (user_id, limit)).fetchall()
    return [row_to_order(r) for r in rows]


def kv_get(k: str, default: str = "") -> str:
    try:
        with closing(db()) as c:
            r = c.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return r[0] if r and r[0] is not None else default
    except sqlite3.Error:
        return default


def sales_paused() -> str:
    """Текст для покупателей, если продажи приостановлены из админ-бота; пустая строка — продажи идут."""
    if kv_get("paused") == "1":
        return kv_get("pause_msg") or "⏸ Продажи временно приостановлены. Скоро вернёмся!"
    return ""


# ---- Промокоды ----
def promo_check(uid: int, code: str) -> tuple[bool, int, str]:
    code = code.strip().upper()
    with closing(db()) as c:
        p = c.execute("SELECT pct,max_uses,used,expires,COALESCE(paused,0) FROM promos WHERE code=?", (code,)).fetchone()
        if not p:
            return False, 0, "Промокод не найден"
        pct, max_uses, used, expires, paused = p
        if paused:
            return False, 0, "Промокод временно отключён"
        if expires and expires < time.time():
            return False, 0, "Срок промокода истёк"
        if max_uses and used >= max_uses:
            return False, 0, "Промокод закончился"
        if c.execute("SELECT 1 FROM promo_uses WHERE code=? AND user_id=?", (code, uid)).fetchone():
            return False, 0, "Ты уже использовал этот промокод"
    return True, int(pct), f"Промокод применён: −{pct}%"


def promo_scope(code: str, pack_id: str) -> tuple[bool, str]:
    """Промокод продавца работает только на его товарах, общий — только на товарах магазина."""
    with closing(db()) as c:
        r = c.execute("SELECT seller_id FROM promos WHERE code=?", (code,)).fetchone()
    sid = int(r[0] or 0) if r else 0
    o = offer_get(pack_id) if is_offer_id(pack_id) else None
    pack_seller = o["seller_id"] if o else 0
    if sid == pack_seller:
        return True, ""
    return False, ("Этот промокод действует только на товары его продавца" if sid else "Этот промокод не действует на товары продавцов")


def calc(uid: int, pack_id: str, promo: str = "", use_bonus: bool = True) -> dict:
    base = price_of(pack_id)
    after, pct, code, msg, promo_ok, scope_bad = base, 0, "", "", True, False
    if pack_id in GIFT_FACE:   # подарочные карты: тонкая наценка — без промокодов, баллов и кэшбэка
        return {"base": base, "after": base, "pct": 0, "promo": "", "promo_ok": not promo.strip(),
                "promo_msg": "Промокоды не действуют на подарочные карты" if promo.strip() else "", "promo_scope": bool(promo.strip()),
                "balance": user_bonus(uid), "bonus": 0, "star_off": 0, "stars": base, "cashback": 0, "cashback_blocked": False, "gift": True}
    if promo.strip():
        promo_ok, pct, msg = promo_check(uid, promo)
        if promo_ok:
            s_ok, s_msg = promo_scope(promo.strip().upper(), pack_id)
            if s_ok:
                code = promo.strip().upper()
                after = max(1, round(base * (100 - pct) / 100))
            else:
                promo_ok, pct, msg, scope_bad = False, 0, s_msg, True
        else:
            pct = 0
    if is_offer_id(pack_id):   # товары продавцов: без баллов и кэшбэка (комиссия магазина небольшая)
        return {"base": base, "after": after, "pct": pct, "promo": code, "promo_ok": promo_ok, "promo_msg": msg,
                "promo_scope": scope_bad, "balance": user_bonus(uid), "bonus": 0, "star_off": 0, "stars": after,
                "cashback": 0, "cashback_blocked": False, "offer": True}
    balance = user_bonus(uid)   # баллы: 1 балл = 1 ₽
    star_off = max(0, min(int(balance // STAR_RUB), after - 1)) if use_bonus else 0
    bonus = int(math.ceil(star_off * STAR_RUB)) if star_off else 0   # списано баллов
    stars = after - star_off
    cb = cashback_for(stars, code, uid)
    return {"base": base, "after": after, "pct": pct, "promo": code, "promo_ok": promo_ok, "promo_msg": msg,
            "promo_scope": scope_bad, "balance": balance, "bonus": bonus, "star_off": star_off, "stars": stars, "cashback": cb,
            "cashback_blocked": bool(code) and stars >= CASHBACK_MIN, "cb_pct": cb_level(uid)["pct"]}


def cb_level(uid: int, exclude: int = 0) -> dict:
    """Уровень кэшбэка по сумме выполненных заказов (⭐): 1 ур. — 1%, 2 ур. — 3%, 3 ур. — 5%."""
    spent = 0
    if uid:
        with closing(db()) as c:
            spent = c.execute("SELECT COALESCE(SUM(stars),0) FROM orders WHERE user_id=? AND status='done' AND id!=?", (uid, exclude)).fetchone()[0]
    lvl = 0
    for i, (frm, _) in enumerate(CB_LEVELS):
        if spent >= frm:
            lvl = i
    nxt = CB_LEVELS[lvl + 1] if lvl + 1 < len(CB_LEVELS) else None
    return {"level": lvl + 1, "pct": CB_LEVELS[lvl][1], "spent": spent, "next_from": nxt[0] if nxt else 0,
            "next_pct": nxt[1] if nxt else 0, "levels": [{"level": i + 1, "from": f, "pct": p} for i, (f, p) in enumerate(CB_LEVELS)]}


def cashback_for(stars: int, promo: str, uid: int = 0, exclude: int = 0) -> int:
    """Кэшбэк в баллах: только от CASHBACK_MIN оплаченных ⭐, без промокода; процент зависит от уровня покупателя."""
    if promo or stars < CASHBACK_MIN:
        return 0
    return int(stars * cb_level(uid, exclude)["pct"] / 100 * STAR_RUB)


def credit_cashback(o: dict) -> int:
    if o["pack"] in GIFT_FACE:
        return 0
    cb = cashback_for(o["stars"], o["promo"], o["user_id"], o["id"])
    if cb >= 1:
        with closing(db()) as c:
            c.execute("UPDATE users SET bonus=bonus+? WHERE user_id=?", (cb, o["user_id"]))
            c.execute("UPDATE orders SET cashback=? WHERE id=?", (cb, o["id"]))
            c.commit()
    return cb


def create_order(user_id: int, pack_id: str, player: str, comment: str, q: dict) -> int:
    g, label, _ = pack_info(pack_id)
    seller_id = offer_get(pack_id)["seller_id"] if is_offer_id(pack_id) else 0
    with closing(db()) as c:
        # старые неоплаченные счета больше не действуют
        c.execute("UPDATE orders SET status='expired' WHERE user_id=? AND status='new'", (user_id,))
        cur = c.execute(
            "INSERT INTO orders(user_id,pack,game,label,player,comment,stars,status,created,promo,base,bonus,seller_id) "
            "VALUES(?,?,?,?,?,?,?,'new',?,?,?,?,?)",
            (user_id, pack_id, g["name"], label, player, comment, q["stars"], int(time.time()),
             q["promo"], q["base"], q["bonus"], seller_id))
        c.commit()
        return cur.lastrowid


def mark_paid(oid: int, charge_id: str) -> bool:
    with closing(db()) as c:
        cur = c.execute("UPDATE orders SET status='paid', charge_id=?, paid_at=? WHERE id=? AND status='new'",
                        (charge_id, int(time.time()), oid))
        if cur.rowcount != 1:
            return False
        uid, promo, bonus = c.execute("SELECT user_id,promo,bonus FROM orders WHERE id=?", (oid,)).fetchone()
        if promo:   # промокод тратится только после оплаты
            ins = c.execute("INSERT OR IGNORE INTO promo_uses(code,user_id,order_id) VALUES(?,?,?)", (promo, uid, oid))
            if ins.rowcount:
                c.execute("UPDATE promos SET used=used+1 WHERE code=?", (promo,))
        if bonus:   # списываем бонусы
            c.execute("UPDATE users SET bonus=MAX(0,bonus-?) WHERE user_id=?", (bonus, uid))
        c.commit()
        return True


def set_status(oid: int, status: str, from_status="paid") -> bool:
    frm = (from_status,) if isinstance(from_status, str) else tuple(from_status)
    with closing(db()) as c:
        cur = c.execute(f"UPDATE orders SET status=?, done_at=? WHERE id=? AND status IN ({','.join('?' * len(frm))})",
                        (status, int(time.time()), oid, *frm))
        c.commit()
        return cur.rowcount == 1


def release_refund(o: dict):
    """При возврате: бонусы возвращаются на баланс, промокод освобождается."""
    with closing(db()) as c:
        if o["bonus"]:
            c.execute("UPDATE users SET bonus=bonus+? WHERE user_id=?", (o["bonus"], o["user_id"]))
        if o["promo"]:
            d = c.execute("DELETE FROM promo_uses WHERE code=? AND user_id=? AND order_id=?",
                          (o["promo"], o["user_id"], o["id"]))
            if d.rowcount:
                c.execute("UPDATE promos SET used=MAX(0,used-1) WHERE code=?", (o["promo"],))
        c.commit()


def credit_referrer(o: dict) -> tuple[int, int]:
    """Начисляет бонус пригласившему за выполненный заказ друга. Возвращает (ref_id, сумма)."""
    ref = get_ref_by(o["user_id"])
    reward = int(o["stars"] * REF_PERCENT / 100 * STAR_RUB) if ref and REF_PERCENT > 0 else 0
    if ref and reward >= 1:
        with closing(db()) as c:
            c.execute("UPDATE users SET bonus=bonus+?, ref_earned=ref_earned+? WHERE user_id=?", (reward, reward, ref))
            c.commit()
        return ref, reward
    return 0, 0


def credit_seller(o: dict) -> int:
    """Продавцу — цена минус комиссия магазина."""
    if not o["seller_id"]:
        return 0
    gain = o["stars"] * (100 - seller_fee(o["seller_id"])) // 100
    with closing(db()) as c:
        c.execute("UPDATE sellers SET balance=balance+?, earned=earned+? WHERE user_id=?", (gain, gain, o["seller_id"]))
        if is_offer_id(o["pack"]):
            c.execute("UPDATE offers SET sold=sold+1 WHERE id=?", (int(o["pack"][1:]),))
        c.commit()
    return gain


def expire_old_orders() -> list:
    """Неоплаченные заказы старше ORDER_TTL_MIN минут отменяются. Возвращает [(user_id, order_id, название)]."""
    limit = int(time.time()) - ORDER_TTL_MIN * 60
    with closing(db()) as c:
        rows = c.execute("SELECT id,user_id,label FROM orders WHERE status='new' AND created<?", (limit,)).fetchall()
        if rows:
            c.executemany("UPDATE orders SET status='expired' WHERE id=? AND status='new'", [(r[0],) for r in rows])
            c.commit()
    return [(r[1], r[0], r[2]) for r in rows]


def profile_data(uid: int) -> dict:
    with closing(db()) as c:
        u = c.execute("SELECT name,username,bonus,ref_earned,joined FROM users WHERE user_id=?", (uid,)).fetchone()
        done = c.execute("SELECT COUNT(*), COALESCE(SUM(stars),0) FROM orders WHERE user_id=? AND status IN ('paid','delivered','dispute','done')",
                         (uid,)).fetchone()
        invited = c.execute("SELECT COUNT(*) FROM users WHERE ref_by=?", (uid,)).fetchone()[0]
        buyers = c.execute("SELECT COUNT(DISTINCT o.user_id) FROM orders o JOIN users u ON u.user_id=o.user_id "
                           "WHERE u.ref_by=? AND o.status IN ('paid','delivered','dispute','done')", (uid,)).fetchone()[0]
    name, username, bonus, earned, joined = u if u else ("", "", 0, 0, 0)
    link = f"https://t.me/{BOT_USERNAME}?start=ref{uid}" if BOT_USERNAME else ""
    return {"id": uid, "name": name, "username": username, "orders": done[0], "spent": done[1],
            "bonus": bonus, "earned": earned, "invited": invited, "buyers": buyers,
            "ref_percent": REF_PERCENT, "ref_link": link, "joined": fmt_time(joined), "cb": cb_level(uid),
            "av": av_url(uid), "av_custom": os.path.exists(_av_custom(uid))}


# ==========================================
# 🧰 ПРОЧЕЕ
# ==========================================
def is_admin(uid: int) -> bool:
    return uid == ADMIN_ID


def webapp_link() -> str:
    if not WEBAPP_URL:
        return ""
    sep = "&" if "?" in WEBAPP_URL else "?"
    return WEBAPP_URL + sep + urlencode({"api": API_URL}) if API_URL else WEBAPP_URL


def support_url(src: str) -> str:
    return f"https://t.me/{SUPPORT.lstrip('@')}?start={src}"


def open_kb(uid: int = 0) -> InlineKeyboardMarkup | None:
    url = webapp_link()
    if not url:
        return None
    rows = [[InlineKeyboardButton(text="🛒 Открыть магазин", web_app=WebAppInfo(url=url))]]
    if uid and BOT_USERNAME:
        link = f"https://t.me/{BOT_USERNAME}?start=ref{uid}"
        share = "https://t.me/share/url?" + urlencode({"url": link, "text": f"Пополнение игр за Telegram Stars — {SHOP_NAME}"},
                                                      quote_via=quote)
        rows.append([InlineKeyboardButton(text="👥 Пригласить друга", url=share)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def valid_player(kind: str, value: str) -> bool:
    v = value.strip()
    if kind == "gift":   # e-mail или @username в Telegram
        return bool(re.fullmatch(r"[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}", v) or re.fullmatch(r"@?[A-Za-z][A-Za-z0-9_]{4,31}", v))
    if kind == "numeric":
        return bool(re.fullmatch(r"\d{5,15}", v))
    return bool(re.fullmatch(r"#?[0-9A-Za-z]{3,14}", v) or re.fullmatch(r"[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}", v))


def fmt_time(ts) -> str:
    return time.strftime("%d.%m.%Y %H:%M", time.localtime(ts)) if ts else "—"


def order_line(o: dict) -> str:
    return (f"#{o['id']} · {escape(o['game'])} — {escape(o['label'])} · {o['stars']}⭐ · "
            f"{STATUS_TXT.get(o['status'], o['status'])}")


def admin_order_text(o: dict, user_info: str = "") -> str:
    t = (f"🛒 <b>Заказ #{o['id']}</b>\n"
         f"Игра: <b>{escape(o['game'])}</b>\nТовар: <b>{escape(o['label'])}</b>\n"
         f"{'Получатель кода' if o['pack'].startswith('gc_') else 'Игрок'}: <code>{escape(o['player'])}</code>\n")
    if o["comment"]:
        t += f"Комментарий: {escape(o['comment'])}\n"
    t += f"Сумма: {o['stars']}⭐"
    extra = []
    if o["promo"]:
        extra.append(f"промокод {escape(o['promo'])}")
    if o["bonus"]:
        extra.append(f"баллами {o['bonus']}")
    if extra:
        t += f" (цена {o['base']}⭐, " + ", ".join(extra) + ")"
    t += f"\nСтатус: {STATUS_TXT.get(o['status'], o['status'])}\n"
    if o.get("seller_id"):
        t += (f"Продавец: <a href=\"tg://user?id={o['seller_id']}\">{escape(seller_name(o['seller_id']))}</a> "
              f"({o['seller_id']}) · ему {o['stars'] * (100 - seller_fee(o['seller_id'])) // 100}⭐ после подтверждения\n")
    t += f"Клиент: <a href=\"tg://user?id={o['user_id']}\">{o['user_id']}</a> {user_info}\n"
    if o["pack"].startswith("gc_") and o["status"] in ("paid", "delivered"):
        t += f"🎁 Код карты отправь клиенту: <code>/say {o['user_id']} Ваш код: …</code>, затем нажми «Выполнено».\n"
    t += f"Создан: {fmt_time(o['created'])} · оплачен: {fmt_time(o['paid_at'])}"
    return t


def admin_kb(oid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Выполнено", callback_data=f"done:{oid}"),
        InlineKeyboardButton(text="❌ Отказ + возврат", callback_data=f"cancel:{oid}"),
    ]])


def seller_order_kb(oid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📦 Передал товар", callback_data=f"dlv:{oid}"),
        InlineKeyboardButton(text="❌ Отказ + возврат", callback_data=f"cancel:{oid}"),
    ]])


def buyer_kb(oid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Всё получил", callback_data=f"bok:{oid}"),
        InlineKeyboardButton(text="⚠️ Проблема", callback_data=f"bbad:{oid}"),
    ]])


def user_info(uid: int) -> str:
    with closing(db()) as c:
        r = c.execute("SELECT name,username FROM users WHERE user_id=?", (uid,)).fetchone()
    if not r:
        return ""
    return escape(r[0] or "") + (f" @{escape(r[1])}" if r[1] else "")


async def safe_send(uid: int, text: str):
    try:
        await bot.send_message(uid, text)
    except Exception as e:
        logging.warning("send_message %s: %s", uid, e)


async def welcome_referrer(ref: int, name: str):
    await safe_send(ref, f"👥 По твоей ссылке пришёл <b>{escape(name or 'новый пользователь')}</b>!\n"
                         f"Ты будешь получать {REF_PERCENT}% бонусами с каждого его выполненного заказа.")


def parse_ref(arg: str) -> int:
    m = re.fullmatch(r"ref(\d{1,15})", (arg or "").strip())
    return int(m.group(1)) if m else 0


# ==========================================
# 💬 КОМАНДЫ БОТА
# ==========================================
@dp.message(CommandStart())
async def start(message: Message, command: CommandObject):
    u = message.from_user
    ref = parse_ref(command.args or "")
    if ensure_user(u.id, u.first_name, u.username, ref) and get_ref_by(u.id):
        asyncio.create_task(welcome_referrer(get_ref_by(u.id), u.first_name))
    text = (f"👋 Привет, {escape(u.first_name or '')}!\n\n"
            f"<b>{SHOP_NAME}</b> — пополнение игровых аккаунтов:\n"
            + "\n".join(f"{g['emoji']} {g['name']}" for g in GAMES)
            + "\n\nВыбери игру и товар в магазине, оплати Telegram Stars — админ пополнит аккаунт и пришлёт уведомление.\n"
              f"🎁 Приглашай друзей и получай {REF_PERCENT}% баллами с их заказов.\n"
              f"💸 Кэшбэк 1% → 3% → 5% баллами (уровень растёт с покупками) с заказов от {CASHBACK_MIN}⭐ (1 балл = 1 ₽, без промокода).\n"
              f"Вопросы: {escape(SUPPORT)}")
    kb = open_kb(u.id)
    await message.answer(text, reply_markup=kb)
    if not kb:
        await message.answer("⚠️ Магазин ещё не настроен (WEBAPP_URL).")


@dp.message(Command("orders"))
async def cmd_orders(message: Message):
    orders = user_orders(message.from_user.id, 10)
    if not orders:
        await message.answer("У тебя пока нет заказов.", reply_markup=open_kb(message.from_user.id))
        return
    await message.answer("📦 <b>Твои заказы</b>\n\n" + "\n".join(order_line(o) for o in orders))


@dp.message(Command("ref"))
async def cmd_ref(message: Message):
    u = message.from_user
    ensure_user(u.id, u.first_name, u.username)
    p = profile_data(u.id)
    await message.answer(
        f"👥 <b>Реферальная программа</b>\n\nПолучай <b>{REF_PERCENT}%</b> бонусами с каждого выполненного заказа друга. "
        f"Бонусами можно оплатить часть следующего заказа.\n\n"
        f"Твоя ссылка:\n<code>{escape(p['ref_link'])}</code>\n\n"
        f"Приглашено: {p['invited']} · купили: {p['buyers']}\nЗаработано: {p['earned']} баллов · на балансе: {p['bonus']} баллов",
        reply_markup=open_kb(u.id))


@dp.message(Command("level", "cashback"))
async def cmd_level(message: Message):
    u = message.from_user
    ensure_user(u.id, u.first_name, u.username)
    L = cb_level(u.id)
    n = len(L["levels"])
    done = max(0, min(10, round(10 * L["spent"] / L["next_from"]))) if L["next_from"] else 10
    bar = "🟩" * done + "⬜" * (10 - done)
    rows = "\n".join(f"{'✅' if x['level'] <= L['level'] else '▫️'} {x['level']} уровень — <b>{x['pct']}%</b> (от {x['from']}⭐ выполненных заказов)" for x in L["levels"])
    tail = (f"До {L['level'] + 1} уровня ({L['next_pct']}%) осталось <b>{L['next_from'] - L['spent']}⭐</b>" if L["next_from"] else "🏆 Максимальный уровень!")
    await message.answer(f"💸 <b>Твой кэшбэк: уровень {L['level']} из {n} — {L['pct']}%</b>\n{bar}\nПотрачено: {L['spent']}⭐\n\n{rows}\n\n{tail}\n"
                         f"Кэшбэк идёт баллами (1 балл = 1 ₽) с заказов от {CASHBACK_MIN}⭐, без промокода.", reply_markup=open_kb(u.id))


@dp.message(Command("support"))
async def cmd_support(message: Message):
    await message.answer("🆘 По любому вопросу — наша поддержка. Нажми кнопку, и мы поможем:",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💬 Написать в поддержку", url=support_url("shop"))]]))


# ---- Админ ----
@dp.message(Command("admin"))
async def admin_panel(message: Message):
    if not is_admin(message.from_user.id):
        return
    with closing(db()) as c:
        users = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        paid = c.execute("SELECT COUNT(*), COALESCE(SUM(stars),0) FROM orders WHERE status IN ('paid','delivered','dispute','done')").fetchone()
        wait = c.execute("SELECT COUNT(*) FROM orders WHERE status IN ('paid','delivered','dispute')").fetchone()[0]
    await message.answer(
        "🛠 <b>Админ-панель</b>\n"
        f"Пользователей: {users}\nОплаченных заказов: {paid[0]} на {paid[1]}⭐\nЖдут выполнения: {wait}\n\n"
        "/pending — заказы к выполнению\n/order ID — карточка заказа\n/prices — список цен и ID\n/setrate 95 — курс $ для подарочных карт\n"
        "/setprice ID ЗВЁЗДЫ — поменять цену вручную\n/setcost ID РУБЛИ — себестоимость (цена = себест./курс + 50..100⭐)\n/reprice — сбросить ручные цены\n/hide ID, /show ID — скрыть/показать товар или игру\n"
        "/say ID текст — написать клиенту\n/broadcast текст — рассылка всем\n\n"
        "🎟 Промокоды:\n/addpromo КОД ПРОЦЕНТ [макс_использований] [дней] — создать\n/promos — список\n/delpromo КОД — удалить\n\n"
        "🖼 Фото: отправь картинку боту с подписью <code>/setimg ID</code> (ID игры или товара из /prices; "
        "<code>/setimg bs*</code> — на все товары игры сразу). Или по ссылке: <code>/imgurl ID https://…/картинка.png</code>, сразу много: /imgbulk. "
        "Убрать фото: /delimg ID\n\n"
        f"🧑‍💼 Продавцы: /sellers, /banseller USER_ID (заявки, товары и выводы приходят с кнопками). Комиссия по уровням: {', '.join(str(l[4]) + '%' for l in SELLER_LEVELS)}, оплата — {ORDER_TTL_MIN} мин.\n\n"
        "🎁 Баллы вручную: /bonus USER_ID БАЛЛЫ (1 балл = 1 ₽)\n\n"
        f"💸 Кэшбэк по уровням: {' / '.join(f'{p}% от {f}⭐' for f, p in CB_LEVELS)} потраченных, от оплаты ≥{CASHBACK_MIN}⭐, без промокода (после «Выполнено»). Курс: 1⭐ = {STAR_RUB:g} ₽ (STAR_RUB)")


@dp.message(Command("pending"))
async def admin_pending(message: Message):
    if not is_admin(message.from_user.id):
        return
    with closing(db()) as c:
        rows = c.execute(f"SELECT {ORDER_COLS} FROM orders WHERE status IN ('paid','delivered','dispute') ORDER BY id").fetchall()
    if not rows:
        await message.answer("Нет заказов, ожидающих выполнения 🎉")
        return
    for r in rows:
        o = row_to_order(r)
        await message.answer(admin_order_text(o, user_info(o["user_id"])), reply_markup=admin_kb(o["id"]))


@dp.message(Command("order"))
async def admin_order(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    if not (command.args or "").strip().isdigit():
        await message.answer("Формат: /order 12")
        return
    o = get_order(int(command.args))
    if not o:
        await message.answer("Заказ не найден")
        return
    await message.answer(admin_order_text(o, user_info(o["user_id"])),
                         reply_markup=admin_kb(o["id"]) if o["status"] == "paid" else None)


@dp.message(Command("prices"))
async def admin_prices(message: Message):
    if not is_admin(message.from_user.id):
        return
    hid = hidden_keys()
    imgs = image_versions()
    lines = []
    for g in GAMES:
        lines.append(f"\n<b>{g['name']}</b> (<code>{g['id']}</code>)" + (" — скрыта" if g["id"] in hid else "")
                     + (" 🖼" if g["id"] in imgs else ""))
        for pid, label, _ in g["packs"]:
            pr, cost = price_of(pid), cost_of(pid)
            prof = f" · себест. {cost:g}₽, прибыль ≈{pr - cost / STAR_RUB:.0f}⭐" if cost else ""
            lines.append(f"<code>{pid}</code> {escape(label)} — {pr}⭐{prof}" + (" (скрыт)" if pid in hid else "")
                         + (" 🖼" if pid in imgs else ""))
    await message.answer("\n".join(lines))


@dp.message(Command("setcost"))
async def admin_setcost(message: Message, command: CommandObject):
    """/setcost ID РУБЛИ — своя себестоимость, цена в ⭐ пересчитается (с наценкой 50..100⭐)."""
    if not is_admin(message.from_user.id):
        return
    a = (command.args or "").replace(",", ".").split()
    try:
        rub = float(a[1])
    except (IndexError, ValueError):
        rub = 0
    if len(a) != 2 or a[0] not in PACKS or rub <= 0:
        await message.answer("Формат: /setcost bs_g80 560  (себестоимость в ₽, ID смотри в /prices)")
        return
    with closing(db()) as c:
        c.execute("INSERT OR REPLACE INTO costs(pack,rub) VALUES(?,?)", (a[0], rub))
        c.execute("DELETE FROM prices WHERE pack=?", (a[0],))
        c.commit()
    await message.answer(f"✅ {a[0]}: себестоимость {rub:g}₽ → цена {price_of(a[0])}⭐")


@dp.message(Command("reprice"))
async def admin_reprice(message: Message):
    """Сбросить ручные цены (/setprice) — вернуться к автоцене от себестоимости."""
    if not is_admin(message.from_user.id):
        return
    with closing(db()) as c:
        c.execute("DELETE FROM prices")
        c.commit()
    await message.answer("✅ Ручные цены сброшены, цены пересчитаны от себестоимости. Смотри /prices")


@dp.message(Command("setrate"))
async def admin_setrate(message: Message, command: CommandObject):
    """/setrate 95 — курс доллара для подарочных карт: цены в ⭐ пересчитываются сразу."""
    if not is_admin(message.from_user.id):
        return
    a = (command.args or "").replace(",", ".").split()
    if not a:
        await message.answer(f"Текущий курс: <b>{usd_rub():g} ₽/$</b>\nИзменить: /setrate 95\n"
                             f"Закупка = номинал × курс × {GIFT_COST_K:g}, наценка {GIFT_MARGIN_PCT:g}% (не меньше {GIFT_MARGIN_MIN}⭐).")
        return
    try:
        rate = float(a[0])
        assert 20 <= rate <= 500
    except (ValueError, AssertionError):
        await message.answer("Курс должен быть числом от 20 до 500, например /setrate 95")
        return
    with closing(db()) as c:
        c.execute("INSERT OR REPLACE INTO kv(k,v) VALUES('gift_usd_rub',?)", (str(rate),))
        c.commit()
    ex = ", ".join(f"${n} = {price_of(f'gc_apple_{n}')}⭐" for n in (10, 25, 50))
    await message.answer(f"✅ Курс {rate:g} ₽/$. Apple: {ex}\nЦены карт обновились. Ручные цены (/setprice) не менялись.")


@dp.message(Command("setprice"))
async def admin_setprice(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    a = (command.args or "").split()
    if len(a) != 2 or a[0] not in PACKS or not a[1].isdigit() or int(a[1]) < 1:
        await message.answer("Формат: /setprice bs_g30 200  (ID смотри в /prices)")
        return
    with closing(db()) as c:
        c.execute("INSERT OR REPLACE INTO prices(pack,stars) VALUES(?,?)", (a[0], int(a[1])))
        c.commit()
    await message.answer(f"✅ {a[0]} теперь {a[1]}⭐")


@dp.message(Command("hide", "show"))
async def admin_hide(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    key = (command.args or "").strip()
    if key not in PACKS and key not in GAME_BY_ID:
        await message.answer("Формат: /hide ID или /show ID (ID из /prices)")
        return
    with closing(db()) as c:
        if command.command == "hide":
            c.execute("INSERT OR IGNORE INTO hidden(key) VALUES(?)", (key,))
        else:
            c.execute("DELETE FROM hidden WHERE key=?", (key,))
        c.commit()
    await message.answer("✅ Готово")


@dp.message(Command("say"))
async def admin_say(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    parts = (command.args or "").split(maxsplit=1)
    if len(parts) != 2 or not parts[0].isdigit():
        await message.answer("Формат: /say USER_ID текст")
        return
    try:
        await bot.send_message(int(parts[0]), f"💬 <b>{SHOP_NAME}:</b> {escape(parts[1])}")
        await message.answer("✅ Отправлено")
    except Exception as e:
        await message.answer(f"Не отправилось: {escape(str(e))}")


@dp.message(Command("broadcast"))
async def admin_broadcast(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    text = (command.args or "").strip()
    if not text:
        await message.answer("Формат: /broadcast текст")
        return
    with closing(db()) as c:
        ids = [r[0] for r in c.execute("SELECT user_id FROM users")]
    ok = 0
    for uid in ids:
        try:
            await bot.send_message(uid, text, reply_markup=open_kb())
            ok += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)
    await message.answer(f"📣 Доставлено: {ok} из {len(ids)}")


@dp.message(Command("bonus"))
async def admin_bonus(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    a = (command.args or "").split()
    if len(a) != 2 or not a[0].isdigit() or not re.fullmatch(r"-?\d+", a[1]) or not user_exists(int(a[0])):
        await message.answer("Формат: /bonus USER_ID БАЛЛЫ (можно отрицательные)")
        return
    with closing(db()) as c:
        c.execute("UPDATE users SET bonus=MAX(0,bonus+?) WHERE user_id=?", (int(a[1]), int(a[0])))
        c.commit()
    await message.answer(f"✅ Баланс {a[0]}: {user_bonus(int(a[0]))} баллов")


# ---- Промокоды ----
@dp.message(Command("addpromo"))
async def admin_addpromo(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    a = (command.args or "").split()
    if len(a) < 2 or not re.fullmatch(r"[A-Za-z0-9_-]{2,24}", a[0]) or not a[1].isdigit() or not 1 <= int(a[1]) <= 90 \
            or any(not x.isdigit() for x in a[2:4]) or len(a) > 4:
        await message.answer("Формат: /addpromo КОД ПРОЦЕНТ [макс_использований] [дней]\n"
                             "Пример: /addpromo START10 10 100 30 — −10%, 100 раз, 30 дней (проценты 1–90)")
        return
    code, pct = a[0].upper(), int(a[1])
    max_uses = int(a[2]) if len(a) > 2 else 0
    expires = int(time.time()) + int(a[3]) * 86400 if len(a) > 3 and int(a[3]) > 0 else 0
    with closing(db()) as c:
        c.execute("INSERT OR REPLACE INTO promos(code,pct,max_uses,used,expires) VALUES(?,?,?,COALESCE((SELECT used FROM promos WHERE code=?),0),?)",
                  (code, pct, max_uses, code, expires))
        c.commit()
    await message.answer(f"✅ Промокод <code>{code}</code>: −{pct}%, "
                         f"{'без лимита' if not max_uses else f'{max_uses} раз'}, "
                         f"{'бессрочно' if not expires else 'до ' + fmt_time(expires)}")


@dp.message(Command("promos"))
async def admin_promos(message: Message):
    if not is_admin(message.from_user.id):
        return
    with closing(db()) as c:
        rows = c.execute("SELECT code,pct,max_uses,used,expires FROM promos ORDER BY code").fetchall()
    if not rows:
        await message.answer("Промокодов нет. Создай: /addpromo START10 10")
        return
    await message.answer("🎟 <b>Промокоды</b>\n\n" + "\n".join(
        f"<code>{r[0]}</code> −{r[1]}% · {r[3]}/{r[2] or '∞'}"
        + (f" · до {fmt_time(r[4])}" if r[4] else "") for r in rows))


@dp.message(Command("delpromo"))
async def admin_delpromo(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    code = (command.args or "").strip().upper()
    with closing(db()) as c:
        d = c.execute("DELETE FROM promos WHERE code=?", (code,))
        c.commit()
    await message.answer("🗑 Удалён" if d.rowcount else "Такого промокода нет")


# ---- Фото товаров: админ шлёт картинку с подписью /setimg ID ----
def img_keys(key: str) -> list:
    """ID игры/товара; 'bs*' = все товары игры."""
    if key.endswith("*") and key[:-1] in GAME_BY_ID:
        return [p[0] for p in GAME_BY_ID[key[:-1]]["packs"]]
    return [key] if (key in PACKS or key in GAME_BY_ID) else []


def save_img_record(keys: list, src_path: str):
    import shutil
    with closing(db()) as c:
        for k in keys:
            dst = os.path.join(IMG_DIR, f"{k}.jpg")
            if os.path.abspath(dst) != os.path.abspath(src_path):
                shutil.copyfile(src_path, dst)
            c.execute("INSERT OR REPLACE INTO images(key,ver) VALUES(?,?)", (k, int(time.time())))
        c.commit()


@dp.message(F.photo, F.caption.regexp(r"^/setimg(@\w+)?\s+[\w*]+"))
async def admin_setimg(message: Message):
    if not is_admin(message.from_user.id):
        return
    key = message.caption.split()[1]
    keys = img_keys(key)
    if not keys:
        await message.answer("Не знаю такой ID. Список — /prices")
        return
    tmp = os.path.join(IMG_DIR, "_tmp.jpg")
    try:
        await bot.download(message.photo[-1], destination=tmp)
        save_img_record(keys, tmp)
    except Exception as e:
        await message.answer(f"Не удалось сохранить фото: {escape(str(e))}")
        return
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    await message.answer(f"🖼 Фото сохранено ({len(keys)} шт.). Обнови магазин, чтобы увидеть.")


async def _fetch_img(url: str) -> bytes:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25)) as ses:
        async with ses.get(url, headers={"User-Agent": "Mozilla/5.0"}) as r:
            data = await r.read()
            if r.status != 200 or not r.headers.get("Content-Type", "").startswith("image/") or len(data) > 6_000_000:
                raise ValueError(f"HTTP {r.status}, {r.headers.get('Content-Type')} — нужна прямая ссылка на картинку")
            return data


@dp.message(Command("imgbulk"))
async def admin_imgbulk(message: Message, command: CommandObject):
    """Много фото за раз: после команды, каждая строка — «ID ССЫЛКА». Например:
    /imgbulk
    bs* https://…/gems.png
    cr* https://…/royale.png"""
    if not is_admin(message.from_user.id):
        return
    lines = [l.split() for l in (command.args or "").splitlines() if l.strip()]
    if not lines:
        await message.answer("Формат (по строке на фото):\n<code>/imgbulk\nbs* https://…/картинка.png\ncr* https://…/картинка.png</code>")
        return
    ok, bad = 0, []
    tmp = os.path.join(IMG_DIR, "_tmp.jpg")
    for parts in lines:
        keys = img_keys(parts[0]) if len(parts) == 2 else []
        if not keys or not parts[1].startswith("http"):
            bad.append(f"{parts[0]}: неверная строка")
            continue
        try:
            with open(tmp, "wb") as f:
                f.write(await _fetch_img(parts[1]))
            save_img_record(keys, tmp)
            ok += 1
        except Exception as e:
            bad.append(f"{parts[0]}: {escape(str(e))[:80]}")
    try:
        os.remove(tmp)
    except OSError:
        pass
    await message.answer(f"🖼 Загружено: {ok}" + ("\nОшибки:\n" + "\n".join(bad) if bad else "") + "\nОбнови магазин.")


@dp.message(Command("imgurl"))
async def admin_imgurl(message: Message, command: CommandObject):
    """/imgurl ID https://ссылка-на-картинку — сервер сам скачает картинку."""
    if not is_admin(message.from_user.id):
        return
    a = (command.args or "").split()
    keys = img_keys(a[0]) if len(a) == 2 else []
    if not keys or not a[1].startswith("http"):
        await message.answer("Формат: /imgurl ID ССЫЛКА (ID из /prices, <code>bs*</code> — все товары игры)")
        return
    tmp = os.path.join(IMG_DIR, "_tmp.jpg")
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25)) as ses:
            async with ses.get(a[1], headers={"User-Agent": "Mozilla/5.0"}) as r:
                data = await r.read()
                if r.status != 200 or not r.headers.get("Content-Type", "").startswith("image/") or len(data) > 6_000_000:
                    raise ValueError(f"HTTP {r.status}, {r.headers.get('Content-Type')}, {len(data)} байт — нужна прямая ссылка на картинку")
        with open(tmp, "wb") as f:
            f.write(data)
        save_img_record(keys, tmp)
    except Exception as e:
        await message.answer(f"Не вышло: {escape(str(e))[:300]}")
        return
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    await message.answer(f"🖼 Фото сохранено ({len(keys)} шт.). Обнови магазин.")


@dp.message(Command("delimg"))
async def admin_delimg(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    keys = img_keys((command.args or "").strip())
    n = 0
    with closing(db()) as c:
        for key in keys:
            n += c.execute("DELETE FROM images WHERE key=?", (key,)).rowcount
            try:
                os.remove(os.path.join(IMG_DIR, f"{key}.jpg"))
            except OSError:
                pass
        c.commit()
    await message.answer(f"🗑 Фото удалено: {n}" if n else "У этого ID нет фото")


async def _complete(o: dict, auto: bool = False):
    """Заказ завершён: покупателю — уведомление, продавцу — деньги, магазину — кэшбэк/реферал."""
    oid = o["id"]
    await safe_send(
        o["user_id"],
        (f"⏱ Прошло {AUTO_CONFIRM_DAYS} дн. — заказ #{oid} подтверждён автоматически.\n" if auto else f"✅ <b>Заказ #{oid} завершён!</b>\n")
        + f"{escape(o['game'])} — {escape(o['label'])}\n"
        f"Спасибо, что выбрал {SHOP_NAME}! 💚\n⭐ Оставь отзыв в приложении: вкладка «Заказы».\n"
        f"Если что-то не так — {escape(SUPPORT)}")
    gain = credit_seller(o)
    if gain:
        await notify_seller(o["seller_id"], f"💰 Заказ #{oid} подтверждён{' автоматически' if auto else ' покупателем'}: "
                                            f"<b>+{gain}⭐</b> на твой баланс (комиссия магазина {seller_fee(o['seller_id'])}%).")
        await check_levelup(o["seller_id"])
    cb = 0 if o["seller_id"] else credit_cashback(o)
    if cb:
        await safe_send(o["user_id"], f"💸 Кэшбэк <b>+{cb} баллов</b> за заказ #{oid} (1 балл = 1 ₽).\n"
                                      f"Баланс: {user_bonus(o['user_id'])} баллов — спишется при следующей оплате.")
    ref, reward = (0, 0) if o["seller_id"] else credit_referrer(o)
    if ref:
        await safe_send(ref, f"🎁 Тебе начислено <b>{reward} баллов</b>: друг получил заказ #{oid}.\n"
                             f"Баланс: {user_bonus(ref)} баллов — тратится на следующий заказ в магазине.")


async def process_done(oid: int, actor: int) -> tuple[bool, str]:
    """«Выполнено» — только админ (товары магазина, либо решение спора в пользу продавца)."""
    o = get_order(oid)
    if not o or not is_admin(actor):
        return False, "Нет доступа"
    if not set_status(oid, "done", LIVE):
        return False, "Заказ уже обработан"
    await _complete(o)
    return True, "Завершено"


async def process_deliver(oid: int, actor: int) -> tuple[bool, str]:
    """Продавец отметил: товар передан. Покупатель получает кнопки подтверждения, у продавца пока ничего не зачислено."""
    o = get_order(oid)
    if not o or not o["seller_id"] or not (is_admin(actor) or o["seller_id"] == actor):
        return False, "Нет доступа"
    with closing(db()) as c:
        if not c.execute("UPDATE orders SET status='delivered', delivered_at=? WHERE id=? AND status='paid'",
                         (int(time.time()), oid)).rowcount:
            return False, "Заказ уже обработан"
        c.commit()
    try:
        await bot.send_message(
            o["user_id"], f"📦 <b>Заказ #{oid} передан</b>\n{escape(o['game'])} — {escape(o['label'])}\n"
                          f"Продавец {escape(seller_name(o['seller_id']) or '')} отметил, что выдал товар. Проверь аккаунт.\n\n"
                          f"Если всё хорошо — подтверди. Если нажать ничего — заказ подтвердится автоматически через {AUTO_CONFIRM_DAYS} дн. "
                          "Если что-то не так — «Проблема», и вопрос решит поддержка.", reply_markup=buyer_kb(oid))
    except Exception as e:
        logging.info("deliver notify buyer %s: %s", o["user_id"], e)
    return True, "Покупателю отправлено. Деньги придут после подтверждения"


async def process_confirm(oid: int, actor: int, auto: bool = False) -> tuple[bool, str]:
    o = get_order(oid)
    if not o or not o["seller_id"] or not (auto or o["user_id"] == actor or is_admin(actor)):
        return False, "Нет доступа"
    if not set_status(oid, "done", ("delivered",)):
        return False, "Заказ уже обработан"
    await _complete(o, auto)
    return True, "Спасибо! Заказ завершён"


DISPUTE_REASONS = {
    "not_delivered": "📭 Товар не получен",
    "no_reply": "🔕 Продавец не отвечает",
    "wrong": "🔄 Выдали не то, что заказывал",
    "not_working": "⚙️ Не работает / ошибка",
    "other": "✍️ Другое (опишу сам)",
}
DISPUTE_NOTE: dict[int, dict] = {}   # покупатель дописывает детали спора в боте


async def process_dispute(oid: int, actor: int, code: str = "other", text: str = "") -> tuple[bool, str]:
    o = get_order(oid)
    if not o or not o["seller_id"] or o["user_id"] != actor:
        return False, "Нет доступа"
    if code not in DISPUTE_REASONS:
        code, text = "other", (text or code)
    text = _clip(text, 400)
    if code == "other" and len(text) < 5:
        return False, "Опиши проблему хотя бы в паре слов"
    with closing(db()) as c:
        if not c.execute("UPDATE orders SET status='dispute' WHERE id=? AND status IN ('paid','delivered')", (oid,)).rowcount:
            return False, "Спор уже открыт или заказ завершён"
        c.commit()
    o = get_order(oid)
    label = DISPUTE_REASONS[code]
    await bot.send_message(ADMIN_ID, "⚠️ <b>СПОР по заказу</b>\n" + admin_order_text(o, user_info(actor))
                           + f"\nПричина: <b>{escape(label)}</b>" + (f"\nОписание: {escape(text)}" if text else "")
                           + f"\n\nПереписка: /chat {oid}", reply_markup=admin_kb(oid))
    await notify_seller(o["seller_id"], f"⚠️ По заказу #{oid} открыт спор: {escape(label)}. Решение примет поддержка, деньги заморожены до разбора.")
    return True, "Спор открыт, поддержка разберётся"


async def process_cancel(oid: int, actor: int) -> tuple[bool, str]:
    o = get_order(oid)
    if not o or not (is_admin(actor) or (o["seller_id"] and o["seller_id"] == actor)):
        return False, "Нет доступа"
    allowed = LIVE if is_admin(actor) else ("paid",)   # продавец отменяет только до передачи товара
    if o["status"] not in allowed:
        return False, "Заказ уже обработан"
    try:
        await bot.refund_star_payment(user_id=o["user_id"], telegram_payment_charge_id=o["charge_id"])
    except Exception as e:
        return False, f"Возврат не удался: {e}"[:190]
    set_status(oid, "refunded", LIVE)
    release_refund(o)
    back = f" и баллы ({o['bonus']}) вернулись на баланс" if o["bonus"] else ""
    await safe_send(
        o["user_id"],
        f"❌ <b>Заказ #{oid} отменён.</b>\nStars ({o['stars']}⭐) возвращены на твой счёт{back}.\n"
        f"Вопросы — {escape(SUPPORT)}")
    if o["seller_id"] and o["seller_id"] != actor:
        await notify_seller(o["seller_id"], f"❌ Заказ #{oid} отменён поддержкой, Stars возвращены покупателю.")
    return True, "Stars возвращены"


async def cb_done(callback: CallbackQuery):
    ok, msg = await process_done(int(callback.data.split(":")[1]), callback.from_user.id)
    if ok:
        await callback.message.edit_text(callback.message.html_text + "\n\n✅ <b>Выполнено</b>", reply_markup=None)
    await callback.answer(msg, show_alert=not ok)


async def cb_cancel(callback: CallbackQuery):
    ok, msg = await process_cancel(int(callback.data.split(":")[1]), callback.from_user.id)
    if ok:
        await callback.message.edit_text(callback.message.html_text + "\n\n❌ <b>Отменён, Stars возвращены</b>", reply_markup=None)
    await callback.answer(msg, show_alert=not ok)


async def cb_deliver(callback: CallbackQuery):
    ok, msg = await process_deliver(int(callback.data.split(":")[1]), callback.from_user.id)
    if ok:
        await callback.message.edit_text(callback.message.html_text + "\n\n📦 <b>Передано, ждём подтверждения покупателя</b>", reply_markup=None)
    await callback.answer(msg, show_alert=not ok)


def why_kb(oid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=f"bwhy:{oid}:{k}")] for k, t in DISPUTE_REASONS.items()]
                                + [[InlineKeyboardButton(text="« Назад", callback_data=f"bback:{oid}")]])


async def cb_buyer(callback: CallbackQuery):
    """Кнопки покупателя под сообщением о передаче: bok — всё получил, bbad — проблема (дальше выбор причины)."""
    kind, num = callback.data.split(":")
    oid = int(num)
    uid = callback.from_user.id
    if kind == "bok":
        ok, msg = await process_confirm(oid, uid)
        if ok:
            await callback.message.edit_text(callback.message.html_text + "\n\n✅ <b>Получение подтверждено</b>", reply_markup=None)
        await callback.answer(msg, show_alert=not ok)
    elif kind == "bback":
        await callback.message.edit_reply_markup(reply_markup=buyer_kb(oid))
        await callback.answer()
    else:
        o = get_order(oid)
        if not o or o["user_id"] != uid or o["status"] not in ("paid", "delivered"):
            await callback.answer("Спор уже открыт или заказ завершён", show_alert=True)
            return
        await callback.message.edit_reply_markup(reply_markup=why_kb(oid))
        await callback.answer("Выбери, что случилось")


async def cb_why(callback: CallbackQuery):
    _, num, code = callback.data.split(":")
    oid, uid = int(num), callback.from_user.id
    if code == "other":
        DISPUTE_NOTE[uid] = {"oid": oid, "code": "other", "t": time.time(), "extra": False}
        await callback.message.answer("✍️ Опиши проблему одним сообщением — я передам её поддержке.")
        await callback.answer()
        return
    ok, msg = await process_dispute(oid, uid, code)
    if ok:
        DISPUTE_NOTE[uid] = {"oid": oid, "code": code, "t": time.time(), "extra": True}
        await callback.message.edit_text(callback.message.html_text + f"\n\n⚠️ <b>Спор открыт: {escape(DISPUTE_REASONS[code])}</b>\n"
                                         "Хочешь добавить подробности — напиши их следующим сообщением (10 минут).", reply_markup=None)
    await callback.answer(msg, show_alert=not ok)


async def dispute_note(message: Message) -> bool:
    """Текст покупателя после выбора причины: «Другое» открывает спор, остальные — дополняют его."""
    st = DISPUTE_NOTE.get(message.from_user.id)
    if not st or time.time() - st["t"] > 600:
        DISPUTE_NOTE.pop(message.from_user.id, None)
        return False
    DISPUTE_NOTE.pop(message.from_user.id, None)
    text = message.text or ""
    if st["extra"]:
        await bot.send_message(ADMIN_ID, f"➕ <b>Уточнение по спору #{st['oid']}</b> от покупателя:\n{escape(_clip(text, 500))}")
        await message.answer("✅ Передал поддержке.")
        return True
    ok, msg = await process_dispute(st["oid"], message.from_user.id, "other", text)
    await message.answer("⚠️ Спор открыт, поддержка свяжется." if ok else "⚠️ " + msg)
    return True


async def cb_reply(callback: CallbackQuery, bot_key: str = "m"):
    """Кнопка «Ответить» под сообщением чата: следующий текст уйдёт собеседнику."""
    oid = int(callback.data.split(":")[1])
    o = get_order(oid)
    if not chat_role(o, callback.from_user.id) or o["status"] not in CHAT_OPEN:
        await callback.answer("Чат недоступен", show_alert=True)
        return
    chat_set(bot_key, callback.from_user.id, oid)
    mark_read(oid, callback.from_user.id)
    await callback.message.answer(f"✍️ Пиши ответ по заказу #{oid} — следующее сообщение уйдёт собеседнику. Выйти из чата: /stop")
    await callback.answer()


async def cb_reply_seller(callback: CallbackQuery):
    await cb_reply(callback, "s")


async def relay_text(message: Message, bot_key: str = "m"):
    """Текст вне команд: если выбран чат заказа — отправляем собеседнику."""
    oid = chat_get(bot_key, message.from_user.id)
    if not oid:
        return False
    mark_read(oid, message.from_user.id)
    ok, msg = await chat_send(oid, message.from_user.id, message.text or "")
    await message.answer("✅ Отправлено" if ok else "⚠️ " + msg)
    return True


async def main_text(message: Message):
    if await dispute_note(message):
        return
    if not await relay_text(message, "m"):
        await message.answer("Чтобы открыть магазин, нажми /start. Ответить продавцу можно кнопкой «Ответить» под его сообщением.")


async def cmd_stop(message: Message):
    chat_set("m", message.from_user.id, 0)
    chat_set("s", message.from_user.id, 0)
    await message.answer("Вышел из чата заказа.")


# ==========================================
# 🧑‍💼 ПРОДАВЦЫ: модерация, выплаты, фото объявлений
# ==========================================
@dp.callback_query(F.data.regexp(r"^(selok|selno|ofok|ofno|payok|payno):\d+$"))
async def cb_moderate(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    kind, num = callback.data.split(":")
    num = int(num)
    note = ""
    with closing(db()) as c:
        if kind in ("selok", "selno"):
            new = "approved" if kind == "selok" else "banned"
            if not c.execute("UPDATE sellers SET status=? WHERE user_id=? AND status='pending'", (new, num)).rowcount:
                await callback.answer("Уже обработано", show_alert=True)
                return
            c.commit()
            if new == "approved":
                rows = []
                if SELLER_BOT_USERNAME:
                    rows.append([InlineKeyboardButton(text="🚀 Перейти в Azika Seller", url=f"https://t.me/{SELLER_BOT_USERNAME}")])
                try:
                    await bot.send_message(num, "✅ <b>Заявка одобрена!</b>\nТеперь тебе доступен <b>Azika Seller</b> — там ты выставляешь объявления, "
                                                "делаешь промокоды, рекламу и общаешься с покупателями. Нажми кнопку ниже.",
                                           reply_markup=InlineKeyboardMarkup(inline_keyboard=rows) if rows else None)
                except Exception as e:
                    logging.info("approve notify %s: %s", num, e)
            else:
                await safe_send(num, "❌ К сожалению, заявка отклонена.")
            note = "✅ Одобрен" if new == "approved" else "❌ Отклонён"
        elif kind in ("ofok", "ofno"):
            new = "active" if kind == "ofok" else "rejected"
            r = c.execute("SELECT seller_id,title FROM offers WHERE id=? AND status='pending'", (num,)).fetchone()
            if not r:
                await callback.answer("Уже обработано", show_alert=True)
                return
            c.execute("UPDATE offers SET status=? WHERE id=?", (new, num))
            c.commit()
            await notify_seller(r[0], (f"✅ Товар «{escape(r[1])}» опубликован в магазине!" if new == "active"
                                   else f"❌ Товар «{escape(r[1])}» отклонён модератором."))
            note = "✅ Опубликован" if new == "active" else "❌ Отклонён"
        else:
            r = c.execute("SELECT seller_id,stars FROM payouts WHERE id=? AND status='pending'", (num,)).fetchone()
            if not r:
                await callback.answer("Уже обработано", show_alert=True)
                return
            if kind == "payok":
                c.execute("UPDATE payouts SET status='paid' WHERE id=?", (num,))
                await notify_seller(r[0], f"💸 Выплата #{num} на {r[1]}⭐ отправлена.")
                note = "✅ Выплачено"
            else:
                c.execute("UPDATE payouts SET status='rejected' WHERE id=?", (num,))
                c.execute("UPDATE sellers SET balance=balance+? WHERE user_id=?", (r[1], r[0]))
                await notify_seller(r[0], f"↩️ Выплата #{num} отклонена, {r[1]}⭐ вернулись на баланс.")
                note = "↩️ Отклонено, баланс возвращён"
            c.commit()
    await callback.message.edit_text(callback.message.html_text + f"\n\n<b>{note}</b>", reply_markup=None)
    await callback.answer(note)


async def save_offer_photo(message: Message, oid) -> str:
    o = offer_get(str(oid))
    if not o or not (o["seller_id"] == message.from_user.id or is_admin(message.from_user.id)):
        return "Это не твой товар или такого номера нет."
    tmp = os.path.join(IMG_DIR, f"_tmp{message.from_user.id}.jpg")
    try:
        await message.bot.download(message.photo[-1], destination=tmp)
        save_img_record([f"o{o['id']}"], tmp)
    except Exception as e:
        return f"Не удалось сохранить фото: {escape(str(e))}"
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    return "🖼 Фото сохранено. Обнови магазин."


async def seller_offerimg(message: Message):
    """Фото с подписью /offerimg НОМЕР — для своего объявления (в любом из ботов)."""
    await message.answer(await save_offer_photo(message, message.caption.split()[1]))


@dp.message(Command("chat"))
async def admin_chat(message: Message, command: CommandObject):
    """/chat НОМЕР_ЗАКАЗА — переписка продавца и покупателя (для разбора споров)."""
    if not is_admin(message.from_user.id):
        return
    a = (command.args or "").strip()
    if not a.isdigit():
        await message.answer("Формат: /chat НОМЕР_ЗАКАЗА")
        return
    with closing(db()) as c:
        rows = c.execute("SELECT from_id,text,created FROM messages WHERE order_id=? ORDER BY id DESC LIMIT 40", (int(a),)).fetchall()
    o = get_order(int(a))
    lines = [f"{'🛍 покупатель' if o and r[0] == o['user_id'] else '🧑‍💼 продавец'} · {fmt_time(r[2])}\n{escape(r[1])}" for r in reversed(rows)]
    await message.answer("\n\n".join(lines) or "Сообщений нет.")


@dp.message(Command("verify", "unverify"))
async def admin_verify(message: Message, command: CommandObject):
    """/verify USER_ID — выдать продавцу галочку (✓ рядом с именем), /unverify — снять."""
    if not is_admin(message.from_user.id):
        return
    a = (command.args or "").split()
    if len(a) != 1 or not a[0].isdigit() or not seller_row(int(a[0])):
        await message.answer("Формат: /verify USER_ID (ID из /sellers)")
        return
    on = message.text.startswith("/verify")
    with closing(db()) as c:
        c.execute("UPDATE sellers SET verified=? WHERE user_id=?", (1 if on else 0, int(a[0])))
        c.commit()
    await message.answer("✅ Галочка выдана" if on else "Галочка снята")
    if on:
        await notify_seller(int(a[0]), "✅ Тебе выдана галочка проверенного продавца — она теперь видна рядом с именем в магазине!")


@dp.message(Command("sellers"))
async def admin_sellers(message: Message):
    if not is_admin(message.from_user.id):
        return
    with closing(db()) as c:
        rows = c.execute("SELECT user_id,status,balance,earned,verified FROM sellers ORDER BY created DESC LIMIT 50").fetchall()
    lines = []
    for r in rows:
        avg, cnt = rating_of(r[0])
        lines.append(f"<code>{r[0]}</code> {escape(seller_name(r[0]))}{' ✓' if r[4] else ''} — {r[1]}, баланс {r[2]}⭐, заработано {r[3]}⭐, "
                     f"отзывов {cnt}" + (f" (★{avg})" if cnt else ""))
    await message.answer("\n".join(lines) + "\n\nГалочка: /verify USER_ID · снять: /unverify USER_ID" if lines else "Продавцов нет. Блокировка: /banseller USER_ID")


@dp.message(Command("banseller"))
async def admin_banseller(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    a = (command.args or "").split()
    if len(a) != 1 or not a[0].isdigit():
        await message.answer("Формат: /banseller USER_ID (его товары пропадут из магазина)")
        return
    with closing(db()) as c:
        c.execute("UPDATE sellers SET status='banned' WHERE user_id=?", (int(a[0]),))
        c.commit()
    await message.answer("🚫 Продавец заблокирован, товары скрыты.")


async def autoconfirm_loop():
    """Раз в 30 минут: переданные заказы, которые покупатель не подтвердил за AUTO_CONFIRM_DAYS, завершаются автоматически."""
    while True:
        try:
            limit = int(time.time()) - AUTO_CONFIRM_DAYS * 86400
            with closing(db()) as c:
                ids = [r[0] for r in c.execute("SELECT id FROM orders WHERE status='delivered' AND delivered_at>0 AND delivered_at<?", (limit,))]
            for oid in ids:
                await process_confirm(oid, 0, auto=True)
        except Exception as e:
            logging.warning("autoconfirm: %s", e)
        await asyncio.sleep(1800)


async def expire_loop():
    """Каждую минуту отменяет заказы, не оплаченные за ORDER_TTL_MIN минут."""
    while True:
        try:
            for uid, oid, label in expire_old_orders():
                await safe_send(uid, f"⏱ Заказ #{oid} ({escape(label)}) отменён: не оплачен за {ORDER_TTL_MIN} минут. "
                                     "Оформи заказ заново, если он ещё нужен.")
        except Exception as e:
            logging.warning("expire_loop: %s", e)
        await asyncio.sleep(60)


# ==========================================
# ⭐ ОПЛАТА
# ==========================================
@dp.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    ok, err = False, "Заказ недоступен"
    try:
        kind, oid = query.invoice_payload.split(":")
        o = get_order(int(oid)) if kind == "shop" else None
        if o and o["status"] == "new" and time.time() - o["created"] > ORDER_TTL_MIN * 60:
            err = f"Время на оплату ({ORDER_TTL_MIN} мин) истекло, создай заказ заново"
        elif (o and o["status"] == "new" and o["user_id"] == query.from_user.id
                and query.currency == "XTR" and query.total_amount == o["stars"] and pack_available(o["pack"])
                and user_bonus(o["user_id"]) >= o["bonus"]):
            ok, err = True, ""
        elif o and o["status"] == "new" and user_bonus(o["user_id"]) < o["bonus"]:
            err = "Бонусный баланс изменился, создай заказ заново"
    except Exception:
        pass
    await query.answer(ok=ok, error_message=None if ok else err)


@dp.message(F.successful_payment)
async def successful_payment(message: Message):
    pay = message.successful_payment
    try:
        kind, oid = pay.invoice_payload.split(":")
        oid = int(oid)
    except Exception:
        return
    if kind != "shop":
        return
    u = message.from_user
    ensure_user(u.id, u.first_name, u.username)
    o = get_order(oid)
    if not o or not mark_paid(oid, pay.telegram_payment_charge_id):
        # деньги пришли, а заказ не найден/уже обработан — возвращаем и сообщаем админу
        try:
            await bot.refund_star_payment(user_id=u.id, telegram_payment_charge_id=pay.telegram_payment_charge_id)
        except Exception as e:
            logging.warning("auto refund: %s", e)
        await bot.send_message(ADMIN_ID, f"⚠️ Платёж без активного заказа #{oid} от {u.id}: "
                                         f"{pay.total_amount}⭐ ({escape(pay.telegram_payment_charge_id)}). Пробую вернуть Stars.")
        return
    o = get_order(oid)
    await message.answer(
        f"✅ <b>Оплата получена!</b> Заказ #{oid}\n{escape(o['game'])} — {escape(o['label'])}\n"
        f"Игрок: <code>{escape(o['player'])}</code>\n\nАдмин пополнит аккаунт в ближайшее время и пришлёт уведомление.")
    try:
        await bot.send_message(ADMIN_ID, "💰 <b>Новая оплата!</b>\n\n" + admin_order_text(o, user_info(o["user_id"])),
                               reply_markup=admin_kb(oid))
    except Exception as e:
        logging.warning("notify admin: %s", e)
    if o["seller_id"] and o["seller_id"] != ADMIN_ID:
        await notify_seller(o["seller_id"], "💰 <b>Твой товар купили!</b> Выдай заказ и нажми «Выполнено».\n"
                            "Выдай товар и нажми «Передал товар». Деньги придут на баланс, когда покупатель подтвердит получение "
                            f"(или автоматически через {AUTO_CONFIRM_DAYS} дн.). Вопросы покупателю — «Чаты» в кабинете или здесь.\n\n"
                            + admin_order_text(o, user_info(o["user_id"])), seller_order_kb(oid))


# ==========================================
# 🌐 API ДЛЯ MINI APP
# ==========================================
def verify_init_data(init_data: str) -> dict | None:
    """Мини-приложения магазина и Azika Seller подписываются токенами разных ботов — пробуем оба."""
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        got = pairs.pop("hash")
        check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        for tok in (BOT_TOKEN, SELLER_BOT_TOKEN):
            if not tok:
                continue
            secret = hmac.new(b"WebAppData", tok.encode(), hashlib.sha256).digest()
            if hmac.compare_digest(hmac.new(secret, check.encode(), hashlib.sha256).hexdigest(), got):
                break
        else:
            return None
        if time.time() - int(pairs.get("auth_date", "0")) > 86400:
            return None
        user = json.loads(pairs["user"])
        user["_sp"] = pairs.get("start_param", "")
        return user
    except Exception:
        return None


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


async def _auth(request):
    """Проверяет подпись Telegram, регистрирует пользователя (и реферала из start_param)."""
    try:
        data = await request.json()
    except Exception:
        return None, {}
    user = verify_init_data(str(data.get("initData", "")))
    if user:
        touch_seen(user["id"])
        ref = parse_ref(user.get("_sp", ""))
        if ensure_user(user["id"], user.get("first_name", ""), user.get("username", ""), ref) and get_ref_by(user["id"]):
            asyncio.create_task(welcome_referrer(get_ref_by(user["id"]), user.get("first_name", "")))
        ph = str(user.get("photo_url") or "")[:300]
        if ph:
            with closing(db()) as c:
                if c.execute("UPDATE users SET photo=? WHERE user_id=? AND COALESCE(photo,'')!=?", (ph, user["id"], ph)).rowcount:
                    for f in (f"av_{user['id']}.jpg", f"av_{user['id']}.jpg.none"):
                        try:
                            os.remove(os.path.join(IMG_DIR, f))
                        except OSError:
                            pass
                c.commit()
    return user, data


def _deny():
    return web.json_response({"ok": False, "msg": "Открой приложение из Telegram"}, status=401)


_rate: dict[int, list] = {}


def rate_ok(uid: int, limit: int = 6, window: int = 300) -> bool:
    now = time.time()
    hits = [t for t in _rate.get(uid, []) if now - t < window]
    if len(hits) >= limit:
        _rate[uid] = hits
        return False
    hits.append(now)
    _rate[uid] = hits
    return True


async def api_ping(request):
    return web.json_response({"ok": True, "shop": SHOP_NAME})


async def api_catalog(request):
    with closing(db()) as c:
        done = c.execute("SELECT COUNT(*) FROM orders WHERE status='done'").fetchone()[0]
    return web.json_response({"ok": True, "paused": sales_paused(), "shop": SHOP_NAME, "support": SUPPORT, "ref_percent": REF_PERCENT,
                              "done": done, "star_rub": STAR_RUB, "cashback_pct": max(p for _, p in CB_LEVELS), "cashback_levels": cb_level(0)["levels"],
                              "cashback_min": CASHBACK_MIN, "ttl_min": ORDER_TTL_MIN, "seller_fee": SELLER_FEE, "seller_fee_min": min(_FEES),
                              "payout_min": PAYOUT_MIN, "seller_bot": SELLER_BOT_USERNAME, "seller_app": SELLER_WEBAPP_URL, "ad_price": AD_PRICE, "shop_rating": dict(zip(("avg", "count"), rating_of(0))),
                              "games": catalog()})


async def api_promo(request):
    """Проверка промокода без выбора товара (активация на главной / в профиле)."""
    user, data = await _auth(request)
    if not user:
        return _deny()
    code = str(data.get("promo", "")).strip()
    if not code:
        return web.json_response({"ok": False, "msg": "Введи промокод"})
    ok, pct, msg = promo_check(user["id"], code)
    return web.json_response({"ok": ok, "pct": pct, "code": code.upper(), "msg": msg})


async def api_img(request):
    key = request.match_info.get("key", "")
    if key not in PACKS and key not in GAME_BY_ID and not is_offer_id(key):
        raise web.HTTPNotFound()
    path = os.path.join(IMG_DIR, f"{key}.jpg")
    if not os.path.isfile(path):
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers={"Cache-Control": "public, max-age=86400", "Content-Type": "image/jpeg"})


async def api_profile(request):
    user, _ = await _auth(request)
    if not user:
        return _deny()
    return web.json_response({"ok": True, **profile_data(user["id"])})


async def api_orders(request):
    user, _ = await _auth(request)
    if not user:
        return _deny()
    now = time.time()
    with closing(db()) as c:
        reviewed = {r[0] for r in c.execute("SELECT order_id FROM reviews WHERE user_id=?", (user["id"],))}
        unread = dict(c.execute("SELECT order_id,COUNT(*) FROM messages WHERE to_id=? AND read=0 GROUP BY order_id", (user["id"],)).fetchall())
    out = [{"id": o["id"], "game": o["game"], "label": o["label"], "player": o["player"], "stars": o["stars"],
            "cashback": o["cashback"], "status": o["status"], "status_text": STATUS_TXT.get(o["status"], o["status"]),
            "created": fmt_time(o["created"]), "seller_id": o["seller_id"],
            "expires_in": max(0, int(o["created"] + ORDER_TTL_MIN * 60 - now)) if o["status"] == "new" else 0,
            "can_review": o["status"] == "done" and o["id"] not in reviewed, "reviewed": o["id"] in reviewed,
            "chat": bool(o["seller_id"]) and o["status"] in CHAT_OPEN, "unread": unread.get(o["id"], 0),
            "seller_review": on_review(o["seller_id"]),
            "escrow": bool(o["seller_id"]), "can_confirm": o["status"] == "delivered" and bool(o["seller_id"]),
            "can_dispute": o["status"] in ("paid", "delivered") and bool(o["seller_id"]),
            "auto_in": max(0, int(o["delivered_at"] + AUTO_CONFIRM_DAYS * 86400 - now)) if o["status"] == "delivered" else 0}
           for o in user_orders(user["id"])]
    return web.json_response({"ok": True, "orders": out})


async def api_quote(request):
    user, data = await _auth(request)
    if not user:
        return _deny()
    pack_id = str(data.get("pack", ""))
    if not pack_available(pack_id):
        return web.json_response({"ok": False, "msg": "Товар недоступен"})
    q = calc(user["id"], pack_id, str(data.get("promo", "")), bool(data.get("use_bonus", True)))
    return web.json_response({"ok": True, **q})


async def api_invoice(request):
    user, data = await _auth(request)
    if not user:
        return _deny()
    uid = user["id"]
    if sales_paused():
        return web.json_response({"ok": False, "msg": sales_paused()})
    pack_id = str(data.get("pack", ""))
    player = str(data.get("player", "")).strip()
    comment = str(data.get("comment", "")).strip()[:200]
    promo = str(data.get("promo", "")).strip()
    if not pack_available(pack_id):
        return web.json_response({"ok": False, "msg": "Товар недоступен"})
    g = pack_info(pack_id)[0]
    if is_offer_id(pack_id) and offer_get(pack_id)["seller_id"] == uid:
        return web.json_response({"ok": False, "msg": "Нельзя купить свой собственный товар"})
    if not valid_player(g["kind"], player):
        return web.json_response({"ok": False, "msg": "Проверь ID игрока: " + g["field"]})
    q = calc(uid, pack_id, promo, bool(data.get("use_bonus", True)))
    if promo and not q["promo_ok"] and not q.get("promo_scope"):
        return web.json_response({"ok": False, "msg": q["promo_msg"]})
    if not rate_ok(uid):
        return web.json_response({"ok": False, "msg": "Слишком много попыток, подожди пару минут"})
    oid = create_order(uid, pack_id, player, comment, q)
    o = get_order(oid)
    desc = f"Пополнение игрового аккаунта. Игрок: {player}. Счёт действует {ORDER_TTL_MIN} мин"
    if q["pct"]:
        desc += f" (промокод −{q['pct']}%)"
    try:
        link = await bot.create_invoice_link(
            title=f"{o['game']} — {o['label']}", description=desc,
            payload=f"shop:{oid}", provider_token="", currency="XTR",
            prices=[LabeledPrice(label=o["label"], amount=o["stars"])])
    except Exception as e:
        logging.warning("create_invoice_link: %s", e)
        return web.json_response({"ok": False, "msg": "Не удалось создать счёт. Попробуй позже"})
    return web.json_response({"ok": True, "link": link, "order": oid, "stars": o["stars"], "ttl": ORDER_TTL_MIN * 60})




# ==========================================
# 🧑‍💼 ОБЩАЯ ЛОГИКА ПРОДАВЦОВ (используют и приложение, и бот продавцов)
# ==========================================
def _clip(v, n) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip()[:n]


def _kb(*pairs) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in pairs]])


async def notify_seller(uid: int, text: str, kb=None) -> bool:
    """Пишет продавцу через бота продавцов (если настроен и продавец его запускал), иначе через основной бот."""
    if SELLER_READY:
        try:
            await seller_bot.send_message(uid, text, reply_markup=kb)
            return True
        except Exception as e:
            logging.info("seller_bot -> %s: %s", uid, e)
    try:
        await bot.send_message(uid, text, reply_markup=kb)
        return True
    except Exception as e:
        logging.warning("notify_seller %s: %s", uid, e)
        return False


async def submit_application(uid: int, about: str):
    sl = seller_row(uid)
    if sl and sl["status"] in ("approved", "pending", "banned"):
        return False, {"approved": "Ты уже продавец", "pending": "Заявка уже на рассмотрении",
                       "banned": "Продажи для тебя закрыты"}[sl["status"]]
    about = _clip(about, 300)
    if len(about) < 10:
        return False, "Расскажи о себе и что продаёшь (минимум 10 символов)"
    if not rate_ok(uid, 3, 3600):
        return False, "Слишком часто, попробуй позже"
    with closing(db()) as c:
        c.execute("INSERT OR REPLACE INTO sellers(user_id,about,status,balance,earned,created) VALUES(?,?,'pending',0,0,?)",
                  (uid, about, int(time.time())))
        c.commit()
    await bot.send_message(ADMIN_ID, f"🧑‍💼 <b>Заявка в продавцы</b>\n{user_info(uid)} (<code>{uid}</code>)\n\n{escape(about)}",
                           reply_markup=_kb(("✅ Одобрить", f"selok:{uid}"), ("❌ Отклонить", f"selno:{uid}")))
    return True, ""


async def create_offer(uid: int, game: str, title: str, descr: str, stars) -> tuple[bool, str, str]:
    sl = seller_row(uid)
    if not sl or sl["status"] != "approved":
        return False, "Сначала нужно стать продавцом", ""
    title, descr = _clip(title, 60), _clip(descr, 300)
    try:
        stars = int(stars)
    except (TypeError, ValueError):
        stars = 0
    if game not in GAME_BY_ID or len(title) < 3 or not 5 <= stars <= 200000:
        return False, "Заполни игру, название (от 3 символов) и цену (от 5 ⭐)", ""
    with closing(db()) as c:
        L = seller_level(uid)
        if c.execute("SELECT COUNT(*) FROM offers WHERE seller_id=? AND status IN ('pending','active','hidden')", (uid,)).fetchone()[0] >= L["limit"]:
            return False, f"Лимит объявлений на уровне «{L['name']}» — {L['limit']}. Совершай сделки и получай отзывы, чтобы поднять уровень", ""
        cur = c.execute("INSERT INTO offers(seller_id,game,title,descr,stars,status,created) VALUES(?,?,?,?,?,'pending',?)",
                        (uid, game, title, descr, stars, int(time.time())))
        c.commit()
        oid = cur.lastrowid
    await bot.send_message(ADMIN_ID, f"🆕 <b>Новый товар #{oid}</b> от {user_info(uid)} (<code>{uid}</code>)\n"
                                     f"{escape(GAME_BY_ID[game]['name'])} — <b>{escape(title)}</b> · {stars}⭐\n{escape(descr)}",
                           reply_markup=_kb(("✅ Опубликовать", f"ofok:{oid}"), ("❌ Отклонить", f"ofno:{oid}")))
    return True, "", f"o{oid}"


async def request_payout(uid: int, amount, details: str) -> tuple[bool, str]:
    sl = seller_row(uid)
    try:
        amount = int(amount)
    except (TypeError, ValueError):
        amount = 0
    details = _clip(details, 200)
    if not sl or sl["status"] != "approved":
        return False, "Недоступно"
    if amount < PAYOUT_MIN or amount > sl["balance"]:
        return False, f"Сумма от {PAYOUT_MIN} ⭐ и не больше баланса ({sl['balance']} ⭐)"
    if len(details) < 5:
        return False, "Укажи, куда выплатить (карта, кошелёк или @username)"
    with closing(db()) as c:
        if not c.execute("UPDATE sellers SET balance=balance-? WHERE user_id=? AND balance>=?", (amount, uid, amount)).rowcount:
            return False, "Недостаточно средств"
        cur = c.execute("INSERT INTO payouts(seller_id,stars,details,created) VALUES(?,?,?,?)", (uid, amount, details, int(time.time())))
        c.commit()
        pid = cur.lastrowid
    await bot.send_message(ADMIN_ID, f"💸 <b>Запрос на вывод #{pid}</b>\n{user_info(uid)} (<code>{uid}</code>)\n"
                                     f"Сумма: <b>{amount}⭐</b>\nКуда: {escape(details)}",
                           reply_markup=_kb(("✅ Выплачено", f"payok:{pid}"), ("↩️ Отклонить", f"payno:{pid}")))
    return True, ""


def create_seller_promo(uid: int, code: str, pct, max_uses=0, days=0) -> tuple[bool, str]:
    """Промокод продавца: скидка только на его товары, за счёт продавца."""
    code = (code or "").strip().upper()
    sl = seller_row(uid)
    if not sl or sl["status"] != "approved":
        return False, "Недоступно"
    try:
        pct, max_uses, days = int(pct), int(max_uses or 0), int(days or 0)
    except ValueError:
        return False, "Проценты, лимит и дни — числами"
    if not re.fullmatch(r"[A-Z0-9_-]{3,16}", code):
        return False, "Код: 3–16 символов, латиница, цифры, _ или -"
    if not 1 <= pct <= SELLER_PROMO_MAX:
        return False, f"Скидка от 1 до {SELLER_PROMO_MAX}%"
    if max_uses < 0 or days < 0:
        return False, "Числа не могут быть отрицательными"
    with closing(db()) as c:
        ex = c.execute("SELECT seller_id FROM promos WHERE code=?", (code,)).fetchone()
        if ex and int(ex[0] or 0) != uid:
            return False, "Такой код уже занят, выбери другой"
        if not ex and c.execute("SELECT COUNT(*) FROM promos WHERE seller_id=?", (uid,)).fetchone()[0] >= 20:
            return False, "Максимум 20 промокодов"
        c.execute("INSERT OR REPLACE INTO promos(code,pct,max_uses,used,expires,seller_id) VALUES(?,?,?,COALESCE((SELECT used FROM promos WHERE code=?),0),?,?)",
                  (code, pct, max_uses, code, int(time.time()) + days * 86400 if days else 0, uid))
        c.commit()
    return True, ""


def buy_ad(uid: int, offer_id: str, days) -> tuple[bool, str]:
    """Реклама товара: поднимается наверх списка с меткой «Реклама». Оплата с баланса продавца."""
    o = offer_get(offer_id)
    try:
        days = int(days)
    except (TypeError, ValueError):
        days = 0
    if not o or o["seller_id"] != uid or o["status"] != "active":
        return False, "Рекламировать можно только свой опубликованный товар"
    if days not in (1, 3, 7):
        return False, "Срок: 1, 3 или 7 суток"
    cost = AD_PRICE * days
    now = int(time.time())
    with closing(db()) as c:
        if not c.execute("UPDATE sellers SET balance=balance-? WHERE user_id=? AND balance>=?", (cost, uid, cost)).rowcount:
            return False, f"Не хватает баланса: нужно {cost} ⭐"
        until = max(now, int(o["boost_until"] or 0)) + days * 86400
        c.execute("UPDATE offers SET boost_until=?, boost_at=? WHERE id=?", (until, now, o["id"]))
        c.execute("INSERT INTO ads(offer_id,seller_id,days,cost,created) VALUES(?,?,?,?,?)", (o["id"], uid, days, cost, now))
        c.commit()
    return True, f"до {fmt_time(until)}"


# ---- Чат продавец ↔ покупатель (по заказу; контакты скрыты, общение через боты и приложение)
CONTACT_RE = re.compile(r"(@\w{4,}|t\.me|https?://|www\.|\+\d[\d\s\-()]{8,})", re.I)


def chat_role(o: dict | None, uid: int) -> str:
    if not o or not o["seller_id"]:
        return ""
    return "buyer" if uid == o["user_id"] else "seller" if uid == o["seller_id"] else ""


def chat_set(bot_key: str, uid: int, oid: int):
    with closing(db()) as c:
        if oid:
            c.execute("INSERT OR REPLACE INTO chat_state(bot,user_id,order_id) VALUES(?,?,?)", (bot_key, uid, oid))
        else:
            c.execute("DELETE FROM chat_state WHERE bot=? AND user_id=?", (bot_key, uid))
        c.commit()


def chat_get(bot_key: str, uid: int) -> int:
    with closing(db()) as c:
        r = c.execute("SELECT order_id FROM chat_state WHERE bot=? AND user_id=?", (bot_key, uid)).fetchone()
    return int(r[0]) if r else 0


def mark_read(oid: int, uid: int):
    with closing(db()) as c:
        c.execute("UPDATE messages SET read=1 WHERE order_id=? AND to_id=?", (oid, uid))
        c.commit()


def av_sig(uid: int) -> str:
    return hmac.new(BOT_TOKEN.encode(), f"av{uid}".encode(), hashlib.sha256).hexdigest()[:16]


def _av_custom(uid: int) -> str:
    return os.path.join(IMG_DIR, f"avc_{uid}.jpg")


def av_url(uid: int) -> str:
    if not uid:
        return ""
    p = _av_custom(uid)
    v = int(os.path.getmtime(p)) if os.path.exists(p) else 0
    return f"/api/avatar/{uid}?t={av_sig(uid)}&v={v}"


async def api_avatar_set(request):
    """Человек меняет аватарку: JPEG/PNG в base64 (приложение уменьшает до ~400px)."""
    user, data = await _auth(request)
    if not user:
        return _deny()
    import base64
    try:
        blob = base64.b64decode(str(data.get("data", "")).split(",")[-1])
    except Exception:
        return web.json_response({"ok": False, "msg": "Не удалось прочитать фото"})
    if len(blob) < 300 or len(blob) > 2_000_000 or blob[:2] not in (b"\xff\xd8", b"\x89P"):
        return web.json_response({"ok": False, "msg": "Нужна картинка JPEG или PNG до 2 МБ"})
    if not rate_ok(-user["id"], 6, 600):   # отдельный счётчик, не мешает чату
        return web.json_response({"ok": False, "msg": "Слишком часто, подожди немного"})
    with open(_av_custom(user["id"]), "wb") as f:
        f.write(blob)
    return web.json_response({"ok": True, "av": av_url(user["id"]), "av_custom": True})


async def api_avatar_reset(request):
    user, _ = await _auth(request)
    if not user:
        return _deny()
    try:
        os.remove(_av_custom(user["id"]))
    except OSError:
        pass
    return web.json_response({"ok": True, "av": av_url(user["id"]), "av_custom": False})


async def api_avatar(request):
    """Аватарка из Telegram (кэш на сервере). Ссылка подписана, чужие id перебрать нельзя."""
    try:
        uid = int(request.match_info["uid"])
    except ValueError:
        return web.Response(status=404)
    if not hmac.compare_digest(request.query.get("t", ""), av_sig(uid)):
        return web.Response(status=403)
    cust = _av_custom(uid)
    if os.path.exists(cust):   # фото, которое человек загрузил сам
        return web.FileResponse(cust, headers={"Cache-Control": "public, max-age=3600"})
    path = os.path.join(IMG_DIR, f"av_{uid}.jpg")
    none = path + ".none"
    now = time.time()
    have = os.path.exists(path)
    if not have or now - os.path.getmtime(path) > 86400:
        if not have and os.path.exists(none) and now - os.path.getmtime(none) < 21600:
            return web.Response(status=404)
        got = False
        with closing(db()) as c:
            row = c.execute("SELECT photo FROM users WHERE user_id=?", (uid,)).fetchone()
        if row and row[0] and str(row[0]).startswith("https://"):   # фото из мини-приложения (photo_url в initData)
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as sess:
                    async with sess.get(row[0]) as resp:
                        blob = await resp.content.read(2_000_000)
                if resp.status == 200 and blob[:2] in (b"\xff\xd8", b"\x89P"):
                    with open(path, "wb") as f:
                        f.write(blob)
                    got = True
            except Exception as e:
                logging.info("avatar url %s: %s", uid, e)
        for b in ([] if got else [bot] + ([seller_bot] if SELLER_READY else [])):
            try:
                ph = await b.get_user_profile_photos(uid, limit=1)
                if ph.total_count and ph.photos:
                    sizes = ph.photos[0]
                    await b.download(sizes[min(1, len(sizes) - 1)].file_id, destination=path)
                    got = True
                    break
            except Exception as e:
                logging.info("avatar %s: %s", uid, e)
        if not got and not have:
            open(none, "w").close()
            os.utime(none, None)
            return web.Response(status=404)
    return web.FileResponse(path, headers={"Cache-Control": "public, max-age=3600"})


def chat_messages(oid: int, uid: int, limit: int = 80) -> list:
    with closing(db()) as c:
        rows = c.execute("SELECT id,from_id,text,created,read FROM messages WHERE order_id=? ORDER BY id DESC LIMIT ?", (oid, limit)).fetchall()
        c.execute("UPDATE messages SET read=1 WHERE order_id=? AND to_id=?", (oid, uid))
        c.commit()
    return [{"id": r[0], "mine": r[1] == uid, "text": r[2], "time": time.strftime("%d.%m %H:%M", time.gmtime(r[3] + 7 * 3600)),
             "read": bool(r[4]), "av": av_url(r[1])}
            for r in reversed(rows)]


def reply_kb(oid: int) -> InlineKeyboardMarkup:
    return _kb(("✉️ Ответить", f"rp:{oid}"))


async def chat_send(oid: int, uid: int, text: str) -> tuple[bool, str]:
    o = get_order(oid)
    role = chat_role(o, uid)
    if not role or o["status"] not in CHAT_OPEN:
        return False, "Чат доступен по оплаченным заказам продавцов"
    text = _clip(text, 500)
    if not text:
        return False, "Напиши сообщение"
    if CONTACT_RE.search(text):
        return False, "Ссылки, @username и телефоны запрещены — общайтесь здесь, в чате заказа"
    if not rate_ok(uid, 20, 60):
        return False, "Слишком часто, подожди минуту"
    to = o["seller_id"] if role == "buyer" else o["user_id"]
    with closing(db()) as c:
        c.execute("INSERT INTO messages(order_id,from_id,to_id,text,created) VALUES(?,?,?,?,?)", (oid, uid, to, text, int(time.time())))
        c.commit()
    head = f"💬 Заказ #{oid} · {escape(o['label'])}\n"
    if role == "buyer":
        await notify_seller(to, head + f"Покупатель {escape(user_info(uid).split(' @')[0] or 'покупатель')}:\n{escape(text)}", reply_kb(oid))
    else:
        try:
            await bot.send_message(to, head + f"Продавец {escape(seller_name(uid) or 'продавец')}:\n{escape(text)}", reply_markup=reply_kb(oid))
        except Exception as e:
            logging.info("chat -> buyer %s: %s", to, e)
    return True, ""


def my_chats(uid: int) -> list:
    with closing(db()) as c:
        rows = c.execute("SELECT id,user_id,seller_id,label,game,status FROM orders WHERE (user_id=? OR seller_id=?) AND seller_id>0 "
                         "AND status IN ('paid','delivered','dispute','done') ORDER BY id DESC LIMIT 30", (uid, uid)).fetchall()
        out = []
        for oid, buyer, seller, label, game, st in rows:
            last = c.execute("SELECT text,created FROM messages WHERE order_id=? ORDER BY id DESC LIMIT 1", (oid,)).fetchone()
            unread = c.execute("SELECT COUNT(*) FROM messages WHERE order_id=? AND to_id=? AND read=0", (oid, uid)).fetchone()[0]
            peer = (seller_name(seller) or "Продавец") if uid == buyer else ((user_info(buyer).split(" @")[0]) or "Покупатель")
            out.append({"order": oid, "label": label, "game": game, "status": st, "role": "buyer" if uid == buyer else "seller",
                        "peer": peer, "last": last[0] if last else "", "unread": unread, "av": av_url(seller if uid == buyer else buyer)})
    return out


async def api_reviews(request):
    """GET /api/reviews?seller=0|ID — отзывы продавца (0 = сам магазин)."""
    try:
        sid = int(request.query.get("seller", "0"))
    except ValueError:
        sid = 0
    with closing(db()) as c:
        rows = c.execute("SELECT r.rating,r.text,r.created,r.user_id,o.label FROM reviews r LEFT JOIN orders o ON o.id=r.order_id "
                         "WHERE r.seller_id=? ORDER BY r.created DESC LIMIT 40", (sid,)).fetchall()
    avg, cnt = rating_of(sid)
    return web.json_response({"ok": True, "avg": avg, "count": cnt, "name": seller_name(sid) if sid else SHOP_NAME,
                              "verified": bool((seller_row(sid) or {}).get("verified")),
                              "reviews": [{"rating": r[0], "text": r[1], "date": fmt_time(r[2]).split(" ")[0],
                                           "name": (seller_name(r[3]).split(" ")[0] or "Покупатель")[:14], "label": r[4] or ""}
                                          for r in rows]})


async def api_review(request):
    user, data = await _auth(request)
    if not user:
        return _deny()
    try:
        oid, rating = int(data.get("order")), int(data.get("rating"))
    except (TypeError, ValueError):
        return web.json_response({"ok": False, "msg": "Поставь оценку"})
    o = get_order(oid)
    if not o or o["user_id"] != user["id"] or o["status"] != "done":
        return web.json_response({"ok": False, "msg": "Отзыв можно оставить после выполнения заказа"})
    if not 1 <= rating <= 5:
        return web.json_response({"ok": False, "msg": "Оценка от 1 до 5"})
    with closing(db()) as c:
        ins = c.execute("INSERT OR IGNORE INTO reviews(order_id,user_id,seller_id,target,rating,text,created) VALUES(?,?,?,?,?,?,?)",
                        (oid, user["id"], o["seller_id"], o["pack"], rating, _clip(data.get("text"), 400), int(time.time())))
        c.commit()
    if not ins.rowcount:
        return web.json_response({"ok": False, "msg": "Ты уже оставил отзыв на этот заказ"})
    target = o["seller_id"] or ADMIN_ID
    await (notify_seller if o["seller_id"] else safe_send)(target, f"⭐ Новый отзыв на заказ #{oid} ({escape(o['label'])}): {'★' * rating}{'☆' * (5 - rating)}\n"
                            f"{escape(_clip(data.get('text'), 400))}")
    if o["seller_id"]:
        await check_levelup(o["seller_id"])
        await suggest_verify(o["seller_id"])
    return web.json_response({"ok": True})


async def suggest_verify(sid: int):
    """Когда у продавца набралось отзывов и рейтинг высокий — один раз предлагаем админу выдать галочку."""
    sl = seller_row(sid)
    if not sl or sl["verified"]:
        return
    avg, cnt = rating_of(sid)
    with closing(db()) as c:
        if c.execute("SELECT verify_asked FROM sellers WHERE user_id=?", (sid,)).fetchone()[0]:
            return
        deals = c.execute("SELECT COUNT(*) FROM orders WHERE seller_id=? AND status='done'", (sid,)).fetchone()[0]
        if cnt < VERIFY_MIN_REVIEWS or avg < VERIFY_MIN_AVG:
            return
        c.execute("UPDATE sellers SET verify_asked=1, verify_req=CASE WHEN COALESCE(verify_req,0)=0 THEN ? ELSE verify_req END WHERE user_id=?", (int(time.time()), sid))
        c.commit()
    await bot.send_message(ADMIN_ID, f"🏅 Продавец <b>{escape(seller_name(sid))}</b> набрал {cnt} отзывов (★{avg}), сделок {deals}.\nВыдать галочку?",
                           reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✓ Выдать галочку", callback_data=f"vfy:{sid}")]]))


async def cb_verify(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    sid = int(callback.data.split(":")[1])
    with closing(db()) as c:
        c.execute("UPDATE sellers SET verified=1, verify_req=0 WHERE user_id=?", (sid,))
        c.commit()
    await notify_seller(sid, "✅ Тебе выдана галочка проверенного продавца — она видна рядом с именем в магазине!")
    await callback.message.edit_text(callback.message.html_text + "\n\n✅ Галочка выдана", reply_markup=None)
    await callback.answer("Готово")


def ad_rank(offer_id: int, game: str) -> int:
    """Место товара среди рекламируемых в своей игре (1 — самый верх). 0 — не рекламируется."""
    now = int(time.time())
    with closing(db()) as c:
        me = c.execute("SELECT boost_until,boost_at FROM offers WHERE id=?", (offer_id,)).fetchone()
        if not me or (me[0] or 0) <= now:
            return 0
        return 1 + c.execute("SELECT COUNT(*) FROM offers WHERE status='active' AND game=? AND boost_until>? AND (boost_at>? OR (boost_at=? AND id>?))",
                             (game, now, me[1] or 0, me[1] or 0, offer_id)).fetchone()[0]


VERIFY_RETRY_DAYS = 3


def verify_state(uid: int, sl: dict) -> dict:
    """Что показывать продавцу в профиле про галочку: verified | pending | cooldown | need_deal | can."""
    if sl.get("verified"):
        return {"state": "verified"}
    with closing(db()) as c:
        r = c.execute("SELECT COALESCE(verify_req,0),COALESCE(verify_rej,0) FROM sellers WHERE user_id=?", (uid,)).fetchone() or (0, 0)
        deals = c.execute("SELECT COUNT(*) FROM orders WHERE seller_id=? AND status='done'", (uid,)).fetchone()[0]
    if r[0]:
        return {"state": "pending"}
    left = r[1] + VERIFY_RETRY_DAYS * 86400 - time.time() if r[1] else 0
    if left > 0:
        return {"state": "cooldown", "days": int(left // 86400) + 1}
    if deals < 1:
        return {"state": "need_deal"}
    return {"state": "can"}


async def api_seller_verify_req(request):
    user, _ = await _auth(request)
    if not user:
        return _deny()
    uid = user["id"]
    sl = seller_row(uid)
    if not sl or sl["status"] != "approved":
        return web.json_response({"ok": False, "msg": "Доступно одобренным продавцам"})
    v = verify_state(uid, sl)
    if v["state"] != "can":
        msg = {"verified": "Галочка у тебя уже есть", "pending": "Заявка уже на рассмотрении", "need_deal": "Нужна хотя бы одна завершённая сделка",
               "cooldown": f"Повторно подать заявку можно через {v.get('days', 1)} дн."}[v["state"]]
        return web.json_response({"ok": False, "msg": msg, "verify": v})
    with closing(db()) as c:
        c.execute("UPDATE sellers SET verify_req=? WHERE user_id=?", (int(time.time()), uid))
        c.commit()
    avg, cnt = rating_of(uid)
    p = seller_payload(uid)
    try:
        await bot.send_message(ADMIN_ID, f"📨 <b>Заявка на галочку</b> от {escape(seller_name(uid))}\nСделок: {p['deals']} · отзывов: {cnt}" + (f" · ★{avg}" if cnt else "")
                               + "\nОткрой @azikaadmin_bot → «Заявки».")
    except Exception as e:
        logging.info("verify_req notify: %s", e)
    return web.json_response({"ok": True, "msg": "Заявка отправлена — мы ответим в этом боте", "verify": {"state": "pending"}})


ADMIN_API_PORT = int(os.getenv("ADMIN_API_PORT", "8082"))


async def api_admin_proxy(request):
    """Мини-приложение админа ходит через тот же HTTPS-адрес: пересылаем запрос админ-боту (он проверяет, что это ты)."""
    tail = request.match_info["tail"]
    if not re.fullmatch(r"[a-z_]{1,30}", tail):
        return web.Response(status=404)
    body = await request.read()
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25)) as sess:
            async with sess.post(f"http://127.0.0.1:{ADMIN_API_PORT}/api/{tail}", data=body, headers={"Content-Type": "application/json"}) as r:
                return web.Response(body=await r.read(), status=r.status, content_type="application/json")
    except Exception as e:
        logging.info("admin proxy: %s", e)
        return web.json_response({"ok": False, "msg": "Админ-бот не запущен на сервере"}, status=502)


def admin_unread(uid: int) -> int:
    with closing(db()) as c:
        return c.execute("SELECT COUNT(*) FROM admin_msgs WHERE seller_id=? AND from_admin=1 AND read=0", (uid,)).fetchone()[0]


async def api_seller_admin_chat(request):
    """Переписка продавца с администрацией (админ пишет первым через @azikaadmin_bot / мини-приложение)."""
    user, _ = await _auth(request)
    if not user:
        return _deny()
    uid = user["id"]
    if not seller_row(uid):
        return web.json_response({"ok": False, "msg": "Только для продавцов"})
    with closing(db()) as c:
        rows = c.execute("SELECT id,from_admin,text,created,read FROM admin_msgs WHERE seller_id=? ORDER BY id DESC LIMIT 80", (uid,)).fetchall()[::-1]
        c.execute("UPDATE admin_msgs SET read=1 WHERE seller_id=? AND from_admin=1 AND read=0", (uid,))
        c.commit()
    return web.json_response({"ok": True, "review": on_review(uid), **{"admin_" + k: v for k, v in seen_of(ADMIN_ID).items()},
                              "msgs": [{"id": r[0], "mine": not r[1], "text": r[2], "t": fmt_time(r[3]), "read": bool(r[4])} for r in rows]})


async def api_seller_admin_send(request):
    user, data = await _auth(request)
    if not user:
        return _deny()
    uid = user["id"]
    if not seller_row(uid):
        return web.json_response({"ok": False, "msg": "Только для продавцов"})
    text = str(data.get("text", "")).strip()[:1000]
    if not text:
        return web.json_response({"ok": False, "msg": "Напиши сообщение"})
    with closing(db()) as c:
        c.execute("INSERT INTO admin_msgs(seller_id,from_admin,text,created) VALUES(?,0,?,?)", (uid, text, int(time.time())))
        c.commit()
    try:
        await bot.send_message(ADMIN_ID, f"💬 <b>{escape(seller_name(uid) or str(uid))}</b> (продавец):\n{escape(text)}\n\nОтветь в @azikaadmin_bot → Продавцы → Чаты.")
    except Exception as e:
        logging.info("admin_send notify: %s", e)
    return web.json_response({"ok": True})


def seller_payload(uid: int) -> dict:
    sl = seller_row(uid)
    if not sl:
        return {"status": "none"}
    with closing(db()) as c:
        rows = c.execute(f"SELECT {OFFER_COLS} FROM offers WHERE seller_id=? AND status!='deleted' ORDER BY id DESC", (uid,)).fetchall()
        pend = c.execute("SELECT COALESCE(SUM(stars),0) FROM payouts WHERE seller_id=? AND status='pending'", (uid,)).fetchone()[0]
        deals = c.execute("SELECT COUNT(*) FROM orders WHERE seller_id=? AND status='done'", (uid,)).fetchone()[0]
        _fee = seller_fee(uid)
        escrow = sum(r[0] * (100 - _fee) // 100 for r in c.execute(
            "SELECT stars FROM orders WHERE seller_id=? AND status IN ('paid','delivered','dispute')", (uid,)))
    avg, cnt = rating_of(uid)
    return {"id": uid, "av": av_url(uid), "av_custom": os.path.exists(_av_custom(uid)), "status": sl["status"], "balance": sl["balance"], "earned": sl["earned"], "pending_payout": pend,
            "deals": deals, "avg": avg, "reviews": cnt, "fee": seller_fee(uid), "level": seller_level(uid), "offers_used": sum(1 for o in rows if o[6] in ("pending", "active", "hidden")), "payout_min": PAYOUT_MIN,
            "seller_bot": SELLER_BOT_USERNAME, "ad_price": AD_PRICE, "verified": bool(sl.get("verified")), "verify": verify_state(uid, sl),
            "escrow": escrow, "auto_days": AUTO_CONFIRM_DAYS, "name": seller_name(uid),
            "unread": sum(c["unread"] for c in my_chats(uid)), "review": on_review(uid), "admin_unread": admin_unread(uid),
            "offers": [{"id": f"o{o[0]}", "game": o[2], "title": o[3], "desc": o[4], "stars": o[5], "status": o[6], "sold": o[8],
                        "ad": (o[9] or 0) > time.time(), "rank": ad_rank(o[0], o[2])} for o in rows]}


async def api_seller_me(request):
    user, _ = await _auth(request)
    if not user:
        return _deny()
    return web.json_response({"ok": True, **seller_payload(user["id"])})


async def api_seller_apply(request):
    user, data = await _auth(request)
    if not user:
        return _deny()
    ok, msg = await submit_application(user["id"], data.get("about"))
    return web.json_response({"ok": ok, "msg": msg})


async def api_seller_offer(request):
    user, data = await _auth(request)
    if not user:
        return _deny()
    ok, msg, oid = await create_offer(user["id"], str(data.get("game", "")), data.get("title"), data.get("desc"), data.get("stars"))
    if ok:
        await notify_seller(user["id"], f"📦 Товар <b>{escape(oid)}</b> отправлен на проверку.\n"
                                        f"Хочешь фото? Пришли сюда картинку с подписью <code>/offerimg {oid[1:]}</code>.")
    return web.json_response({"ok": ok, "msg": msg, "id": oid})


async def api_seller_offer_set(request):
    """Скрыть / показать / удалить своё объявление."""
    user, data = await _auth(request)
    if not user:
        return _deny()
    o = offer_get(str(data.get("id", "")))
    action = str(data.get("action", ""))
    if not o or o["seller_id"] != user["id"] or action not in ("hide", "show", "delete"):
        return web.json_response({"ok": False, "msg": "Не получилось"})
    new = {"hide": "hidden", "show": "active", "delete": "deleted"}[action]
    if o["status"] in ("pending", "rejected", "deleted") and action == "show":
        return web.json_response({"ok": False, "msg": "Товар ещё не одобрен"})
    with closing(db()) as c:
        c.execute("UPDATE offers SET status=? WHERE id=?", (new, o["id"]))
        c.commit()
    return web.json_response({"ok": True})


async def api_seller_payout(request):
    user, data = await _auth(request)
    if not user:
        return _deny()
    ok, msg = await request_payout(user["id"], data.get("stars"), data.get("details"))
    return web.json_response({"ok": ok, "msg": msg})



async def api_chat_orders(request):
    user, _ = await _auth(request)
    if not user:
        return _deny()
    return web.json_response({"ok": True, "chats": my_chats(user["id"])})


async def api_chat_messages(request):
    user, data = await _auth(request)
    if not user:
        return _deny()
    try:
        oid = int(data.get("order"))
    except (TypeError, ValueError):
        return web.json_response({"ok": False, "msg": "Нет заказа"})
    o = get_order(oid)
    if not chat_role(o, user["id"]):
        return web.json_response({"ok": False, "msg": "Нет доступа"})
    peer = (seller_name(o["seller_id"]) or "Продавец") if user["id"] == o["user_id"] else ((user_info(o["user_id"]).split(" @")[0]) or "Покупатель")
    return web.json_response({"ok": True, "peer": peer, "label": o["label"], "open": o["status"] in CHAT_OPEN,
                              "peer_av": av_url(o["seller_id"] if user["id"] == o["user_id"] else o["user_id"]), "me_av": av_url(user["id"]),
                              **{"peer_" + k: v for k, v in seen_of(o["seller_id"] if user["id"] == o["user_id"] else o["user_id"]).items()},
                              "messages": chat_messages(oid, user["id"])})


async def api_chat_send(request):
    user, data = await _auth(request)
    if not user:
        return _deny()
    try:
        oid = int(data.get("order"))
    except (TypeError, ValueError):
        return web.json_response({"ok": False, "msg": "Нет заказа"})
    ok, msg = await chat_send(oid, user["id"], data.get("text"))
    return web.json_response({"ok": ok, "msg": msg})



async def api_order_confirm(request):
    user, data = await _auth(request)
    if not user:
        return _deny()
    try:
        oid = int(data.get("order"))
    except (TypeError, ValueError):
        return web.json_response({"ok": False, "msg": "Нет заказа"})
    ok, msg = await process_confirm(oid, user["id"])
    return web.json_response({"ok": ok, "msg": msg})


async def api_order_dispute(request):
    user, data = await _auth(request)
    if not user:
        return _deny()
    try:
        oid = int(data.get("order"))
    except (TypeError, ValueError):
        return web.json_response({"ok": False, "msg": "Нет заказа"})
    ok, msg = await process_dispute(oid, user["id"], str(data.get("code", "other")), data.get("text", ""))
    return web.json_response({"ok": ok, "msg": msg})


async def api_seller_orders(request):
    user, _ = await _auth(request)
    if not user:
        return _deny()
    uid = user["id"]
    with closing(db()) as c:
        rows = c.execute(f"SELECT {ORDER_COLS} FROM orders WHERE seller_id=? AND status!='expired' ORDER BY id DESC LIMIT 60", (uid,)).fetchall()
        unread = dict(c.execute("SELECT order_id,COUNT(*) FROM messages WHERE to_id=? AND read=0 GROUP BY order_id", (uid,)).fetchall())
    now = time.time()
    out = []
    for r in rows:
        o = row_to_order(r)
        out.append({"id": o["id"], "label": o["label"], "game": o["game"], "player": o["player"], "comment": o["comment"],
                    "stars": o["stars"], "gain": o["stars"] * (100 - seller_fee(uid)) // 100, "status": o["status"],
                    "status_text": STATUS_TXT.get(o["status"], o["status"]), "created": fmt_time(o["created"]),
                    "unread": unread.get(o["id"], 0), "chat": o["status"] in CHAT_OPEN,
                    "auto_in": max(0, int(o["delivered_at"] + AUTO_CONFIRM_DAYS * 86400 - now)) if o["status"] == "delivered" else 0})
    return web.json_response({"ok": True, "orders": out, "auto_days": AUTO_CONFIRM_DAYS})


async def api_seller_deliver(request):
    user, data = await _auth(request)
    if not user:
        return _deny()
    try:
        oid = int(data.get("order"))
    except (TypeError, ValueError):
        return web.json_response({"ok": False, "msg": "Нет заказа"})
    ok, msg = await process_deliver(oid, user["id"])
    return web.json_response({"ok": ok, "msg": msg})


def _need_seller(uid: int):
    sl = seller_row(uid)
    return sl if sl and sl["status"] == "approved" else None


async def api_seller_promos(request):
    user, _ = await _auth(request)
    if not user:
        return _deny()
    with closing(db()) as c:
        rows = c.execute("SELECT code,pct,used,max_uses,expires FROM promos WHERE seller_id=? ORDER BY rowid DESC", (user["id"],)).fetchall()
    return web.json_response({"ok": True, "max_pct": SELLER_PROMO_MAX, "promos": [
        {"code": r[0], "pct": r[1], "used": r[2], "max": r[3], "until": fmt_time(r[4]) if r[4] else ""} for r in rows]})


async def api_seller_promo_new(request):
    user, data = await _auth(request)
    if not user:
        return _deny()
    ok, msg = create_seller_promo(user["id"], data.get("code"), data.get("pct"), data.get("max"), data.get("days"))
    return web.json_response({"ok": ok, "msg": msg})


async def api_seller_promo_del(request):
    user, data = await _auth(request)
    if not user:
        return _deny()
    with closing(db()) as c:
        c.execute("DELETE FROM promos WHERE code=? AND seller_id=?", (str(data.get("code", "")).upper(), user["id"]))
        c.commit()
    return web.json_response({"ok": True})


async def api_seller_ad(request):
    user, data = await _auth(request)
    if not user:
        return _deny()
    ok, msg = buy_ad(user["id"], str(data.get("id", "")), data.get("days"))
    return web.json_response({"ok": ok, "msg": msg})


async def api_seller_offer_edit(request):
    """Цена меняется сразу; название/описание — через повторную проверку админом."""
    user, data = await _auth(request)
    if not user:
        return _deny()
    o = offer_get(str(data.get("id", "")))
    if not o or o["seller_id"] != user["id"] or o["status"] == "deleted":
        return web.json_response({"ok": False, "msg": "Товар не найден"})
    title, descr = _clip(data.get("title", o["title"]), 60), _clip(data.get("desc", o["descr"]), 300)
    try:
        stars = int(data.get("stars", o["stars"]))
    except (TypeError, ValueError):
        stars = 0
    if len(title) < 3 or not 5 <= stars <= 200000:
        return web.json_response({"ok": False, "msg": "Название от 3 символов, цена от 5 ⭐"})
    text_changed = (title, descr) != (o["title"], o["descr"])
    status = "pending" if text_changed and o["status"] in ("active", "hidden") else o["status"]
    with closing(db()) as c:
        c.execute("UPDATE offers SET title=?,descr=?,stars=?,status=? WHERE id=?", (title, descr, stars, status, o["id"]))
        c.commit()
    if status == "pending" and o["status"] != "pending":
        await bot.send_message(ADMIN_ID, f"✏️ <b>Товар #{o['id']} изменён</b> — нужна повторная проверка\n{escape(title)} · {stars}⭐\n{escape(descr)}",
                               reply_markup=_kb(("✅ Опубликовать", f"ofok:{o['id']}"), ("❌ Отклонить", f"ofno:{o['id']}")))
    return web.json_response({"ok": True, "status": status})


async def api_seller_offer_img(request):
    """Фото товара из мини-приложения: JPEG в base64 (приложение уменьшает его заранее)."""
    user, data = await _auth(request)
    if not user:
        return _deny()
    o = offer_get(str(data.get("id", "")))
    if not o or o["seller_id"] != user["id"]:
        return web.json_response({"ok": False, "msg": "Товар не найден"})
    import base64
    raw = str(data.get("data", ""))
    try:
        blob = base64.b64decode(raw.split(",")[-1], validate=False)
    except Exception:
        return web.json_response({"ok": False, "msg": "Не удалось прочитать фото"})
    if len(blob) < 500 or len(blob) > 3_000_000 or blob[:2] != b"\xff\xd8":
        return web.json_response({"ok": False, "msg": "Нужен JPEG до 3 МБ"})
    tmp = os.path.join(IMG_DIR, f"_up{user['id']}.jpg")
    with open(tmp, "wb") as f:
        f.write(blob)
    try:
        save_img_record([f"o{o['id']}"], tmp)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    return web.json_response({"ok": True})


async def start_api():
    app = web.Application(middlewares=[cors_mw], client_max_size=4 * 1024 * 1024)
    app.router.add_get("/api/ping", api_ping)
    app.router.add_get("/api/catalog", api_catalog)
    app.router.add_get("/api/img/{key}", api_img)
    app.router.add_route("*", "/api/profile", api_profile)
    app.router.add_route("*", "/api/orders", api_orders)
    app.router.add_route("*", "/api/promo", api_promo)
    app.router.add_route("*", "/api/quote", api_quote)
    app.router.add_route("*", "/api/invoice", api_invoice)
    app.router.add_get("/api/reviews", api_reviews)
    app.router.add_route("*", "/api/review", api_review)
    app.router.add_route("*", "/api/seller/me", api_seller_me)
    app.router.add_route("*", "/api/seller/verify_req", api_seller_verify_req)
    app.router.add_route("*", "/api/seller/admin_chat", api_seller_admin_chat)
    app.router.add_route("*", "/api/seller/admin_send", api_seller_admin_send)
    app.router.add_route("*", "/api/admin/{tail}", api_admin_proxy)
    app.router.add_route("*", "/api/seller/apply", api_seller_apply)
    app.router.add_route("*", "/api/seller/offer", api_seller_offer)
    app.router.add_route("*", "/api/seller/offer_set", api_seller_offer_set)
    app.router.add_route("*", "/api/seller/payout", api_seller_payout)
    app.router.add_route("*", "/api/order/confirm", api_order_confirm)
    app.router.add_route("*", "/api/order/dispute", api_order_dispute)
    app.router.add_route("*", "/api/seller/orders", api_seller_orders)
    app.router.add_route("*", "/api/seller/deliver", api_seller_deliver)
    app.router.add_route("*", "/api/seller/promos", api_seller_promos)
    app.router.add_route("*", "/api/seller/promo_new", api_seller_promo_new)
    app.router.add_route("*", "/api/seller/promo_del", api_seller_promo_del)
    app.router.add_route("*", "/api/seller/ad", api_seller_ad)
    app.router.add_route("*", "/api/seller/offer_edit", api_seller_offer_edit)
    app.router.add_route("*", "/api/seller/offer_img", api_seller_offer_img)
    app.router.add_get("/api/avatar/{uid}", api_avatar)
    app.router.add_route("*", "/api/avatar_set", api_avatar_set)
    app.router.add_route("*", "/api/avatar_reset", api_avatar_reset)
    app.router.add_route("*", "/api/chat/orders", api_chat_orders)
    app.router.add_route("*", "/api/chat/messages", api_chat_messages)
    app.router.add_route("*", "/api/chat/send", api_chat_send)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, API_HOST, API_PORT).start()
    print(f"🌐 API магазина: порт {API_PORT} → {API_URL or '(API_URL не задан)'}")



# ==========================================
# 🤖 БОТ ПРОДАВЦОВ «Azika Sellers»
# ==========================================
ST: dict[int, dict] = {}   # шаги мастеров (в памяти)
OFFER_ST = {"active": "в продаже", "pending": "на проверке", "hidden": "скрыт", "rejected": "отклонён"}


def skb(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in r] for r in rows])


S_MENU = skb([("📦 Мои товары", "s:offers"), ("➕ Добавить товар", "s:add")],
             [("🎟 Промокоды", "s:promos"), ("📢 Реклама", "s:ads")],
             [("💬 Чаты", "s:chats"), ("💰 Баланс и вывод", "s:bal")],
             [("⭐ Отзывы", "s:rev")])
S_BACK = skb([("« Меню", "s:menu")])
FX = {"party": "5046509860389126442", "fire": "5104841245755180586", "like": "5107584321108051014"}   # эффекты сообщений Telegram


async def fx_answer(message, text: str, kb=None, fx: str = "party"):
    """Ответ с эффектом (конфетти/огонь) — если клиент/версия не поддерживает, просто обычное сообщение."""
    try:
        return await message.answer(text, reply_markup=kb, message_effect_id=FX[fx])
    except Exception:
        return await message.answer(text, reply_markup=kb)


async def animate_intro(message, name: str):
    """Короткая анимация приветствия: сообщение «собирается» кадрами через редактирование."""
    try:
        await message.bot.send_chat_action(message.chat.id, "typing")
        frames = ["🟩⬜⬜⬜⬜  <b>Azika Seller</b>", "🟩🟩⬜⬜⬜  <b>Azika Seller</b>\nзагружаю кабинет…", "🟩🟩🟩⬜⬜  <b>Azika Seller</b>\nпроверяю заказы…",
                  "🟩🟩🟩🟩⬜  <b>Azika Seller</b>\nсчитаю баланс…", "🟩🟩🟩🟩🟩  <b>Azika Seller</b>\nготово ✨"]
        m = await message.answer(frames[0])
        for f in frames[1:]:
            await asyncio.sleep(0.45)
            await m.edit_text(f)
        await asyncio.sleep(0.5)
        await m.edit_text(f"👋 <b>С возвращением, {escape(name)}!</b>")
    except Exception as e:
        logging.info("intro anim: %s", e)


def s_menu_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(inline_keyboard=[r[:] for r in S_MENU.inline_keyboard])
    if SELLER_WEBAPP_URL:
        kb.inline_keyboard.insert(0, [InlineKeyboardButton(text="🚀 Открыть Azika Seller", web_app=WebAppInfo(url=SELLER_WEBAPP_URL))])
    kb.inline_keyboard.append([InlineKeyboardButton(text="🆘 Поддержка", url=support_url("seller"))])
    return kb


async def out(ev, text: str, kb=None):
    """Ответ в боте продавцов: на кнопку — правим сообщение, на текст — отвечаем новым."""
    is_cb = hasattr(ev, "data")
    msg = ev.message if is_cb else ev
    try:
        await (msg.edit_text if is_cb else msg.answer)(text, reply_markup=kb)
    except Exception:
        await msg.answer(text, reply_markup=kb)


async def s_home(ev, uid: int):
    sl = seller_row(uid)
    avg, cnt = rating_of(uid)
    unread = sum(c["unread"] for c in my_chats(uid))
    await out(ev, f"🧑‍💼 <b>Кабинет продавца</b>{' ✓' if sl.get('verified') else ''}\nБаланс: <b>{sl['balance']}⭐</b> · рейтинг: {('★ ' + str(avg) + f' ({cnt})') if cnt else 'нет отзывов'}"
                  f"{f' · 💬 новых: {unread}' if unread else ''}\nКомиссия магазина {seller_level(uid)['fee']}% · {seller_level(uid)['name']}.", s_menu_kb())


async def s_start(message: Message):
    u = message.from_user
    ensure_user(u.id, u.first_name, u.username)
    ST.pop(u.id, None)
    sl = seller_row(u.id)
    if not sl:
        await message.answer(f"👋 Это бот продавцов <b>{SHOP_NAME}</b>.\nЗдесь можно выставлять свои товары для игр, делать промокоды, "
                             f"рекламировать товары и общаться с покупателями. Комиссия зависит от уровня: от {min(_FEES)}% до {max(_FEES)}%.\n\nЧтобы начать — подай заявку:",
                             reply_markup=skb([("📝 Подать заявку", "s:apply")]))
    elif sl["status"] == "pending":
        await message.answer("⏳ Заявка на рассмотрении. Я напишу, когда её проверят.")
    elif sl["status"] == "banned":
        await message.answer("Продажи для тебя закрыты.")
    else:
        await animate_intro(message, u.first_name or "продавец")
        await s_home(message, u.id)


def _offer_card(o: dict) -> tuple[str, InlineKeyboardMarkup]:
    ad = f"\n📢 Реклама до {fmt_time(o['boost_until'])}" if (o["boost_until"] or 0) > time.time() else ""
    t = (f"<b>#{o['id']} {escape(o['title'])}</b>\n{escape(GAME_BY_ID.get(o['game'], {}).get('name', o['game']))} · {o['stars']}⭐ · "
         f"{OFFER_ST.get(o['status'], o['status'])} · продано {o['sold']}{ad}\n{escape(o['descr'] or '')}")
    row1 = []
    if o["status"] == "active":
        row1 = [("🙈 Скрыть", f"s:oh:{o['id']}"), ("📢 Реклама", f"s:ad:{o['id']}")]
    elif o["status"] == "hidden":
        row1 = [("👁 Показать", f"s:os:{o['id']}")]
    return t, skb(row1, [("🖼 Фото", f"s:ph:{o['id']}"), ("🗑 Удалить", f"s:od:{o['id']}")], [("« Товары", "s:offers")])


async def s_cb(cb: CallbackQuery):
    uid = cb.from_user.id
    parts = cb.data.split(":")[1:]
    act = parts[0]
    sl = seller_row(uid)
    if act == "apply":
        if sl:
            await cb.answer("Заявка уже есть", show_alert=True)
            return
        ST[uid] = {"step": "apply"}
        await out(cb, "Расскажи коротко о себе и что собираешься продавать (от 10 символов):", S_BACK)
        await cb.answer()
        return
    if not sl or sl["status"] != "approved":
        await cb.answer("Доступно после одобрения заявки", show_alert=True)
        return
    ST.pop(uid, None)
    if act == "menu":
        chat_set("s", uid, 0)
        await s_home(cb, uid)
    elif act == "offers":
        with closing(db()) as c:
            rows = c.execute(f"SELECT {OFFER_COLS} FROM offers WHERE seller_id=? AND status!='deleted' ORDER BY id DESC LIMIT 30", (uid,)).fetchall()
        offers = [dict(zip(OFFER_COLS.split(","), r)) for r in rows]
        kb = skb(*[[(f"#{o['id']} {o['title'][:24]} · {o['stars']}⭐ · {OFFER_ST.get(o['status'], '')}", f"s:o:{o['id']}")] for o in offers],
                 [("➕ Добавить", "s:add"), ("« Меню", "s:menu")])
        await out(cb, "📦 <b>Мои товары</b>" + ("" if offers else "\nПока пусто — добавь первый."), kb)
    elif act in ("o", "oh", "os", "od", "ph"):
        o = offer_get(parts[1])
        if not o or o["seller_id"] != uid or o["status"] == "deleted":
            await cb.answer("Товар не найден", show_alert=True)
            return
        if act == "ph":
            ST[uid] = {"step": "photo", "oid": o["id"]}
            await out(cb, f"🖼 Пришли картинку для товара #{o['id']} (одним сообщением, как фото).", S_BACK)
            await cb.answer()
            return
        if act in ("oh", "os", "od"):
            with closing(db()) as c:
                c.execute("UPDATE offers SET status=? WHERE id=?", ({"oh": "hidden", "os": "active", "od": "deleted"}[act], o["id"]))
                c.commit()
            if act == "od":
                await cb.answer("Удалено")
                cb.data = "s:offers"
                await s_cb(cb)
                return
            o = offer_get(parts[1])
        t, kb = _offer_card(o)
        await out(cb, t, kb)
    elif act == "add":
        await out(cb, "➕ <b>Новый товар</b>\nВыбери игру:",
                  skb(*[[(g["name"], f"s:ag:{g['id']}")] for g in GAMES], [("« Меню", "s:menu")]))
    elif act == "ag":
        if parts[1] not in GAME_BY_ID:
            await cb.answer()
            return
        ST[uid] = {"step": "title", "game": parts[1]}
        await out(cb, f"{GAME_BY_ID[parts[1]]['name']}. Напиши <b>название товара</b> (например: «500 гемов за 10 минут»):", S_BACK)
    elif act == "promos":
        with closing(db()) as c:
            rows = c.execute("SELECT code,pct,used,max_uses,expires FROM promos WHERE seller_id=? ORDER BY rowid DESC", (uid,)).fetchall()
        lines = [f"<code>{r[0]}</code> −{r[1]}% · использовано {r[2]}{'/' + str(r[3]) if r[3] else ''}"
                 f"{' · до ' + fmt_time(r[4]) if r[4] else ''}" for r in rows]
        await out(cb, "🎟 <b>Промокоды</b>\nСкидка действует только на твои товары и оплачивается из твоей цены.\n\n" + ("\n".join(lines) or "Пока нет."),
                  skb(*[[(f"🗑 {r[0]}", f"s:pd:{r[0]}")] for r in rows[:10]], [("➕ Создать", "s:padd"), ("« Меню", "s:menu")]))
    elif act == "padd":
        ST[uid] = {"step": "pcode"}
        await out(cb, "Придумай <b>код</b> (латиница/цифры, 3–16 символов), например <code>DANIL10</code>:", S_BACK)
    elif act == "pd":
        with closing(db()) as c:
            c.execute("DELETE FROM promos WHERE code=? AND seller_id=?", (parts[1], uid))
            c.commit()
        await cb.answer("Удалён")
        cb.data = "s:promos"
        await s_cb(cb)
        return
    elif act == "ads":
        with closing(db()) as c:
            rows = c.execute(f"SELECT {OFFER_COLS} FROM offers WHERE seller_id=? AND status='active' ORDER BY id DESC LIMIT 20", (uid,)).fetchall()
        offers = [dict(zip(OFFER_COLS.split(","), r)) for r in rows]
        await out(cb, f"📢 <b>Реклама товаров</b>\nРекламируемый товар встаёт <b>на 1 место</b> в списке своей игры с меткой «Реклама». "
                      f"Если другой продавец купит рекламу после тебя — он станет первым, а ты сдвинешься на 2 место, следующий — на 3 и так далее. "
                      f"Чтобы вернуться наверх, продли или купи рекламу снова. "
                      f"Цена: <b>{AD_PRICE}⭐ в сутки</b> с твоего баланса ({sl['balance']}⭐).\n\nВыбери товар:",
                  skb(*[[(("📢 " if (o["boost_until"] or 0) > time.time() else "") + f"#{o['id']} {o['title'][:28]}", f"s:ad:{o['id']}")] for o in offers],
                      [("« Меню", "s:menu")]))
    elif act == "ad":
        o = offer_get(parts[1])
        if not o or o["seller_id"] != uid or o["status"] != "active":
            await cb.answer("Рекламировать можно только опубликованный товар", show_alert=True)
            return
        await out(cb, f"📢 Реклама «{escape(o['title'])}»\nНа сколько суток? ({AD_PRICE}⭐/сут, баланс {sl['balance']}⭐)",
                  skb([(f"1 сутки · {AD_PRICE}⭐", f"s:adb:{o['id']}:1"), (f"3 · {AD_PRICE * 3}⭐", f"s:adb:{o['id']}:3"),
                       (f"7 · {AD_PRICE * 7}⭐", f"s:adb:{o['id']}:7")], [("« Реклама", "s:ads")]))
    elif act == "adb":
        ok, msg = buy_ad(uid, f"o{parts[1]}", parts[2])
        await cb.answer(("✅ Реклама запущена " if ok else "⚠️ ") + msg, show_alert=True)
        cb.data = "s:ads"
        await s_cb(cb)
        return
    elif act == "chats":
        chats = [c for c in my_chats(uid) if c["role"] == "seller"]
        kb = skb(*[[(f"{'🔴 ' if c['unread'] else ''}#{c['order']} {c['peer']} · {c['label'][:18]}", f"s:c:{c['order']}")] for c in chats[:15]],
                 [("« Меню", "s:menu")])
        await out(cb, "💬 <b>Чаты с покупателями</b>\nВыбери заказ, затем пиши — сообщения уйдут покупателю. Контакты и ссылки запрещены." +
                  ("" if chats else "\nПока нет оплаченных заказов."), kb)
    elif act == "c":
        o = get_order(int(parts[1]))
        if chat_role(o, uid) != "seller":
            await cb.answer("Нет доступа", show_alert=True)
            return
        chat_set("s", uid, o["id"])
        msgs = chat_messages(o["id"], uid, 10)
        hist = "\n".join(f"{'Ты' if m['mine'] else 'Покупатель'}: {escape(m['text'])}" for m in msgs) or "Сообщений пока нет."
        await out(cb, f"💬 Заказ #{o['id']} · {escape(o['label'])}\nИгрок: <code>{escape(o['player'])}</code>\n\n{hist}\n\n"
                      "✍️ Пиши сообщение — оно уйдёт покупателю. Выйти: /stop", skb([("« Чаты", "s:chats")]))
    elif act == "bal":
        with closing(db()) as c:
            pend = c.execute("SELECT COALESCE(SUM(stars),0) FROM payouts WHERE seller_id=? AND status='pending'", (uid,)).fetchone()[0]
        await out(cb, f"💰 <b>Баланс</b>: {sl['balance']}⭐\nВсего заработано: {sl['earned']}⭐" + (f"\nНа выводе: {pend}⭐" if pend else "") +
                  f"\nМинимальный вывод {PAYOUT_MIN}⭐. Выплачивает админ вручную.",
                  skb([("💸 Вывести", "s:pay")], [("« Меню", "s:menu")]))
    elif act == "pay":
        ST[uid] = {"step": "pay_amt"}
        await out(cb, f"💸 Сколько ⭐ вывести? (от {PAYOUT_MIN}, доступно {sl['balance']}):", S_BACK)
    elif act == "rev":
        with closing(db()) as c:
            rows = c.execute("SELECT rating,text,created FROM reviews WHERE seller_id=? ORDER BY created DESC LIMIT 8", (uid,)).fetchall()
        avg, cnt = rating_of(uid)
        await out(cb, f"⭐ <b>Отзывы</b>: {('★ ' + str(avg) + f' · {cnt} шт.') if cnt else 'пока нет'}\n\n" +
                  "\n\n".join(f"{'★' * r[0]}{'☆' * (5 - r[0])} {fmt_time(r[2]).split(' ')[0]}\n{escape(r[1] or '')}" for r in rows), S_BACK)
    try:
        await cb.answer()
    except Exception:
        pass


async def s_text(message: Message):
    uid, text = message.from_user.id, (message.text or "").strip()
    st = ST.get(uid)
    if not st:
        if await relay_text(message, "s"):
            return
        sl = seller_row(uid)
        if sl and sl["status"] == "approved":
            await s_home(message, uid)
        else:
            await s_start(message)
            await message.answer("🆘 Вопрос по покупке или по заявке? Пиши в поддержку:",
                                 reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💬 Поддержка", url=support_url("seller"))]]))
        return
    step = st["step"]
    if step == "apply":
        ok, msg = await submit_application(uid, text)
        ST.pop(uid, None)
        await message.answer("✅ Заявка отправлена админу. Я напишу, когда её проверят." if ok else "⚠️ " + msg)
    elif step == "title":
        st.update(step="desc", title=text)
        await message.answer("Напиши <b>описание</b> (что входит, как выдаёшь, сроки) или «-», чтобы пропустить:", reply_markup=S_BACK)
    elif step == "desc":
        st.update(step="price", desc="" if text == "-" else text)
        await message.answer("Цена в ⭐ (числом):", reply_markup=S_BACK)
    elif step == "price":
        ok, msg, oid = await create_offer(uid, st["game"], st["title"], st.get("desc", ""), re.sub(r"\D", "", text) or 0)
        if ok:
            ST.pop(uid, None)
            await message.answer(f"✅ Товар <b>{escape(oid)}</b> отправлен на проверку админу.\n"
                                 f"Фото можно добавить в «Мои товары» → товар → «Фото».", reply_markup=s_menu_kb())
        else:
            await message.answer("⚠️ " + msg + "\nПришли цену ещё раз или нажми «Меню».", reply_markup=S_BACK)
    elif step == "pcode":
        if not re.fullmatch(r"[A-Za-z0-9_-]{3,16}", text):
            await message.answer("⚠️ Код: 3–16 символов, латиница, цифры, _ или -. Попробуй ещё раз:")
            return
        st.update(step="ppct", code=text.upper())
        await message.answer(f"Скидка в процентах (1–{SELLER_PROMO_MAX}):")
    elif step == "ppct":
        st.update(step="pmax", pct=re.sub(r"\D", "", text) or 0)
        await message.answer("Сколько раз можно использовать? (0 — без ограничений):")
    elif step == "pmax":
        st.update(step="pdays", max=re.sub(r"\D", "", text) or 0)
        await message.answer("Сколько дней действует? (0 — бессрочно):")
    elif step == "pdays":
        ok, msg = create_seller_promo(uid, st["code"], st["pct"], st["max"], re.sub(r"\D", "", text) or 0)
        ST.pop(uid, None)
        await message.answer(f"✅ Промокод <code>{st['code']}</code> создан: −{st['pct']}% на твои товары. Расскажи его клиентам!" if ok
                             else "⚠️ " + msg, reply_markup=s_menu_kb())
    elif step == "pay_amt":
        st.update(step="pay_det", amount=re.sub(r"\D", "", text) or 0)
        await message.answer("Куда выплатить? (карта, кошелёк или @username):")
    elif step == "pay_det":
        ok, msg = await request_payout(uid, st["amount"], text)
        ST.pop(uid, None)
        await message.answer("✅ Запрос отправлен админу, выплата вручную." if ok else "⚠️ " + msg, reply_markup=s_menu_kb())


async def s_photo(message: Message):
    uid = message.from_user.id
    st = ST.get(uid)
    cap = (message.caption or "").split()
    if st and st.get("step") == "photo":
        ST.pop(uid, None)
        await message.answer(await save_offer_photo(message, st["oid"]), reply_markup=s_menu_kb())
    elif len(cap) == 2 and cap[0].startswith("/offerimg") and cap[1].isdigit():
        await message.answer(await save_offer_photo(message, cap[1]))
    else:
        await message.answer("Чтобы добавить фото: «Мои товары» → товар → «Фото».")


async def s_stop(message: Message):
    ST.pop(message.from_user.id, None)
    chat_set("s", message.from_user.id, 0)
    await message.answer("Ок, вышел из режима.", reply_markup=S_BACK)


def register_handlers():
    """Подключаем обработчики основного бота и бота продавцов (порядок важен: общий текст — последним)."""
    dp.callback_query.register(cb_done, F.data.startswith("done:"))
    dp.callback_query.register(cb_cancel, F.data.startswith("cancel:"))
    dp.callback_query.register(cb_reply, F.data.startswith("rp:"))
    dp.callback_query.register(cb_verify, F.data.startswith("vfy:"))
    dp.callback_query.register(cb_buyer, F.data.regexp(r"^(bok|bbad|bback):\d+$"))
    dp.callback_query.register(cb_why, F.data.regexp(r"^bwhy:\d+:\w+$"))
    dp.callback_query.register(cb_deliver, F.data.startswith("dlv:"))
    dp.message.register(seller_offerimg, F.photo, F.caption.regexp(r"^/offerimg(@\w+)?\s+\d+"))
    dp.message.register(cmd_stop, Command("stop"))
    dp.message.register(main_text, F.text, ~F.text.startswith("/"))
    dp2.callback_query.register(cb_done, F.data.startswith("done:"))
    dp2.callback_query.register(cb_cancel, F.data.startswith("cancel:"))
    dp2.callback_query.register(cb_deliver, F.data.startswith("dlv:"))
    dp2.callback_query.register(cb_reply_seller, F.data.startswith("rp:"))
    dp2.callback_query.register(s_cb, F.data.startswith("s:"))
    dp2.message.register(s_start, CommandStart())
    dp2.message.register(s_start, Command("menu"))
    dp2.message.register(s_stop, Command("stop"))
    dp2.message.register(s_photo, F.photo)
    dp2.message.register(s_text, F.text, ~F.text.startswith("/"))


async def main():
    global BOT_USERNAME, SELLER_BOT_USERNAME, SELLER_READY
    if not BOT_TOKEN:
        raise SystemExit("Задай переменную окружения BOT_TOKEN")
    init_db()
    await bot.delete_webhook(drop_pending_updates=False)
    me = await bot.get_me()
    BOT_USERNAME = me.username or ""
    try:
        url = webapp_link()
        if url:
            await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="🛒 Магазин", web_app=WebAppInfo(url=url)))
        else:
            await bot.set_chat_menu_button(menu_button=MenuButtonDefault())
    except Exception as e:
        logging.warning("menu button: %s", e)
    try:
        await bot.set_my_commands([BotCommand(command="orders", description="📦 Мои заказы"), BotCommand(command="level", description="💸 Мой уровень кэшбэка"),
                                   BotCommand(command="ref", description="👥 Пригласить друзей"), BotCommand(command="support", description="🆘 Поддержка")])
    except Exception as e:
        logging.warning("set_my_commands: %s", e)
    register_handlers()
    seller_ok = False
    if SELLER_BOT_TOKEN:
        try:   # неверный/отозванный токен бота продавцов не должен ронять магазин
            me2 = await seller_bot.get_me()
            SELLER_BOT_USERNAME = me2.username or ""
            await seller_bot.delete_webhook(drop_pending_updates=False)
            seller_ok = True
            if SELLER_WEBAPP_URL:
                try:
                    await seller_bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="🚀 Seller", web_app=WebAppInfo(url=SELLER_WEBAPP_URL)))
                except Exception as e:
                    logging.warning("seller menu button: %s", e)
        except Exception as e:
            SELLER_READY = False
            print(f"⚠️ БОТ ПРОДАВЦОВ НЕ ЗАПУЩЕН: {e}\n   Проверь SELLER_BOT_TOKEN в /etc/azika_shop.env (токен из @BotFather, без пробелов и кавычек).")
    await start_api()
    asyncio.create_task(expire_loop())
    asyncio.create_task(autoconfirm_loop())
    print(f"✅ {SHOP_NAME} запущен: @{BOT_USERNAME}" + (f" · бот продавцов @{SELLER_BOT_USERNAME}" if SELLER_BOT_USERNAME else ""))
    async def poll_sellers():
        try:
            await dp2.start_polling(seller_bot, handle_signals=False)
        except Exception as e:
            print(f"⚠️ Бот продавцов остановился: {e}")
    if seller_ok:
        asyncio.create_task(poll_sellers())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
