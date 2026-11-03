"""
Azika Admin — один админ-бот для Azika Shop и Azika VPN (@azikaadmin_bot).

Что умеет (всё кнопками, писать команды не нужно):
  🏪 Продавцы   — список, карточка, галочка ✓, уровень (+/−/авто), блок/разблок, написать продавцу, заявки
  🎟 Промокоды  — создать для магазина / VPN / обоих сразу, приостановить, включить, удалить
  ⏸ Продажи    — пауза и возобновление продаж отдельно для магазина, VPN или обоих
  📊 Сводка     — цифры по магазину и VPN

Бот напрямую работает с базами обоих ботов (SQLite), поэтому перезапускать основные боты после действий не нужно.
Доступ только у ADMIN_ID. Токен этого бота — только в /etc/azika_admin.env на сервере.
"""
import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import sqlite3
import time
from contextlib import closing
from datetime import datetime
from html import escape
from urllib.parse import parse_qsl

from aiohttp import web

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import BotCommand, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, MenuButtonWebApp, Message, WebAppInfo

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

ADMIN_BOT_TOKEN = os.getenv("ADMIN_BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "6095977712"))
SHOP_DB = os.getenv("SHOP_DB", "/opt/azika_shop/shop.db")
VPN_DB = os.getenv("VPN_DB", "/opt/azika/azika_vpn.db")
SHOP_TOKEN = os.getenv("BOT_TOKEN", "")                 # из /etc/azika_shop.env — чтобы писать продавцам
SELLER_TOKEN = os.getenv("SELLER_BOT_TOKEN", "")

_FEES = [int(x) for x in os.getenv("SELLER_FEES", "10,8,6,4").split(",")]
_LIMS = [int(x) for x in os.getenv("SELLER_LIMITS", "3,10,30,100").split(",")]
# (название, сделок, отзывов, мин. рейтинг, комиссия %, лимит объявлений) — как в shop_bot.py
LEVELS = [("Новичок", 0, 0, 0.0, _FEES[0], _LIMS[0]), ("Продавец", 5, 0, 0.0, _FEES[1], _LIMS[1]),
          ("Профи", 20, 5, 4.5, _FEES[2], _LIMS[2]), ("Топ-продавец", 50, 10, 4.7, _FEES[3], _LIMS[3])]
ADMIN_WEBAPP_URL = os.getenv("ADMIN_WEBAPP_URL", "")      # https://azika304.github.io/Azika/admin.html
ADMIN_API_PORT = int(os.getenv("ADMIN_API_PORT", "8082"))
SHOP_IMG_DIR = os.getenv("SHOP_IMG_DIR", os.path.join(os.path.dirname(SHOP_DB) or ".", "img"))
GAME_NAMES = {"bs": "Brawl Stars", "cr": "Clash Royale", "coc": "Clash of Clans", "pubg": "PUBG", "ff": "Free Fire", "so2": "Standoff 2"}
PER_PAGE = 8
DEFAULT_PAUSE = "⏸ Продажи временно приостановлены. Скоро вернёмся!"


# ======================================================================
# База данных
# ======================================================================
def sdb():
    return closing(sqlite3.connect(SHOP_DB, timeout=10))


def vdb():
    return closing(sqlite3.connect(VPN_DB, timeout=10))


def _addcol(c, table, col, ddl):
    try:
        c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
    except sqlite3.OperationalError:
        pass


def ensure_schema():
    """Добавляет нужные колонки, если основные боты ещё не обновлены (безопасно повторять)."""
    if os.path.exists(SHOP_DB):
        with sdb() as c:
            c.execute("CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT)")
            _addcol(c, "promos", "paused", "INTEGER DEFAULT 0")
            _addcol(c, "promos", "seller_id", "INTEGER DEFAULT 0")
            _addcol(c, "sellers", "lvl_set", "INTEGER DEFAULT 0")
            _addcol(c, "sellers", "lvl", "INTEGER DEFAULT 1")
            _addcol(c, "sellers", "verified", "INTEGER DEFAULT 0")
            _addcol(c, "sellers", "verify_req", "INTEGER DEFAULT 0")
            _addcol(c, "sellers", "verify_rej", "INTEGER DEFAULT 0")
            _addcol(c, "sellers", "review", "INTEGER DEFAULT 0")
            c.execute("CREATE TABLE IF NOT EXISTS seen(user_id INTEGER PRIMARY KEY, ts INTEGER)")
            c.execute("CREATE TABLE IF NOT EXISTS admin_msgs(id INTEGER PRIMARY KEY AUTOINCREMENT, seller_id INTEGER, from_admin INTEGER, text TEXT, created INTEGER, read INTEGER DEFAULT 0)")
            c.commit()
    if os.path.exists(VPN_DB):
        with vdb() as c:
            c.execute("CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT)")
            _addcol(c, "promos", "paused", "INTEGER DEFAULT 0")
            c.commit()


def shop_ok() -> bool:
    return os.path.exists(SHOP_DB)


def vpn_ok() -> bool:
    return os.path.exists(VPN_DB)


# ---------------------------- продавцы --------------------------------
def auto_level(deals: int, cnt: int, avg: float) -> int:
    lv = 0
    for i, (_, d, r, a, _, _) in enumerate(LEVELS):
        if deals >= d and cnt >= r and (not a or avg >= a):
            lv = i
    return lv


def av_path(uid: int) -> str:
    """Ссылка на аватарку через API магазина (подпись — токеном бота магазина, как в shop_bot.av_url)."""
    if not SHOP_TOKEN:
        return ""
    sig = hmac.new(SHOP_TOKEN.encode(), f"av{uid}".encode(), hashlib.sha256).hexdigest()[:16]
    v = 0
    for name in ("avs", "avc"):   # у продавца своё фото продавца, потом общее, потом Telegram
        p = os.path.join(SHOP_IMG_DIR, f"{name}_{uid}.jpg")
        if os.path.exists(p):
            v = int(os.path.getmtime(p))
            break
    return f"/api/avatar/{uid}?t={sig}&v={v}&r=s"


def seller_info(uid: int) -> dict | None:
    with sdb() as c:
        r = c.execute("SELECT s.status,s.balance,s.earned,COALESCE(s.verified,0),COALESCE(s.lvl_set,0),COALESCE(s.lvl,1),s.about,s.created,"
                      "u.name,u.username FROM sellers s LEFT JOIN users u ON u.user_id=s.user_id WHERE s.user_id=?", (uid,)).fetchone()
        if not r:
            return None
        deals = c.execute("SELECT COUNT(*) FROM orders WHERE seller_id=? AND status='done'", (uid,)).fetchone()[0]
        live = c.execute("SELECT COUNT(*) FROM orders WHERE seller_id=? AND status IN ('paid','delivered','dispute')", (uid,)).fetchone()[0]
        rv = c.execute("SELECT AVG(rating),COUNT(*) FROM reviews WHERE seller_id=?", (uid,)).fetchone()
        offers = c.execute("SELECT COUNT(*) FROM offers WHERE seller_id=? AND status IN ('pending','active','hidden')", (uid,)).fetchone()[0]
    with sdb() as c:
        vreq, review = c.execute("SELECT COALESCE(verify_req,0),COALESCE(review,0) FROM sellers WHERE user_id=?", (uid,)).fetchone()
    avg, cnt = (round(rv[0], 1) if rv[0] else 0.0), int(rv[1])
    auto = auto_level(deals, cnt, avg)
    eff = max(auto, min(r[4], len(LEVELS)) - 1) if r[4] else auto
    return {"id": uid, "status": r[0], "balance": r[1], "earned": r[2], "verified": bool(r[3]), "lvl_set": r[4], "lvl_db": r[5],
            "about": r[6] or "", "created": r[7] or 0, "name": r[8] or "", "username": r[9] or "", "deals": deals, "live": live,
            "avg": avg, "cnt": cnt, "offers": offers, "auto": auto, "level": eff, "name_lvl": LEVELS[eff][0], "verify_req": vreq, "review": bool(review), "av": av_path(uid),
            "fee": LEVELS[eff][4], "limit": LEVELS[eff][5]}


def display_name(i: dict) -> str:
    return i["name"] or (("@" + i["username"]) if i["username"] else f"id{i['id']}")


def list_sellers(page: int, mode: str = "a", q: str = "", per: int = PER_PAGE) -> tuple[list[dict], int]:
    """mode: a — все, p — заявки на продажу, v — заявки на галочку. q — поиск по имени, @username или ID."""
    conds = []
    if mode in (True, "p"):
        conds.append("s.status='pending'")
    elif mode == "v":
        conds.append("COALESCE(s.verify_req,0)>0 AND COALESCE(s.verified,0)=0")
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    with sdb() as c:
        rows = c.execute(f"SELECT s.user_id,COALESCE(u.name,''),COALESCE(u.username,'') FROM sellers s LEFT JOIN users u ON u.user_id=s.user_id {where} "
                         "ORDER BY (s.status='pending') DESC, s.created DESC").fetchall()
    q = (q or "").strip().lstrip("@").casefold()
    if q:   # поиск в Python: SQLite не умеет регистронезависимо работать с кириллицей
        rows = [r for r in rows if q in str(r[0]) or q in r[1].casefold() or q in r[2].casefold()]
    ids = [r[0] for r in rows[page * per:(page + 1) * per]]
    return [i for i in (seller_info(u) for u in ids) if i], len(rows)


def set_verified(uid: int, on: bool):
    with sdb() as c:
        c.execute("UPDATE sellers SET verified=? WHERE user_id=?", (1 if on else 0, uid))
        c.commit()


def set_level_floor(uid: int, floor: int) -> dict:
    """Минимальный уровень продавца (0 — только авто). Уровень никогда не ниже заработанного самим продавцом."""
    floor = max(0, min(floor, len(LEVELS)))
    with sdb() as c:
        c.execute("UPDATE sellers SET lvl_set=? WHERE user_id=?", (floor, uid))
        c.commit()
    i = seller_info(uid)
    with sdb() as c:   # чтобы бот магазина не слал второе «новый уровень»
        c.execute("UPDATE sellers SET lvl=? WHERE user_id=?", (i["level"] + 1, uid))
        c.commit()
    return seller_info(uid)


def set_status(uid: int, status: str):
    with sdb() as c:
        c.execute("UPDATE sellers SET status=? WHERE user_id=?", (status, uid))
        c.commit()


# ---------------------------- заявки ----------------------------------
def queue_data() -> dict:
    out = {"sellers": [], "verify": [], "offers": [], "payouts": []}
    if not shop_ok():
        return out
    with sdb() as c:
        for uid, about, created in c.execute("SELECT user_id,about,created FROM sellers WHERE status='pending' ORDER BY created"):
            i = seller_info(uid)
            out["sellers"].append({"id": uid, "name": display_name(i), "username": i["username"], "about": about or "", "created": created or 0, "av": i["av"]})
        vids = [r[0] for r in c.execute("SELECT user_id FROM sellers WHERE COALESCE(verify_req,0)>0 AND COALESCE(verified,0)=0 AND status='approved' ORDER BY verify_req")]
        for oid, sid, game, title, descr, stars, created in c.execute(
                "SELECT id,seller_id,game,title,descr,stars,created FROM offers WHERE status='pending' ORDER BY id"):
            si = seller_info(sid)
            out["offers"].append({"id": oid, "seller": sid, "seller_name": display_name(si) if si else str(sid), "game": GAME_NAMES.get(game, game),
                                  "title": title, "descr": descr or "", "stars": stars, "created": created or 0, "img": f"/api/img/o{oid}"})
        for pid, sid, stars, details, created in c.execute("SELECT id,seller_id,stars,details,created FROM payouts WHERE status='pending' ORDER BY id"):
            si = seller_info(sid)
            out["payouts"].append({"id": pid, "seller": sid, "seller_name": display_name(si) if si else str(sid), "stars": stars,
                                   "details": details or "", "created": created or 0, "balance": si["balance"] if si else 0})
    for uid in vids:
        i = seller_info(uid)
        out["verify"].append({"id": uid, "name": display_name(i), "username": i["username"], "deals": i["deals"], "reviews": i["cnt"], "avg": i["avg"],
                              "level": i["name_lvl"], "requested": i["verify_req"], "av": i["av"]})
    return out


def queue_counts() -> dict:
    q = queue_data()
    n = {k: len(v) for k, v in q.items()}
    n["disputes"] = 0
    if shop_ok():
        with sdb() as c:
            n["disputes"] = c.execute("SELECT COUNT(*) FROM orders WHERE status='dispute'").fetchone()[0]
    n["total"] = n["sellers"] + n["verify"] + n["offers"] + n["payouts"]
    return n


async def decide(kind: str, rid: int, ok: bool, reason: str = "") -> tuple[bool, str]:
    """Решение по заявке: kind = seller | verify | offer | payout."""
    reason = escape((reason or "").strip()[:300])
    why = f"\nПричина: {reason}" if reason else ""
    with sdb() as c:
        if kind == "seller":
            if not c.execute("UPDATE sellers SET status=? WHERE user_id=? AND status='pending'", ("approved" if ok else "banned", rid)).rowcount:
                return False, "Уже обработано"
            c.commit()
            msg = ("✅ <b>Заявка одобрена!</b>\nТеперь тебе доступен <b>Azika Seller</b> — выставляй объявления, делай промокоды и рекламу."
                   if ok else "❌ К сожалению, заявка отклонена." + why)
            note = "✅ Продавец одобрен" if ok else "❌ Заявка отклонена"
        elif kind == "verify":
            if not c.execute("SELECT 1 FROM sellers WHERE user_id=? AND COALESCE(verify_req,0)>0", (rid,)).fetchone():
                return False, "Уже обработано"
            if ok:
                c.execute("UPDATE sellers SET verified=1, verify_req=0 WHERE user_id=?", (rid,))
                msg = "✅ Заявка одобрена — тебе выдана <b>галочка проверенного продавца</b>! Она видна рядом с именем в магазине."
            else:
                c.execute("UPDATE sellers SET verify_req=0, verify_rej=? WHERE user_id=?", (int(time.time()), rid))
                msg = "❌ Заявка на галочку отклонена. Повторно подать её можно через 3 дня." + why
            c.commit()
            note = "✅ Галочка выдана" if ok else "❌ Заявка на галочку отклонена"
        elif kind == "offer":
            r = c.execute("SELECT seller_id,title FROM offers WHERE id=? AND status='pending'", (rid,)).fetchone()
            if not r:
                return False, "Уже обработано"
            c.execute("UPDATE offers SET status=? WHERE id=?", ("active" if ok else "rejected", rid))
            c.commit()
            rid_seller = r[0]
            msg = (f"✅ Товар «{escape(r[1])}» опубликован в магазине!" if ok else f"❌ Товар «{escape(r[1])}» отклонён модератором." + why)
            note = "✅ Товар опубликован" if ok else "❌ Товар отклонён"
            c.commit()
            await notify_seller(rid_seller, msg)
            return True, note
        elif kind == "payout":
            r = c.execute("SELECT seller_id,stars FROM payouts WHERE id=? AND status='pending'", (rid,)).fetchone()
            if not r:
                return False, "Уже обработано"
            if ok:
                c.execute("UPDATE payouts SET status='paid' WHERE id=?", (rid,))
                msg, note = f"💸 Выплата #{rid} на {r[1]}⭐ отправлена.", "✅ Выплата отмечена как выплаченная"
            else:
                c.execute("UPDATE payouts SET status='rejected' WHERE id=?", (rid,))
                c.execute("UPDATE sellers SET balance=balance+? WHERE user_id=?", (r[1], r[0]))
                msg, note = f"↩️ Выплата #{rid} отклонена, {r[1]}⭐ вернулись на баланс." + why, "↩️ Отклонено, звёзды вернулись на баланс"
            c.commit()
            await notify_seller(r[0], msg)
            return True, note
        else:
            return False, "Неизвестная заявка"
    await notify_seller(rid, msg)
    return True, note


async def do_seller_act(uid: int, act: str, text: str = "") -> tuple[bool, str]:
    """Действия над продавцом (общие для бота и мини-приложения)."""
    i = seller_info(uid)
    if not i:
        return False, "Продавец не найден"
    if act in ("verify", "unverify"):
        on = act == "verify"
        set_verified(uid, on)
        if on:
            with sdb() as c:
                c.execute("UPDATE sellers SET verify_req=0 WHERE user_id=?", (uid,))
                c.commit()
            await notify_seller(uid, "✅ Тебе выдана <b>галочка проверенного продавца</b> — она теперь видна рядом с именем в магазине!")
        return True, "✅ Галочка выдана" if on else "Галочка снята"
    if act in ("up", "down", "auto"):
        if act == "up":
            if i["level"] + 1 >= len(LEVELS):
                return False, "Уже максимальный уровень"
            new = min(len(LEVELS), i["level"] + 2)
        elif act == "down":
            if not i["lvl_set"]:
                return False, "Вручную уровень не выдавался — снижать нечего (авто-уровень по сделкам)"
            new = max(0, i["lvl_set"] - 1)
        else:
            new = 0
        j = set_level_floor(uid, new)
        if j["level"] != i["level"]:
            word = "повышен" if j["level"] > i["level"] else "изменён"
            await notify_seller(uid, f"⚡ Твой уровень {word}: <b>{j['name_lvl']}</b>.\nКомиссия магазина теперь <b>{j['fee']}%</b>, лимит объявлений — <b>{j['limit']}</b>.")
        return True, f"⚡ Уровень: {j['name_lvl']}"
    if act in ("ban", "unban"):
        set_status(uid, "banned" if act == "ban" else "approved")
        await notify_seller(uid, "🚫 Продажи для тебя закрыты, товары скрыты. Вопросы — в поддержку." if act == "ban" else "✅ Твой доступ к продажам восстановлен.")
        return True, "🚫 Заблокирован, товары скрыты" if act == "ban" else "✅ Разблокирован"
    if act in ("review_on", "review_off"):
        on = act == "review_on"
        text = (text or "").strip()[:1000]
        with sdb() as c:
            c.execute("UPDATE sellers SET review=? WHERE user_id=?", (1 if on else 0, uid))
            c.commit()
        if text.strip() == "-":   # «снять без сообщения»
            return True, "✅ Проверка снята, товары вернулись" if not on else "🔍 Продавец на проверке"
        if on:
            text = text or "🔍 Ваш аккаунт вызван на проверку. Товары временно скрыты из магазина, новые покупки закрыты. Уже оплаченные заказы выполняйте как обычно. Ответьте на вопросы администрации здесь."
        else:
            text = text or "✅ Вы прошли проверку, поздравляем! Ваши товары снова в магазине."
        with sdb() as c:   # сообщение попадает в чат, поэтому продавец сразу появляется в «Чатах»
            c.execute("INSERT INTO admin_msgs(seller_id,from_admin,text,created) VALUES(?,1,?,?)", (uid, text, int(time.time())))
            c.commit()
        await notify_seller(uid, ("🔍 <b>Проверка аккаунта</b>\n" if on else "✅ <b>Проверка завершена</b>\n") + escape(text) + "\n\n<i>Ответить: Azika Seller → Чаты → Администрация</i>")
        return True, "🔍 Продавец на проверке, товары скрыты" if on else "✅ Проверка снята, продавец уведомлён"
    if act == "msg":
        text = (text or "").strip()
        if not text:
            return False, "Напиши текст сообщения"
        return await chat_send(uid, text)
    return False, "Неизвестное действие"


# ---------------------------- чаты с продавцами -----------------------
async def chat_send(uid: int, text: str) -> tuple[bool, str]:
    text = text.strip()[:1000]
    with sdb() as c:
        c.execute("INSERT INTO admin_msgs(seller_id,from_admin,text,created) VALUES(?,1,?,?)", (uid, text, int(time.time())))
        c.commit()
    ok = await notify_seller(uid, f"✉️ <b>Сообщение от администрации:</b>\n{escape(text)}\n\n<i>Ответить: Azika Seller → Чаты → Администрация</i>")
    return True, "✅ Отправлено" if ok else "Сохранено в чате (бот не смог доставить уведомление)"


def seen_of(uid: int) -> dict:
    if not shop_ok():
        return {"seen": 0, "online": False}
    with sdb() as c:
        r = c.execute("SELECT ts FROM seen WHERE user_id=?", (uid,)).fetchone()
    ts = int(r[0]) if r else 0
    return {"seen": ts, "online": bool(ts) and time.time() - ts < 75}


def touch_admin():
    if not shop_ok():
        return
    try:
        with sdb() as c:
            c.execute("INSERT INTO seen(user_id,ts) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET ts=excluded.ts", (ADMIN_ID, int(time.time())))
            c.commit()
    except Exception:
        pass


def chats_list() -> list[dict]:
    if not shop_ok():
        return []
    with sdb() as c:
        rows = c.execute("SELECT seller_id,MAX(id),SUM(CASE WHEN from_admin=0 AND read=0 THEN 1 ELSE 0 END) FROM admin_msgs GROUP BY seller_id ORDER BY MAX(id) DESC").fetchall()
        out = []
        for sid, mid, unread in rows:
            t, fa, cr = c.execute("SELECT text,from_admin,created FROM admin_msgs WHERE id=?", (mid,)).fetchone()
            i = seller_info(sid)
            if i:
                out.append({"id": sid, "name": display_name(i), "av": i["av"], "last": t, "mine": bool(fa), "t": cr, "unread": unread or 0, "review": i["review"], "verified": i["verified"], **seen_of(sid)})
    return out


def chat_unread() -> int:
    if not shop_ok():
        return 0
    with sdb() as c:
        return c.execute("SELECT COUNT(*) FROM admin_msgs WHERE from_admin=0 AND read=0").fetchone()[0]


# ---------------------------- промокоды -------------------------------
def promo_create(scope: str, code: str, pct: int, uses: int, days: int) -> list[str]:
    """scope: shop | vpn | both. uses=0 — без лимита. Возвращает, где создан."""
    done = []
    if scope in ("shop", "both"):
        if not shop_ok():
            raise ValueError("База магазина не найдена")
        exp = int(time.time()) + days * 86400 if days > 0 else 0
        with sdb() as c:
            c.execute("INSERT OR REPLACE INTO promos(code,pct,max_uses,used,expires,paused,seller_id) "
                      "VALUES(?,?,?,COALESCE((SELECT used FROM promos WHERE code=?),0),?,0,0)", (code, pct, uses, code, exp))
            c.commit()
        done.append("🛒 магазин")
    if scope in ("vpn", "both"):
        if not vpn_ok():
            raise ValueError("База VPN не найдена")
        with vdb() as c:
            c.execute("INSERT OR REPLACE INTO promos(code,pct,uses_left,paused) VALUES(?,?,?,0)", (code, pct, uses if uses > 0 else 100000))
            c.commit()
        done.append("🛡 VPN")
    return done


def promos_all() -> list[dict]:
    out = []
    if shop_ok():
        with sdb() as c:
            for code, pct, mx, used, exp, paused in c.execute(
                    "SELECT code,pct,max_uses,used,expires,COALESCE(paused,0) FROM promos WHERE COALESCE(seller_id,0)=0 ORDER BY code"):
                out.append({"scope": "shop", "code": code, "pct": pct, "max": mx, "used": used, "exp": exp, "paused": bool(paused)})
    if vpn_ok():
        with vdb() as c:
            for code, pct, left, paused in c.execute("SELECT code,pct,uses_left,COALESCE(paused,0) FROM promos ORDER BY code"):
                out.append({"scope": "vpn", "code": code, "pct": pct, "max": 0, "left": left, "used": 0, "exp": 0, "paused": bool(paused)})
    return out


def promo_get(scope: str, code: str) -> dict | None:
    return next((p for p in promos_all() if p["scope"] == scope and p["code"] == code), None)


def promo_toggle(scope: str, code: str) -> bool | None:
    p = promo_get(scope, code)
    if not p:
        return None
    new = 0 if p["paused"] else 1
    with (sdb() if scope == "shop" else vdb()) as c:
        c.execute("UPDATE promos SET paused=? WHERE code=?", (new, code))
        c.commit()
    return bool(new)


def promo_delete(scope: str, code: str) -> bool:
    with (sdb() if scope == "shop" else vdb()) as c:
        n = c.execute("DELETE FROM promos WHERE code=?" + (" AND COALESCE(seller_id,0)=0" if scope == "shop" else ""), (code,)).rowcount
        c.commit()
    return bool(n)


def promo_line(p: dict) -> str:
    icon = "🛒" if p["scope"] == "shop" else "🛡"
    if p["scope"] == "shop":
        use = f"{p['used']}/{p['max']}" if p["max"] else f"{p['used']} исп."
    else:
        use = f"осталось {p['left']}"
    return f"{icon} {p['code']} −{p['pct']}% · {use} {'⏸' if p['paused'] else '▶️'}"


# ---------------------------- пауза продаж ----------------------------
def kv_get(db_open, k: str, default: str = "") -> str:
    try:
        with db_open() as c:
            r = c.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return r[0] if r and r[0] is not None else default
    except sqlite3.Error:
        return default


def kv_set(db_open, k: str, v: str):
    with db_open() as c:
        c.execute("CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT)")
        c.execute("INSERT OR REPLACE INTO kv(k,v) VALUES(?,?)", (k, v))
        c.commit()


def pause_state() -> dict:
    return {"shop": kv_get(sdb, "paused") == "1" if shop_ok() else None,
            "vpn": kv_get(vdb, "paused") == "1" if vpn_ok() else None}


def pause_set(which: str, on: bool):
    for name, op, ok in (("shop", sdb, shop_ok()), ("vpn", vdb, vpn_ok())):
        if which in (name, "all") and ok:
            kv_set(op, "paused", "1" if on else "0")


def pause_msg_set(text: str):
    for op, ok in ((sdb, shop_ok()), (vdb, vpn_ok())):
        if ok:
            kv_set(op, "pause_msg", text)


# ---------------------------- сводка ----------------------------------
def stats() -> dict:
    out = {"shop": None, "vpn": None}
    if shop_ok():
        day = int(time.mktime(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timetuple()))
        with sdb() as c:
            q = lambda sql, *a: c.execute(sql, a).fetchone()
            paid = q("SELECT COUNT(*),COALESCE(SUM(stars),0) FROM orders WHERE status IN ('paid','delivered','dispute','done')")
            today = q("SELECT COUNT(*),COALESCE(SUM(stars),0) FROM orders WHERE status IN ('paid','delivered','dispute','done') AND paid_at>=?", day)
            sl = dict(c.execute("SELECT status,COUNT(*) FROM sellers GROUP BY status").fetchall())
            out["shop"] = {"users": q("SELECT COUNT(*) FROM users")[0], "paid": paid[0], "paid_stars": paid[1], "today": today[0], "today_stars": today[1],
                           "wait": q("SELECT COUNT(*) FROM orders WHERE status='paid'")[0], "disputes": q("SELECT COUNT(*) FROM orders WHERE status='dispute'")[0],
                           "sellers": sl.get("approved", 0), "sellers_pending": sl.get("pending", 0), "sellers_banned": sl.get("banned", 0),
                           "offers_pending": q("SELECT COUNT(*) FROM offers WHERE status='pending'")[0]}
    if vpn_ok():
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        today = datetime.now().strftime("%Y-%m-%d")
        with vdb() as c:
            try:
                pay = c.execute("SELECT COUNT(*) FROM payments").fetchone()[0]
                pay_t = c.execute("SELECT COUNT(*) FROM payments WHERE created LIKE ?", (today + "%",)).fetchone()[0]
            except sqlite3.Error:
                pay = pay_t = 0
            out["vpn"] = {"users": c.execute("SELECT COUNT(*) FROM users").fetchone()[0],
                          "active": c.execute("SELECT COUNT(*) FROM users WHERE expire_date>?", (now,)).fetchone()[0], "payments": pay, "payments_today": pay_t}
    return out


def summary_text() -> str:
    st, ps = stats(), pause_state()
    lines = []
    if st["shop"]:
        s_ = st["shop"]
        qn = queue_counts()
        lines += ["🛒 <b>Azika Shop</b>" + (" ⏸" if ps["shop"] else ""),
                  f"Пользователей: <b>{s_['users']}</b>",
                  f"Заказов оплачено: <b>{s_['paid']}</b> на {s_['paid_stars']}⭐ · сегодня <b>{s_['today']}</b> на {s_['today_stars']}⭐",
                  f"Ждут выдачи: <b>{s_['wait']}</b> · споров: <b>{s_['disputes']}</b>",
                  f"Продавцов: {s_['sellers']} (ждут одобрения: <b>{s_['sellers_pending']}</b>, заблокировано: {s_['sellers_banned']})",
                  f"Товаров на проверке: <b>{qn['offers']}</b> · выплат в очереди: <b>{qn['payouts']}</b> · заявок на галочку: <b>{qn['verify']}</b>"]
    else:
        lines.append("🛒 База магазина не найдена")
    lines.append("")
    if st["vpn"]:
        v = st["vpn"]
        lines += ["🛡 <b>Azika VPN</b>" + (" ⏸" if ps["vpn"] else ""),
                  f"Пользователей: <b>{v['users']}</b> · с активной подпиской: <b>{v['active']}</b>",
                  f"Оплат всего: <b>{v['payments']}</b> · сегодня <b>{v['payments_today']}</b>"]
    else:
        lines.append("🛡 База VPN не найдена")
    return "\n".join(lines)


# ======================================================================
# Telegram
# ======================================================================
bot = Bot(token=ADMIN_BOT_TOKEN or "0:stub", default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
_notify_bots: dict[str, Bot] = {}
WAIT: dict[int, dict] = {}   # что бот ждёт от админа следующим сообщением


def kb(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in r] for r in rows])


def is_admin(uid: int) -> bool:
    return uid == ADMIN_ID


async def notify_seller(uid: int, text: str) -> bool:
    """Пишет продавцу через бота продавцов, иначе через бота магазина."""
    for name, tok in (("seller", SELLER_TOKEN), ("shop", SHOP_TOKEN)):
        if not tok:
            continue
        b = _notify_bots.setdefault(name, Bot(token=tok, default=DefaultBotProperties(parse_mode=ParseMode.HTML)))
        try:
            await b.send_message(uid, text)
            return True
        except Exception as e:
            logging.info("notify %s via %s: %s", uid, name, e)
    return False


def main_menu(classic: bool = False) -> tuple[str, InlineKeyboardMarkup]:
    n = queue_counts() if shop_ok() else {"total": 0, "sellers": 0, "verify": 0, "offers": 0, "payouts": 0, "disputes": 0}
    ps = pause_state()
    flag = lambda v: "" if v is None else (" ⏸" if v else " ▶️")
    rows = []
    if ADMIN_WEBAPP_URL:
        rows.append([InlineKeyboardButton(text="🛠 Открыть админку" + (f" · {n['total']}" if n["total"] else ""), web_app=WebAppInfo(url=ADMIN_WEBAPP_URL))])
    if ADMIN_WEBAPP_URL and not classic:   # админка — мини-приложение; обычное меню спрятано под кнопку «Меню в чате»
        if (n["total"] or n["disputes"]):
            pass
        rows.append([InlineKeyboardButton(text="💬 Меню в чате", callback_data="old")])
        return ("🛠 <b>Azika Admin</b>\nВсё управление — в приложении 👇"
                + (f"\n\n📥 Ждут решения: {n['total']}" if n["total"] else "") + (f"\n⚠️ Споров: {n['disputes']}" if n["disputes"] else ""),
                InlineKeyboardMarkup(inline_keyboard=rows))
    rows += [[InlineKeyboardButton(text="🏪 Продавцы" + (f" · заявок {n['sellers']}" if n["sellers"] else "") + (f" · галочек {n['verify']}" if n["verify"] else ""), callback_data="sl:0:a")],
             [InlineKeyboardButton(text="🎟 Промокоды", callback_data="pr")],
             [InlineKeyboardButton(text=f"⏸ Продажи: 🛒{flag(ps['shop'])} 🛡{flag(ps['vpn'])}", callback_data="ps")],
             [InlineKeyboardButton(text="📊 Сводка", callback_data="st")]]
    info = ""
    if n["total"] or n["disputes"]:
        info = (f"\n\n📥 Ждут решения: продавцы {n['sellers']} · галочки {n['verify']} · товары {n['offers']} · выплаты {n['payouts']}"
                + (f"\n⚠️ Споров по заказам: {n['disputes']}" if n["disputes"] else ""))
    return "🛠 <b>Azika Admin</b>\nУправление магазином и VPN в одном месте." + info, InlineKeyboardMarkup(inline_keyboard=rows)


async def show(target, text: str, markup: InlineKeyboardMarkup | None = None):
    """target — Message (новое сообщение) или CallbackQuery (правим то, что есть)."""
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=markup)
        except Exception as e:   # «message is not modified» и т.п.
            logging.debug("edit: %s", e)
        try:
            await target.answer()
        except Exception:
            pass   # на этот запрос уже ответили всплывающим текстом
    else:
        await target.answer(text, reply_markup=markup)


@dp.message(CommandStart())
@dp.message(Command("menu"))
async def cmd_start(message: Message):
    if not is_admin(message.from_user.id):
        return await message.answer("Это закрытый админ-бот Azika.")
    WAIT.pop(message.from_user.id, None)
    t, m = main_menu()
    await message.answer(t, reply_markup=m)


@dp.callback_query(F.data == "old")
async def cb_old(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        return await cq.answer("Нет доступа", show_alert=True)
    t, m = main_menu(classic=True)
    await show(cq, t, m)


@dp.callback_query(F.data == "menu")
async def cb_menu(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        return await cq.answer("Нет доступа", show_alert=True)
    WAIT.pop(cq.from_user.id, None)
    t, m = main_menu()
    await show(cq, t, m)


# ----------------------------- продавцы -------------------------------
@dp.callback_query(F.data.regexp(r"^sl:\d+:[apv]$"))
async def cb_sellers(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        return await cq.answer("Нет доступа", show_alert=True)
    if not shop_ok():
        return await cq.answer("База магазина не найдена", show_alert=True)
    _, p, f = cq.data.split(":")
    page = int(p)
    rows, total = list_sellers(page, f)
    pages = max(1, -(-total // PER_PAGE))
    buttons = []
    for i in rows:
        mark = {"approved": "", "pending": "🆕 ", "banned": "🚫 "}.get(i["status"], "") + ("📨 " if i["verify_req"] and not i["verified"] else "")
        buttons.append([(f"{mark}{display_name(i)}{' ✓' if i['verified'] else ''} · {i['name_lvl']}", f"s:{i['id']}")])
    nav = []
    if page > 0:
        nav.append(("◀️", f"sl:{page - 1}:{f}"))
    nav.append((f"{page + 1}/{pages}", "noop"))
    if page + 1 < pages:
        nav.append(("▶️", f"sl:{page + 1}:{f}"))
    buttons.append(nav)
    buttons.append([("🆕 Заявки", "sl:0:p"), ("📨 На галочку", "sl:0:v")] if f == "a" else [("👥 Все продавцы", "sl:0:a")])
    buttons.append([("« Меню", "menu")])
    title = {"p": "🆕 <b>Заявки продавцов</b>", "v": "📨 <b>Заявки на галочку</b>"}.get(f, "🏪 <b>Продавцы</b>")
    await show(cq, f"{title} · {total}\nНажми на продавца, чтобы открыть карточку." if rows else f"{title}\nПока пусто.", kb(*buttons))


@dp.callback_query(F.data == "noop")
async def cb_noop(cq: CallbackQuery):
    await cq.answer()


def seller_card(i: dict) -> tuple[str, InlineKeyboardMarkup]:
    st = {"approved": "✅ одобрен", "pending": "🆕 ждёт одобрения", "banned": "🚫 заблокирован"}.get(i["status"], i["status"])
    manual = f"{LEVELS[min(i['lvl_set'], len(LEVELS)) - 1][0]}" if i["lvl_set"] else "нет"
    text = (f"🏪 <b>{escape(display_name(i))}</b>{' ✓' if i['verified'] else ''}\n"
            f"ID: <code>{i['id']}</code>" + (f" · @{escape(i['username'])}" if i["username"] else "") + "\n"
            f"Статус: {st}\n\n"
            f"⚡ Уровень: <b>{i['name_lvl']}</b> ({i['level'] + 1} из {len(LEVELS)}) · комиссия {i['fee']}% · до {i['limit']} объявлений\n"
            f"Заработал сам: {LEVELS[i['auto']][0]} · выдан вручную: {manual}\n\n"
            f"Сделок: {i['deals']} (в работе {i['live']}) · отзывов: {i['cnt']}" + (f" · ★{i['avg']}" if i["cnt"] else "") + "\n"
            f"Объявлений: {i['offers']} · баланс: {i['balance']}⭐ · заработано: {i['earned']}⭐"
            + (f"\n\n📝 О себе: {escape(i['about'][:300])}" if i["about"] else ""))
    u = i["id"]
    rows = []
    if i["status"] == "pending":
        rows.append([("✅ Одобрить", f"sok:{u}"), ("❌ Отклонить", f"sno:{u}")])
    if i["verify_req"] and not i["verified"]:
        rows.append([("✅ Одобрить галочку", f"vok:{u}"), ("❌ Отклонить", f"vno:{u}")])
    rows.append([("✓ Снять галочку" if i["verified"] else "✓ Выдать галочку", f"sv:{u}")])
    rows.append([("⬆️ Уровень +1", f"su:{u}"), ("⬇️ −1", f"sd:{u}"), ("🔄 Авто", f"sa:{u}")])
    rows.append([("✅ Разблокировать" if i["status"] == "banned" else "🚫 Заблокировать", f"sb:{u}"), ("✉️ Написать", f"sm:{u}")])
    rows.append([("« К списку", "sl:0:a"), ("« Меню", "menu")])
    return text, kb(*rows)


async def open_seller(cq: CallbackQuery, uid: int, note: str = ""):
    i = seller_info(uid)
    if not i:
        return await cq.answer("Продавец не найден", show_alert=True)
    t, m = seller_card(i)
    await show(cq, (note + "\n\n" if note else "") + t, m)


@dp.callback_query(F.data.regexp(r"^(s|sv|su|sd|sa|sb|sm|sok|sno|vok|vno):\d+$"))
async def cb_seller_action(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        return await cq.answer("Нет доступа", show_alert=True)
    act, num = cq.data.split(":")
    uid = int(num)
    i = seller_info(uid)
    if not i:
        return await cq.answer("Продавец не найден", show_alert=True)
    note = ""
    if act == "sm":
        WAIT[cq.from_user.id] = {"kind": "msg", "uid": uid}
        await cq.message.answer(f"✍️ Напиши сообщение для <b>{escape(display_name(i))}</b> — я передам его от имени магазина. Отмена: /menu")
        return await cq.answer()
    if act in ("sok", "sno"):
        ok, note = await decide("seller", uid, act == "sok")
    elif act in ("vok", "vno"):
        ok, note = await decide("verify", uid, act == "vok")
    else:
        mapped = {"sv": "unverify" if i["verified"] else "verify", "su": "up", "sd": "down", "sa": "auto",
                  "sb": "unban" if i["status"] == "banned" else "ban"}.get(act)
        if not mapped:
            ok, note = True, ""
        else:
            ok, note = await do_seller_act(uid, mapped)
    if not ok:
        return await cq.answer(note, show_alert=True)
    await open_seller(cq, uid, note)


# ----------------------------- промокоды ------------------------------
@dp.callback_query(F.data == "pr")
async def cb_promos(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        return await cq.answer("Нет доступа", show_alert=True)
    ps = promos_all()
    rows = [[(promo_line(p), f"p:{p['scope']}:{p['code']}")] for p in ps[:40]]
    rows.append([("➕ Создать промокод", "prn")])
    rows.append([("« Меню", "menu")])
    txt = "🎟 <b>Промокоды</b>\n🛒 магазин · 🛡 VPN · ▶️ работает · ⏸ приостановлен\n"
    if shop_ok():
        with sdb() as c:
            n = c.execute("SELECT COUNT(*) FROM promos WHERE COALESCE(seller_id,0)>0").fetchone()[0]
        if n:
            txt += f"\nПромокодов продавцов (не показаны): {n}"
    await show(cq, txt if ps else txt + "\nПромокодов нет.", kb(*rows))


@dp.callback_query(F.data == "prn")
async def cb_promo_new(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        return await cq.answer("Нет доступа", show_alert=True)
    await show(cq, "➕ <b>Новый промокод</b>\nГде он будет действовать?",
               kb([("🛒 Магазин", "prs:shop"), ("🛡 VPN", "prs:vpn")], [("🛒+🛡 Оба бота", "prs:both")], [("« Назад", "pr")]))


@dp.callback_query(F.data.regexp(r"^prs:(shop|vpn|both)$"))
async def cb_promo_scope(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        return await cq.answer("Нет доступа", show_alert=True)
    scope = cq.data.split(":")[1]
    WAIT[cq.from_user.id] = {"kind": "promo", "scope": scope}
    name = {"shop": "🛒 магазина", "vpn": "🛡 VPN", "both": "🛒+🛡 обоих ботов"}[scope]
    await cq.message.answer(f"Промокод для {name}.\nНапиши одним сообщением:\n<code>КОД ПРОЦЕНТ [использований] [дней]</code>\n\n"
                            "Примеры:\n<code>SALE20 20 100 30</code> — −20%, 100 раз, 30 дней\n<code>START10 10</code> — −10%, без лимита\n\n"
                            "Процент 1–90. «Дней» работает только в магазине. Отмена: /menu")
    await cq.answer()


@dp.callback_query(F.data.regexp(r"^(p|pt|pd|pdy):(shop|vpn):[A-Za-z0-9_-]{1,24}$"))
async def cb_promo_action(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        return await cq.answer("Нет доступа", show_alert=True)
    act, scope, code = cq.data.split(":")
    note = ""
    if act == "pt":
        r = promo_toggle(scope, code)
        if r is None:
            return await cq.answer("Промокод не найден", show_alert=True)
        note = "⏸ Приостановлен — применить его нельзя" if r else "▶️ Снова работает"
    elif act == "pd":
        return await show(cq, f"🗑 Удалить промокод <code>{code}</code> ({'🛒 магазин' if scope == 'shop' else '🛡 VPN'})?",
                          kb([("Да, удалить", f"pdy:{scope}:{code}"), ("Отмена", f"p:{scope}:{code}")]))
    elif act == "pdy":
        promo_delete(scope, code)
        await cq.answer("Удалён")
        return await cb_promos(cq)
    p = promo_get(scope, code)
    if not p:
        return await cq.answer("Промокод не найден", show_alert=True)
    exp = f"\nДействует до: {datetime.fromtimestamp(p['exp']).strftime('%d.%m.%Y')}" if p.get("exp") else ""
    use = (f"Использовано: {p['used']}" + (f" из {p['max']}" if p["max"] else " (без лимита)")) if scope == "shop" else f"Осталось использований: {p['left']}"
    txt = (f"{note}\n\n" if note else "") + (f"🎟 <code>{code}</code> · −{p['pct']}%\n"
           f"Где: {'🛒 магазин' if scope == 'shop' else '🛡 VPN'}\n{use}{exp}\nСтатус: {'⏸ приостановлен' if p['paused'] else '▶️ работает'}")
    await show(cq, txt, kb([("▶️ Включить" if p["paused"] else "⏸ Приостановить", f"pt:{scope}:{code}")],
                           [("🗑 Удалить", f"pd:{scope}:{code}")], [("« К промокодам", "pr")]))


# ----------------------------- пауза продаж ---------------------------
def pause_screen() -> tuple[str, InlineKeyboardMarkup]:
    ps = pause_state()
    def line(name, v, db_open):
        if v is None:
            return f"{name}: база не найдена"
        msg = kv_get(db_open, "pause_msg", DEFAULT_PAUSE)
        return f"{name}: {'⏸ <b>приостановлены</b>' if v else '▶️ <b>идут</b>'}" + (f"\n   текст: «{escape(msg)}»" if v else "")
    txt = ("⏸ <b>Продажи</b>\nПока продажи на паузе, покупатели видят сообщение и не могут оплатить. "
           "Уже оплаченные заказы работают как раньше.\n\n" + line("🛒 Магазин", ps["shop"], sdb) + "\n" + line("🛡 VPN", ps["vpn"], vdb))
    tg = lambda who, v: ("▶️ Включить " if v else "⏸ Пауза ") + who
    rows = [[(tg("🛒", ps["shop"]), "pst:shop"), (tg("🛡", ps["vpn"]), "pst:vpn")],
            [("⏸ Пауза на оба" if not all(v for v in ps.values() if v is not None) else "▶️ Включить оба", "pst:all")],
            [("✏️ Текст для покупателей", "psm")], [("« Меню", "menu")]]
    return txt, kb(*rows)


@dp.callback_query(F.data == "ps")
async def cb_pause(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        return await cq.answer("Нет доступа", show_alert=True)
    t, m = pause_screen()
    await show(cq, t, m)


@dp.callback_query(F.data.regexp(r"^pst:(shop|vpn|all)$"))
async def cb_pause_toggle(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        return await cq.answer("Нет доступа", show_alert=True)
    which = cq.data.split(":")[1]
    ps = pause_state()
    if which == "all":
        on = not all(v for v in ps.values() if v is not None)
    else:
        on = not ps[which]
    pause_set(which, on)
    await cq.answer("⏸ Приостановлено" if on else "▶️ Продажи включены")
    t, m = pause_screen()
    await show(cq, t, m)


@dp.callback_query(F.data == "psm")
async def cb_pause_msg(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        return await cq.answer("Нет доступа", show_alert=True)
    WAIT[cq.from_user.id] = {"kind": "pausemsg"}
    await cq.message.answer("✏️ Напиши текст, который увидят покупатели во время паузы (до 200 символов). Например: «Технические работы, вернёмся в 18:00».\nОтмена: /menu")
    await cq.answer()


# ----------------------------- сводка ---------------------------------
@dp.callback_query(F.data == "st")
async def cb_summary(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        return await cq.answer("Нет доступа", show_alert=True)
    await show(cq, "📊 <b>Сводка</b>\n\n" + summary_text(), kb([("🔄 Обновить", "st")], [("« Меню", "menu")]))


# ----------------------- текстовые ответы админа -----------------------
@dp.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message):
    uid = message.from_user.id
    if not is_admin(uid):
        return
    st = WAIT.get(uid)
    if not st:
        t, m = main_menu()
        return await message.answer(t, reply_markup=m)
    text = (message.text or "").strip()
    if st["kind"] == "msg":
        WAIT.pop(uid, None)
        ok, info = await do_seller_act(st["uid"], "msg", text)
        return await message.answer(info if ok else "⚠️ " + info,
                                    reply_markup=kb([("« К продавцу", f"s:{st['uid']}")]))
    if st["kind"] == "pausemsg":
        WAIT.pop(uid, None)
        pause_msg_set(text[:200])
        t, m = pause_screen()
        return await message.answer("✅ Текст сохранён.\n\n" + t, reply_markup=m)
    if st["kind"] == "promo":
        a = text.split()
        if len(a) < 2 or len(a) > 4 or not re.fullmatch(r"[A-Za-z0-9_-]{2,24}", a[0]) or any(not x.isdigit() for x in a[1:]) \
                or not 1 <= int(a[1]) <= 90:
            return await message.answer("Формат: <code>КОД ПРОЦЕНТ [использований] [дней]</code>, процент 1–90.\nНапример: <code>SALE20 20 100 30</code>")
        code, pct = a[0].upper(), int(a[1])
        uses = int(a[2]) if len(a) > 2 else 0
        days = int(a[3]) if len(a) > 3 else 0
        try:
            where = promo_create(st["scope"], code, pct, uses, days)
        except Exception as e:
            return await message.answer(f"⚠️ Не создан: {escape(str(e))}")
        WAIT.pop(uid, None)
        return await message.answer(f"✅ Промокод <code>{code}</code> −{pct}% создан: {', '.join(where)}\n"
                                    f"Использований: {uses or 'без лимита'}" + (f" · {days} дн." if days and st["scope"] != "vpn" else ""),
                                    reply_markup=kb([("🎟 К промокодам", "pr")], [("« Меню", "menu")]))


# ======================================================================
# API для мини-приложения (admin.html). Снаружи доступно через shop_bot: /api/admin/<имя> → сюда.
# ======================================================================
def verify_admin(init_data: str) -> dict | None:
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        got = pairs.pop("hash")
        check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", ADMIN_BOT_TOKEN.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(hmac.new(secret, check.encode(), hashlib.sha256).hexdigest(), got):
            return None
        if time.time() - int(pairs.get("auth_date", "0")) > 86400:
            return None
        user = json.loads(pairs["user"])
        return user if int(user.get("id", 0)) == ADMIN_ID else None
    except Exception:
        return None


def admin_route(fn):
    async def wrapper(request):
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"ok": False, "msg": "Некорректный запрос"}, status=400)
        if not verify_admin(str(data.get("initData", ""))):
            return web.json_response({"ok": False, "msg": "Нет доступа: открой приложение из @azikaadmin_bot"}, status=401)
        touch_admin()
        try:
            return web.json_response(await fn(data))
        except Exception as e:
            logging.exception("api %s", fn.__name__)
            return web.json_response({"ok": False, "msg": f"Ошибка сервера: {e}"})
    return wrapper


def seller_row_json(i: dict) -> dict:
    keys = ("id", "name", "username", "status", "verified", "review", "verify_req", "level", "name_lvl", "fee", "limit", "deals", "cnt", "avg", "offers", "balance", "earned", "av", "about", "lvl_set", "auto", "live")
    d = {k: i[k] for k in keys}
    d["name"] = display_name(i)
    d["levels"] = [{"level": n, "name": l[0], "fee": l[4], "limit": l[5]} for n, l in enumerate(LEVELS)]
    return d


@admin_route
async def api_overview(d):
    return {"ok": True, "chat_unread": chat_unread(), "counts": queue_counts() if shop_ok() else {}, "pause": {**pause_state(), "msg": kv_get(sdb, "pause_msg", DEFAULT_PAUSE) if shop_ok() else DEFAULT_PAUSE},
            "stats": stats(), "shop_ok": shop_ok(), "vpn_ok": vpn_ok()}


@admin_route
async def api_queue(d):
    return {"ok": True, **queue_data()}


@admin_route
async def api_decide(d):
    kind, rid = str(d.get("kind", "")), int(d.get("id", 0))
    ok, msg = await decide(kind, rid, bool(d.get("ok")), str(d.get("reason", "")))
    return {"ok": ok, "msg": msg}


@admin_route
async def api_sellers(d):
    rows, total = list_sellers(int(d.get("page", 0)), str(d.get("mode", "a")), str(d.get("q", "")), per=20)
    return {"ok": True, "total": total, "sellers": [seller_row_json(i) for i in rows]}


@admin_route
async def api_seller(d):
    i = seller_info(int(d.get("id", 0)))
    return {"ok": bool(i), "seller": seller_row_json(i) if i else None, "msg": "" if i else "Продавец не найден"}


@admin_route
async def api_seller_act(d):
    uid = int(d.get("id", 0))
    ok, msg = await do_seller_act(uid, str(d.get("act", "")), str(d.get("text", "")))
    i = seller_info(uid)
    return {"ok": ok, "msg": msg, "seller": seller_row_json(i) if i else None}


@admin_route
async def api_chats(d):
    return {"ok": True, "chats": chats_list()}


@admin_route
async def api_chat(d):
    uid = int(d.get("id", 0))
    i = seller_info(uid)
    if not i:
        return {"ok": False, "msg": "Продавец не найден"}
    with sdb() as c:
        rows = c.execute("SELECT id,from_admin,text,created,read FROM admin_msgs WHERE seller_id=? ORDER BY id DESC LIMIT 100", (uid,)).fetchall()[::-1]
        c.execute("UPDATE admin_msgs SET read=1 WHERE seller_id=? AND from_admin=0 AND read=0", (uid,))
        c.commit()
    return {"ok": True, "seller": seller_row_json(i), "msgs": [{"id": r[0], "mine": bool(r[1]), "text": r[2], "created": r[3], "read": bool(r[4])} for r in rows], **seen_of(uid)}


@admin_route
async def api_chat_send(d):
    uid, text = int(d.get("id", 0)), str(d.get("text", "")).strip()
    if not seller_info(uid):
        return {"ok": False, "msg": "Продавец не найден"}
    if not text:
        return {"ok": False, "msg": "Напиши сообщение"}
    ok, msg = await chat_send(uid, text)
    return {"ok": ok, "msg": msg}


@admin_route
async def api_promos(d):
    return {"ok": True, "promos": promos_all()}


@admin_route
async def api_promo_new(d):
    scope, code = str(d.get("scope", "")), str(d.get("code", "")).strip().upper()
    pct, uses, days = int(d.get("pct", 0)), max(0, int(d.get("uses", 0) or 0)), max(0, int(d.get("days", 0) or 0))
    if scope not in ("shop", "vpn", "both") or not re.fullmatch(r"[A-Z0-9_-]{2,24}", code) or not 1 <= pct <= 90:
        return {"ok": False, "msg": "Проверь код (латиница, цифры, 2–24 символа) и процент 1–90"}
    where = promo_create(scope, code, pct, uses, days)
    return {"ok": True, "msg": f"✅ Создан: {', '.join(where)}", "promos": promos_all()}


@admin_route
async def api_promo_act(d):
    scope, code, act = str(d.get("scope", "")), str(d.get("code", "")).upper(), str(d.get("act", ""))
    if scope not in ("shop", "vpn") or not re.fullmatch(r"[A-Z0-9_-]{1,24}", code):
        return {"ok": False, "msg": "Некорректный промокод"}
    if act == "toggle":
        r = promo_toggle(scope, code)
        msg = "Не найден" if r is None else ("⏸ Приостановлен" if r else "▶️ Снова работает")
    elif act == "delete":
        msg = "🗑 Удалён" if promo_delete(scope, code) else "Не найден"
    else:
        return {"ok": False, "msg": "Неизвестное действие"}
    return {"ok": True, "msg": msg, "promos": promos_all()}


@admin_route
async def api_pause(d):
    which = str(d.get("which", "all"))
    if which not in ("shop", "vpn", "all"):
        return {"ok": False, "msg": "Некорректный запрос"}
    if "text" in d:
        pause_msg_set(str(d.get("text", ""))[:200] or DEFAULT_PAUSE)
    if "on" in d:
        pause_set(which, bool(d.get("on")))
    return {"ok": True, "msg": "Сохранено", "pause": {**pause_state(), "msg": kv_get(sdb, "pause_msg", DEFAULT_PAUSE) if shop_ok() else DEFAULT_PAUSE}}


def build_api() -> web.Application:
    app = web.Application(client_max_size=1024 * 1024)
    for name, fn in (("overview", api_overview), ("queue", api_queue), ("decide", api_decide), ("sellers", api_sellers), ("seller", api_seller),
                     ("seller_act", api_seller_act), ("chats", api_chats), ("chat", api_chat), ("chat_send", api_chat_send), ("promos", api_promos), ("promo_new", api_promo_new), ("promo_act", api_promo_act), ("pause", api_pause)):
        app.router.add_post(f"/api/{name}", fn)
    return app


async def main():
    if not ADMIN_BOT_TOKEN:
        raise SystemExit("Нет ADMIN_BOT_TOKEN — впиши токен @azikaadmin_bot в /etc/azika_admin.env")
    ensure_schema()
    me = await bot.get_me()
    await bot.set_my_commands([BotCommand(command="menu", description="🛠 Главное меню")])
    runner = web.AppRunner(build_api())
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", ADMIN_API_PORT).start()
    if ADMIN_WEBAPP_URL:
        try:
            await bot.set_chat_menu_button(chat_id=ADMIN_ID, menu_button=MenuButtonWebApp(text="Админка", web_app=WebAppInfo(url=ADMIN_WEBAPP_URL)))
        except Exception as e:
            logging.warning("menu button: %s", e)
    logging.info("✅ Azika Admin запущен: @%s · магазин: %s · VPN: %s", me.username,
                 "ок" if shop_ok() else "БАЗА НЕ НАЙДЕНА", "ок" if vpn_ok() else "БАЗА НЕ НАЙДЕНА")
    try:
        await dp.start_polling(bot)
    finally:
        for b in _notify_bots.values():
            await b.session.close()


if __name__ == "__main__":
    asyncio.run(main())
