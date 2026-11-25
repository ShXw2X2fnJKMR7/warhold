# -*- coding: utf-8 -*-
# وارهولد - سرور (سایت + API + ربات تلگرام، همه تو یک برنامه)
# نصب: pip install pyTelegramBotAPI   |   اجرا: python server.py
#
# تنظیمات رو یا تو فایل config.txt (کنار همین فایل) بنویس یا به شکل متغیر محیطی بده:
#   BOT_TOKEN=توکن ربات
#   OWNER_ID=آیدی عددی تلگرام مالک
#   SITE_URL=آدرس https سایت (برای دکمه باز کردن سایت تو ربات - اختیاری)
# توکن رو هرگز تو GitHub یا جای عمومی نذار.

import base64
import hashlib
import json
import os
import random
import re
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import telebot
from telebot import types

BASE = os.path.dirname(os.path.abspath(__file__))


def load_config():
    cfg = {}
    path = os.path.join(BASE, "config.txt")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    cfg[k.strip()] = v.strip()
    for k in ("BOT_TOKEN", "OWNER_ID", "PORT", "DB_PATH", "SITE_URL", "COC_API_KEY", "COC_API_BASE", "TELEGRAM_RELAY"):
        if os.environ.get(k):
            cfg[k] = os.environ[k]
    return cfg


CFG = load_config()
BOT_TOKEN = CFG.get("BOT_TOKEN", "")
OWNER_ID = int(CFG.get("OWNER_ID", "0") or 0)
PORT = int(CFG.get("PORT", "8080"))
DB_PATH = CFG.get("DB_PATH", os.path.join(BASE, "warhold.db"))
SITE_URL = CFG.get("SITE_URL", "")
COC_KEY = CFG.get("COC_API_KEY", "")
COC_BASE = CFG.get("COC_API_BASE", "https://api.clashofclans.com/v1")
UPLOAD_DIR = os.path.join(os.path.dirname(DB_PATH), "uploads")
ASSET_DIR = os.path.join(os.path.dirname(DB_PATH), "assets")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(ASSET_DIR, exist_ok=True)
SITE_FILE = os.path.join(BASE, "index.html")
MAX_CLANS_PER_USER = 3
SESSION_DAYS = 30
CODE_SECONDS = 300

TELEGRAM_RELAY = CFG.get("TELEGRAM_RELAY", "").rstrip("/")
if TELEGRAM_RELAY:
    # وقتی سرور به تلگرام دسترسی مستقیم نداره، درخواست‌ها از یه رله (مثل Cloudflare Worker) رد می‌شن
    telebot.apihelper.API_URL = TELEGRAM_RELAY + "/bot{0}/{1}"
    telebot.apihelper.FILE_URL = TELEGRAM_RELAY + "/file/bot{0}/{1}"
bot = telebot.TeleBot(BOT_TOKEN or "0:none", parse_mode=None)
BOT_USERNAME = ""
DRAW_LOCK = threading.Lock()


# ======================= دیتابیس =======================
def q(sql, args=(), fetch="all", commit=False):
    con = sqlite3.connect(DB_PATH, timeout=15)
    try:
        cur = con.execute(sql, args)
        if commit:
            con.commit()
        if fetch == "one":
            return cur.fetchone()
        if fetch == "all":
            return cur.fetchall()
        if fetch == "id":
            return cur.lastrowid
        return None
    finally:
        con.close()


def init_db():
    con = sqlite3.connect(DB_PATH)
    con.executescript("""
    CREATE TABLE IF NOT EXISTS users(tg_id INTEGER PRIMARY KEY, first_name TEXT, username TEXT, created_at INTEGER);
    CREATE TABLE IF NOT EXISTS codes(code TEXT PRIMARY KEY, tg_id INTEGER, expires_at INTEGER);
    CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, tg_id INTEGER, created_at INTEGER);
    CREATE TABLE IF NOT EXISTS admins(tg_id INTEGER PRIMARY KEY);
    CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE IF NOT EXISTS tournaments(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, type TEXT, capacity INTEGER,
        phase TEXT, reg_open INTEGER, live_url TEXT, created_at INTEGER);
    CREATE TABLE IF NOT EXISTS t_players(tid INTEGER, tg_id INTEGER, game_name TEXT, game_tag TEXT, created_at INTEGER,
        PRIMARY KEY(tid, tg_id), UNIQUE(tid, game_tag));
    CREATE TABLE IF NOT EXISTS t_groups(tid INTEGER, tg_id INTEGER, group_no INTEGER, PRIMARY KEY(tid, tg_id));
    CREATE TABLE IF NOT EXISTS t_clans(tid INTEGER, clan_id INTEGER, seed INTEGER, created_at INTEGER, PRIMARY KEY(tid, clan_id));
    CREATE TABLE IF NOT EXISTS clans(id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER, name TEXT, tag TEXT,
        level INTEGER, members INTEGER, description TEXT, status TEXT, created_at INTEGER);
    CREATE TABLE IF NOT EXISTS listings(id INTEGER PRIMARY KEY AUTOINCREMENT, seller_id INTEGER, tag TEXT, price INTEGER, th INTEGER,
        exp_level INTEGER, heroes TEXT, abilities INTEGER, abilities_max INTEGER, units_percent INTEGER, map_percent INTEGER,
        description TEXT, photos TEXT, status TEXT, created_at INTEGER, equipment TEXT);
    CREATE TABLE IF NOT EXISTS equipment(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE, name_fa TEXT, hero TEXT, rarity TEXT);
    """)
    try:
        con.execute("ALTER TABLE listings ADD COLUMN equipment TEXT")
    except sqlite3.OperationalError:
        pass
    if con.execute("SELECT COUNT(*) FROM equipment").fetchone()[0] == 0:
        for hero, items in _EQ.items():
            for name, rarity in items:
                con.execute("INSERT OR IGNORE INTO equipment(name,name_fa,hero,rarity) VALUES(?,?,?,?)", (name, "", hero, rarity))
    con.commit()
    con.close()


def now():
    return int(time.time())


def get_setting(key, default):
    row = q("SELECT value FROM settings WHERE key=?", (key,), "one")
    return row[0] if row else default


def set_setting(key, value):
    q("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, str(value)), None, True)


# ======================= نقش‌ها و ابزارها =======================
def role_of(uid):
    if OWNER_ID and uid == OWNER_ID:
        return "owner"
    if q("SELECT 1 FROM admins WHERE tg_id=?", (uid,), "one"):
        return "admin"
    return "user"


def is_staff(uid):
    return role_of(uid) != "user"


def is_owner(uid):
    return role_of(uid) == "owner"


def staff_ids():
    ids = {r[0] for r in q("SELECT tg_id FROM admins")}
    if OWNER_ID:
        ids.add(OWNER_ID)
    return ids


def clean(text, lo, hi):
    if not isinstance(text, str):
        return None
    t = re.sub(r"[\x00-\x1f\x7f<>]", "", text)
    t = re.sub(r"\s+", " ", t).strip()
    return t if lo <= len(t) <= hi else None


def norm_tag(text):
    if not isinstance(text, str):
        return None
    t = text.strip().upper().replace("O", "0").lstrip("#")
    return "#" + t if re.fullmatch(r"[0289PYLQGRJCUV]{3,15}", t) else None


def clan_link(tag):
    return "https://link.clashofclans.com/en?action=OpenClanProfile&tag=%23" + tag.lstrip("#")


def upsert_user(u):
    q("INSERT INTO users(tg_id,first_name,username,created_at) VALUES(?,?,?,?) "
      "ON CONFLICT(tg_id) DO UPDATE SET first_name=excluded.first_name, username=excluded.username",
      (u.id, u.first_name or "", u.username or "", now()), None, True)


def notify_staff(text, markup=None):
    for uid in staff_ids():
        try:
            bot.send_message(uid, text, reply_markup=markup)
        except Exception:
            pass


def send_long(chat_id, text):
    chunk = ""
    for line in text.split("\n"):
        if len(chunk) + len(line) + 1 > 3800:
            bot.send_message(chat_id, chunk)
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        bot.send_message(chat_id, chunk)


# ======================= ورود با کد ۸ رقمی =======================
def make_code(uid):
    q("DELETE FROM codes WHERE tg_id=? OR expires_at<?", (uid, now()), None, True)
    for _ in range(30):
        code = "%08d" % secrets.randbelow(10 ** 8)
        if not q("SELECT 1 FROM codes WHERE code=?", (code,), "one"):
            q("INSERT INTO codes(code,tg_id,expires_at) VALUES(?,?,?)", (code, uid, now() + CODE_SECONDS), None, True)
            return code
    return None


def login_with_code(code):
    row = q("SELECT tg_id, expires_at FROM codes WHERE code=?", (code,), "one")
    if not row or row[1] < now():
        return None
    q("DELETE FROM codes WHERE code=?", (code,), None, True)
    token = secrets.token_urlsafe(32)
    q("INSERT INTO sessions(token_hash,tg_id,created_at) VALUES(?,?,?)",
      (hashlib.sha256(token.encode()).hexdigest(), row[0], now()), None, True)
    return token, row[0]


def user_from_token(token):
    row = q("SELECT tg_id FROM sessions WHERE token_hash=? AND created_at>?",
            (hashlib.sha256(token.encode()).hexdigest(), now() - SESSION_DAYS * 86400), "one")
    return row[0] if row else None


RATE = {}


def limited(key, limit, window):
    t = now()
    lst = [x for x in RATE.get(key, []) if x > t - window]
    if len(lst) >= limit:
        RATE[key] = lst
        return True
    lst.append(t)
    RATE[key] = lst
    return False


# ======================= تورنمنت‌ها =======================
TYPES = {"solo": (64, 128), "clan": (16, 32)}
LIVE_HOSTS = ("www.aparat.com", "aparat.com", "www.youtube.com", "www.youtube-nocookie.com", "player.twitch.tv")


def safe_live_url(u):
    if not u:
        return ""
    if not isinstance(u, str) or len(u) > 300:
        return None
    p = urllib.parse.urlparse(u.strip())
    return u.strip() if p.scheme == "https" and p.hostname in LIVE_HOSTS else None


def tour_row(tid):
    r = q("SELECT id,name,type,capacity,phase,reg_open,live_url FROM tournaments WHERE id=?", (tid,), "one")
    if not r:
        return None
    return {"id": r[0], "name": r[1], "type": r[2], "capacity": r[3], "phase": r[4], "reg_open": bool(r[5]), "live_url": r[6] or ""}


def t_count(t):
    table = "t_players" if t["type"] == "solo" else "t_clans"
    return q("SELECT COUNT(*) FROM %s WHERE tid=?" % table, (t["id"],), "one")[0]


def tours_list():
    out = []
    for (tid,) in q("SELECT id FROM tournaments ORDER BY id DESC"):
        t = tour_row(tid)
        t["count"] = t_count(t)
        out.append(t)
    return out


def create_tournament(name, typ, cap):
    name = clean(name, 2, 40)
    try:
        cap = int(cap)
    except (TypeError, ValueError):
        cap = 0
    if not name:
        return False, "اسم تورنمنت باید بین ۲ تا ۴۰ حرف باشه.", None
    if typ not in TYPES or cap not in TYPES[typ]:
        return False, "نوع یا ظرفیت نامعتبره.", None
    tid = q("INSERT INTO tournaments(name,type,capacity,phase,reg_open,live_url,created_at) VALUES(?,?,?,?,?,?,?)",
            (name, typ, cap, "registration", 1, "", now()), "id", True)
    return True, "تورنمنت ساخته شد.", tid


def register_player(tid, uid, name, tag):
    t = tour_row(tid)
    if not t or t["type"] != "solo":
        return False, "تورنمنت پیدا نشد."
    name, tag = clean(name, 2, 30), norm_tag(tag)
    if not name:
        return False, "نام باید بین ۲ تا ۳۰ حرف باشه."
    if not tag:
        return False, "تگ معتبر نیست (مثل #2PP0LQ8)."
    if t["phase"] != "registration" or not t["reg_open"]:
        return False, "ثبت‌نام این تورنمنت بسته است."
    if q("SELECT 1 FROM t_players WHERE tid=? AND tg_id=?", (tid, uid), "one"):
        return False, "شما قبلاً ثبت‌نام کرده‌اید."
    if t_count(t) >= t["capacity"]:
        return False, "ظرفیت پر شده است."
    if q("SELECT 1 FROM t_players WHERE tid=? AND game_tag=?", (tid, tag), "one"):
        return False, "این تگ قبلاً ثبت شده است."
    q("INSERT INTO t_players(tid,tg_id,game_name,game_tag,created_at) VALUES(?,?,?,?,?)", (tid, uid, name, tag, now()), None, True)
    if t_count(t) >= t["capacity"]:
        do_draw(tid)
    return True, "ثبت‌نام انجام شد."


def register_clan(tid, uid, clan_id):
    t = tour_row(tid)
    if not t or t["type"] != "clan":
        return False, "تورنمنت پیدا نشد."
    try:
        clan_id = int(clan_id)
    except (TypeError, ValueError):
        return False, "کلن نامعتبره."
    if not q("SELECT 1 FROM clans WHERE id=? AND owner_id=? AND status='approved'", (clan_id, uid), "one"):
        return False, "فقط کلن‌های تأییدشده خودت رو می‌تونی ثبت کنی."
    if t["phase"] != "registration" or not t["reg_open"]:
        return False, "ثبت‌نام این تورنمنت بسته است."
    if q("SELECT 1 FROM t_clans WHERE tid=? AND clan_id=?", (tid, clan_id), "one"):
        return False, "این کلن قبلاً ثبت شده است."
    if t_count(t) >= t["capacity"]:
        return False, "ظرفیت پر شده است."
    q("INSERT INTO t_clans(tid,clan_id,seed,created_at) VALUES(?,?,NULL,?)", (tid, clan_id, now()), None, True)
    if t_count(t) >= t["capacity"]:
        do_draw(tid)
    return True, "کلن ثبت شد."


def cancel_registration(tid, uid):
    t = tour_row(tid)
    if not t or t["phase"] != "registration":
        return False, "الان امکان لغو نیست."
    q("DELETE FROM t_players WHERE tid=? AND tg_id=?", (tid, uid), None, True)
    q("DELETE FROM t_clans WHERE tid=? AND clan_id IN (SELECT id FROM clans WHERE owner_id=?)", (tid, uid), None, True)
    return True, "لغو شد."


def registrants_rows(tid):
    t = tour_row(tid)
    if not t:
        return None, []
    if t["type"] == "solo":
        rows = q("""SELECT p.game_name, p.game_tag, u.first_name, u.username, p.tg_id, g.group_no
                    FROM t_players p LEFT JOIN users u ON u.tg_id=p.tg_id
                    LEFT JOIN t_groups g ON g.tid=p.tid AND g.tg_id=p.tg_id WHERE p.tid=? ORDER BY p.created_at""", (tid,))
        return t, [{"name": r[0], "tag": r[1], "tg": r[2] or "", "username": r[3] or "", "id": r[4], "group": r[5]} for r in rows]
    rows = q("""SELECT c.name, c.tag, u.first_name, u.username, c.owner_id, tc.seed FROM t_clans tc
                JOIN clans c ON c.id=tc.clan_id LEFT JOIN users u ON u.tg_id=c.owner_id
                WHERE tc.tid=? ORDER BY COALESCE(tc.seed, 9999), tc.created_at""", (tid,))
    return t, [{"name": r[0], "tag": r[1], "tg": r[2] or "", "username": r[3] or "", "id": r[4], "seed": r[5]} for r in rows]


def registrants_text(tid):
    t, rows = registrants_rows(tid)
    if not t:
        return "تورنمنت پیدا نشد."
    if not rows:
        return "هنوز کسی ثبت‌نام نکرده."
    out = []
    for i, r in enumerate(rows, 1):
        extra = " | گروه %d" % r["group"] if r.get("group") else (" | سید %d" % r["seed"] if r.get("seed") else "")
        out.append("%d. %s | %s | %s%s | آیدی %d%s" % (i, r["name"], r["tag"], r["tg"] or "-",
                                                      " @" + r["username"] if r["username"] else "", r["id"], extra))
    return "\n".join(out)


def do_draw(tid, forced=False):
    with DRAW_LOCK:
        t = tour_row(tid)
        if not t or t["phase"] != "registration":
            return None
        n = t_count(t)
        minimum = 8 if t["type"] == "solo" else 4
        if n < (minimum if forced else t["capacity"]):
            return None
        if t["type"] == "solo":
            ids = [r[0] for r in q("SELECT tg_id FROM t_players WHERE tid=?", (tid,))]
            total = t["capacity"] // 4 if n == t["capacity"] else (16 if n <= 64 else 32)
            random.shuffle(ids)
            q("DELETE FROM t_groups WHERE tid=?", (tid,), None, True)
            for i, uid in enumerate(ids):
                q("INSERT INTO t_groups(tid,tg_id,group_no) VALUES(?,?,?)", (tid, uid, i % total + 1), None, True)
        else:
            ids = [r[0] for r in q("SELECT clan_id FROM t_clans WHERE tid=?", (tid,))]
            total = 16 if n <= 16 else 32
            random.shuffle(ids)
            for i, cid in enumerate(ids):
                q("UPDATE t_clans SET seed=? WHERE tid=? AND clan_id=?", (i + 1, tid, cid), None, True)
        q("UPDATE tournaments SET phase='started', reg_open=0 WHERE id=?", (tid,), None, True)
    threading.Thread(target=announce_draw, args=(tid,), daemon=True).start()
    return total


def announce_draw(tid):
    t, rows = registrants_rows(tid)
    text = "🎲 قرعه‌کشی «%s» انجام شد و تورنمنت شروع شد!\nلیست کامل ثبت‌نام‌ها:\n\n%s" % (t["name"], registrants_text(tid))
    for uid in staff_ids():
        try:
            send_long(uid, text)
        except Exception:
            pass
    for r in rows:
        try:
            msg = ("🎲 قرعه‌کشی «%s» انجام شد. شما تو گروه %d هستید. موفق باشی!" % (t["name"], r["group"])) if t["type"] == "solo" \
                else ("🎲 قرعه‌کشی «%s» انجام شد. کلن «%s» سید %d شد." % (t["name"], r["name"], r["seed"] or 0))
            bot.send_message(r["id"], msg)
        except Exception:
            pass


def reset_tournament(tid):
    for table in ("t_players", "t_groups", "t_clans"):
        q("DELETE FROM %s WHERE tid=?" % table, (tid,), None, True)
    q("UPDATE tournaments SET phase='registration', reg_open=1 WHERE id=?", (tid,), None, True)


def delete_tournament(tid):
    reset_tournament(tid)
    q("DELETE FROM tournaments WHERE id=?", (tid,), None, True)


def tournament_detail(tid):
    t = tour_row(tid)
    if not t:
        return None
    t["count"] = t_count(t)
    if t["type"] == "solo":
        players = [{"name": r[0], "group": r[1]} for r in q(
            "SELECT p.game_name, g.group_no FROM t_players p LEFT JOIN t_groups g ON g.tid=p.tid AND g.tg_id=p.tg_id "
            "WHERE p.tid=? ORDER BY p.created_at", (tid,))]
        groups = {}
        for p in players:
            if p["group"]:
                groups.setdefault(str(p["group"]), []).append(p["name"])
        t["players"], t["groups"] = players, groups
    else:
        t["clans"] = [{"seed": r[0], "name": r[1], "tag": r[2], "level": r[3], "link": clan_link(r[2])} for r in q(
            "SELECT tc.seed, c.name, c.tag, c.level FROM t_clans tc JOIN clans c ON c.id=tc.clan_id "
            "WHERE tc.tid=? ORDER BY COALESCE(tc.seed, 9999), tc.created_at", (tid,))]
    return t


# ======================= کلن‌ها =======================
def add_clan(uid, name, tag, level, members, desc):
    name, tag = clean(name, 2, 30), norm_tag(tag)
    desc = clean(desc, 0, 200) if desc else ""
    try:
        level, members = int(level), int(members)
    except (TypeError, ValueError):
        return False, "سطح و تعداد اعضا باید عدد باشن."
    if not name or not tag or desc is None:
        return False, "نام یا تگ کلن معتبر نیست."
    if not (1 <= level <= 100) or not (1 <= members <= 50):
        return False, "سطح بین ۱ تا ۱۰۰ و اعضا بین ۱ تا ۵۰ باشه."
    if q("SELECT COUNT(*) FROM clans WHERE owner_id=? AND status!='rejected'", (uid,), "one")[0] >= MAX_CLANS_PER_USER:
        return False, "حداکثر %d کلن می‌تونی ثبت کنی." % MAX_CLANS_PER_USER
    if q("SELECT 1 FROM clans WHERE tag=? AND status!='rejected'", (tag,), "one"):
        return False, "این کلن قبلاً ثبت شده است."
    cid = q("INSERT INTO clans(owner_id,name,tag,level,members,description,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (uid, name, tag, level, members, desc, "pending", now()), "id", True)
    kb = types.InlineKeyboardMarkup()
    kb.row(types.InlineKeyboardButton("✅ تأیید", callback_data="cl:ok:%d" % cid),
           types.InlineKeyboardButton("❌ رد", callback_data="cl:no:%d" % cid))
    notify_staff("🛡 کلن جدید #%d\nنام: %s\nتگ: %s\nسطح: %d | اعضا: %d\nتوضیح: %s\nثبت‌کننده: %s\nلینک: %s" %
                 (cid, name, tag, level, members, desc or "-", who(uid), clan_link(tag)), kb)
    return True, "کلن ثبت شد و منتظر تأیید ادمینه."


def who(uid):
    u = q("SELECT first_name, username FROM users WHERE tg_id=?", (uid,), "one")
    return ("%s @%s" % (u[0], u[1]) if u and u[1] else (u[0] if u else str(uid)))


def review_clan(cid, ok):
    row = q("SELECT owner_id,name,tag,status FROM clans WHERE id=?", (cid,), "one")
    if not row or row[3] != "pending":
        return None
    q("UPDATE clans SET status=? WHERE id=?", ("approved" if ok else "rejected", cid), None, True)
    try:
        bot.send_message(row[0], "کلن «%s» %s" % (row[1], "تأیید شد ✅ حالا می‌تونی تو تورنمنت‌های کلنی ثبتش کنی." if ok else "رد شد ❌"))
    except Exception:
        pass
    return row


# ======================= فروشگاه =======================
HERO_NAMES = ("Barbarian King", "Archer Queen", "Grand Warden", "Royal Champion", "Minion Prince", "Dragon Duke")
EQ_MAX = {"common": 18, "epic": 27, "legendary": 33}
# لیست ابلیتی‌های (Hero Equipment) هر هیرو. اگه چیزی کم یا اشتباه بود، همین‌جا اضافه یا اصلاحش کن.
_EQ = {
    "Barbarian King": [("Barbarian Puppet", "common"), ("Rage Vial", "common"), ("Earthquake Boots", "epic"), ("Vampstache", "epic"),
                       ("Giant Gauntlet", "epic"), ("Spiky Ball", "epic"), ("Snake Bracelet", "epic")],
    "Archer Queen": [("Archer Puppet", "common"), ("Invisibility Vial", "common"), ("Giant Arrow", "epic"), ("Healer Puppet", "epic"),
                     ("Frozen Arrow", "epic"), ("Magic Mirror", "epic"), ("Action Figure", "epic")],
    "Grand Warden": [("Eternal Tome", "common"), ("Life Gem", "common"), ("Rage Gem", "epic"), ("Healing Tome", "epic"),
                     ("Fireball", "epic"), ("Lavaloon Puppet", "epic")],
    "Royal Champion": [("Royal Gem", "common"), ("Seeking Shield", "common"), ("Hog Rider Puppet", "epic"), ("Haste Vial", "epic"),
                       ("Rocket Spear", "epic"), ("Electro Boots", "epic")],
    "Minion Prince": [("Dark Orb", "common"), ("Henchmen Puppet", "epic"), ("Metal Pants", "epic"), ("Noble Iron", "epic"), ("Dark Crown", "epic")],
}


def slug(n):
    return re.sub(r"[^a-z0-9]+", "-", str(n).lower()).strip("-")


def equipment_list():
    return [{"id": r[0], "name": r[1], "name_fa": r[2] or "", "hero": r[3], "rarity": r[4], "max": EQ_MAX.get(r[4], 18)}
            for r in q("SELECT id,name,name_fa,hero,rarity FROM equipment ORDER BY id")]


def eq_map():
    return {e["name"]: e for e in equipment_list()}


class CocError(Exception):
    pass


def coc_lookup(tag):
    tag = norm_tag(tag)
    if not tag:
        raise CocError("تگ معتبر نیست.")
    if not COC_KEY:
        raise CocError("دریافت خودکار هنوز فعال نشده؛ مشخصات رو دستی وارد کن.")
    req = urllib.request.Request(COC_BASE + "/players/" + urllib.parse.quote(tag, safe=""),
                                 headers={"Authorization": "Bearer " + COC_KEY, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise CocError({404: "این تگ تو بازی پیدا نشد.", 403: "کلید API یا آی‌پی سرور مجاز نیست.",
                        429: "درخواست زیاده، کمی بعد امتحان کن."}.get(e.code, "خطا از سمت بازی (%d)." % e.code))
    except Exception:
        raise CocError("اتصال به سرور بازی ممکن نشد.")
    eqs = eq_map()
    home = lambda key: [x for x in data.get(key, []) if x.get("village", "home") == "home"]
    heroes = [{"name": h.get("name"), "level": int(h.get("level", 0)), "max": int(h.get("maxLevel", 0))}
              for h in home("heroes") if h.get("name") in HERO_NAMES]
    equip = [{"name": e.get("name"), "level": int(e.get("level", 0)), "max": int(e.get("maxLevel", 0))}
             for e in home("heroEquipment") if e.get("name") in eqs]
    items = home("heroes") + home("troops") + home("spells") + home("heroEquipment")
    cur = sum(int(i.get("level", 0)) for i in items)
    mx = sum(int(i.get("maxLevel", 0)) for i in items)
    return {"tag": tag, "name": data.get("name", ""), "th": int(data.get("townHallLevel", 0)), "exp": int(data.get("expLevel", 0)),
            "heroes": heroes, "equipment": equip, "units_percent": round(cur * 100 / mx) if mx else None}


def decode_photo(s):
    if not isinstance(s, str) or not s.startswith("data:image/jpeg;base64,"):
        return None
    try:
        raw = base64.b64decode(s.split(",", 1)[1], validate=True)
    except Exception:
        return None
    return raw if len(raw) <= 450000 and raw[:3] == b"\xff\xd8\xff" else None


_DIG = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def int_in(v, lo, hi, optional=False):
    if v is None or (isinstance(v, str) and not v.strip()):
        if optional:
            return None
        raise ValueError
    s = re.sub(r"[,\s٬،]", "", str(v).translate(_DIG))
    if not re.fullmatch(r"\d{1,12}", s):
        raise ValueError
    n = int(s)
    if not lo <= n <= hi:
        raise ValueError
    return n


def norm_tag_loose(text):
    if not isinstance(text, str):
        return None
    t = text.strip().upper().replace("O", "0").lstrip("#")
    return "#" + t if re.fullmatch(r"[0-9A-Z]{2,20}", t) else None


def add_listing(uid, d):
    u = q("SELECT username FROM users WHERE tg_id=?", (uid,), "one")
    if not u or not u[0]:
        return False, "برای فروش باید تو تلگرام یوزرنیم داشته باشی (Settings > Username) و بعد یک بار ربات رو /start کنی."
    tag = norm_tag_loose(d.get("tag"))
    if not tag:
        return False, "تگ اکانت معتبر نیست (فقط حرف و عدد انگلیسی، مثل #2PP0LQ8)."
    try:
        price = int_in(d.get("price"), 1, 10 ** 12)
    except ValueError:
        return False, "قیمت باید فقط عدد باشه (مثلاً 2500000)."
    try:
        th = int_in(d.get("th"), 1, 99)
    except ValueError:
        return False, "لول تاون‌هال باید عدد باشه."
    try:
        exp = int_in(d.get("exp_level"), 0, 99999, True)
    except ValueError:
        return False, "لول اکانت باید عدد باشه (یا خالی بذار)."
    try:
        up = int_in(d.get("units_percent"), 0, 9999, True)
        mp = int_in(d.get("map_percent"), 0, 9999, True)
    except ValueError:
        return False, "درصد مکس باید عدد باشه (یا خالی بذار)."
    heroes = []
    try:
        for h in d.get("heroes") or []:
            lvl = int_in(h.get("level"), 0, 99999, True) or 0
            if h.get("name") in HERO_NAMES and lvl > 0:
                heroes.append({"name": h["name"], "level": lvl, "max": int_in(h.get("max"), 0, 99999, True) or 0})
    except (ValueError, AttributeError, TypeError):
        return False, "لول هیروها باید عدد باشه."
    equip = []
    eqs = eq_map()
    try:
        for e in d.get("equipment") or []:
            lvl = int_in(e.get("level"), 0, 99999, True) or 0
            if lvl <= 0:
                continue
            info = eqs.get(e.get("name"))
            if info:
                equip.append({"name": info["name"], "level": lvl, "max": info["max"]})
            elif e.get("hero") in HERO_NAMES:
                nm = clean(e.get("name"), 2, 40)
                if nm:
                    equip.append({"name": nm, "level": lvl, "max": 0, "hero": e["hero"], "custom": True})
    except (ValueError, AttributeError, TypeError):
        return False, "لول ابلیتی‌ها باید عدد باشه."
    desc = clean(d.get("description") or "", 0, 3000)
    if desc is None:
        return False, "توضیحات زیادی طولانیه."
    photos = d.get("photos") or []
    if not isinstance(photos, list) or len(photos) > 8:
        return False, "حداکثر ۸ عکس می‌شه گذاشت."
    raws = [decode_photo(p) for p in photos]
    if not all(raws):
        return False, "یکی از عکس‌ها معتبر نیست یا زیادی بزرگه."
    if q("SELECT 1 FROM listings WHERE tag=? AND status IN ('pending','approved')", (tag,), "one"):
        return False, "برای این تگ قبلاً آگهی ثبت شده است."
    if q("SELECT COUNT(*) FROM listings WHERE seller_id=? AND status IN ('pending','approved')", (uid,), "one")[0] >= 50:
        return False, "حداکثر ۵۰ آگهی فعال می‌تونی داشته باشی."
    owned = len(equip)
    maxed = sum(1 for e in equip if e["max"] and e["level"] >= e["max"])
    lid = q("INSERT INTO listings(seller_id,tag,price,th,exp_level,heroes,abilities,abilities_max,units_percent,map_percent,"
            "description,photos,status,created_at,equipment) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (uid, tag, price, th, exp, json.dumps(heroes), owned, maxed, up, mp, desc, "[]", "pending", now(), json.dumps(equip)), "id", True)
    names = []
    for i, raw in enumerate(raws):
        fn = "l%d_%d.jpg" % (lid, i)
        with open(os.path.join(UPLOAD_DIR, fn), "wb") as f:
            f.write(raw)
        names.append(fn)
    q("UPDATE listings SET photos=? WHERE id=?", (json.dumps(names), lid), None, True)
    kb = types.InlineKeyboardMarkup()
    kb.row(types.InlineKeyboardButton("✅ تأیید", callback_data="ls:ok:%d" % lid),
           types.InlineKeyboardButton("❌ رد", callback_data="ls:no:%d" % lid))
    notify_staff("🛒 آگهی جدید #%d\nتگ: %s | TH%d | قیمت: %s تومان\nفروشنده: %s\n(عکس‌ها و جزئیات تو پنل سایت)" %
                 (lid, tag, th, format(price, ","), who(uid)), kb)
    return True, "آگهی ثبت شد و بعد از تأیید ادمین تو فروشگاه میاد."


# ======================= تصاویر (آپلود از پنل) =======================
def valid_asset_key(key):
    if not isinstance(key, str):
        return False
    if re.fullmatch(r"th-\d{1,2}", key):
        return 1 <= int(key[3:]) <= 25
    if key.startswith("hero-"):
        return key[5:] in {slug(h) for h in HERO_NAMES}
    m = re.fullmatch(r"eq-([a-z0-9-]+)-(\d{1,2})", key)
    if m:
        for e in equipment_list():
            if slug(e["name"]) == m.group(1):
                return 1 <= int(m.group(2)) <= e["max"]
    return False


def image_ext(raw):
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if raw[:3] == b"\xff\xd8\xff":
        return "jpg"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "webp"
    return None


def find_asset(key):
    for ext in ("png", "webp", "jpg"):
        fp = os.path.join(ASSET_DIR, key + "." + ext)
        if os.path.isfile(fp):
            return fp, ext
    return None, None


def remove_asset_files(key):
    for ext in ("png", "webp", "jpg"):
        try:
            os.remove(os.path.join(ASSET_DIR, key + "." + ext))
        except OSError:
            pass


def bump_assets():
    set_setting("asset_v", int(get_setting("asset_v", "0")) + 1)


def upload_asset(key, data_url):
    if not valid_asset_key(key):
        return False, "کلید تصویر نامعتبره (هیرو، لول تاون‌هال یا لول ابلیتی وجود نداره)."
    m = re.fullmatch(r"data:image/(?:png|jpeg|webp);base64,([A-Za-z0-9+/=]+)", data_url if isinstance(data_url, str) else "")
    if not m:
        return False, "فقط فایل PNG، JPG یا WEBP قبوله."
    try:
        raw = base64.b64decode(m.group(1), validate=True)
    except Exception:
        return False, "فایل خراب بود."
    ext = image_ext(raw)
    if not ext or len(raw) > 1500000:
        return False, "فایل تصویر معتبر نیست یا بزرگ‌تر از ۱.۵ مگابایته."
    remove_asset_files(key)
    with open(os.path.join(ASSET_DIR, key + "." + ext), "wb") as f:
        f.write(raw)
    bump_assets()
    return True, "تصویر ذخیره شد."


def add_equipment(d):
    hero, rarity = d.get("hero"), d.get("rarity")
    name = clean(d.get("name_en"), 2, 40)
    fa = clean(d.get("name_fa") or "", 0, 40)
    if hero not in HERO_NAMES or rarity not in EQ_MAX:
        return False, "هیرو یا نوع ابلیتی نامعتبره."
    if not name or not re.fullmatch(r"[A-Za-z0-9 '\-]+", name) or not slug(name):
        return False, "اسم انگلیسی فقط حرف و عدد انگلیسی باشه."
    if fa is None:
        return False, "اسم فارسی بیش از حد طولانیه."
    if any(slug(e["name"]) == slug(name) for e in equipment_list()):
        return False, "ابلیتی با این اسم قبلاً هست."
    q("INSERT INTO equipment(name,name_fa,hero,rarity) VALUES(?,?,?,?)", (name, fa, hero, rarity), None, True)
    return True, "ابلیتی اضافه شد."


def del_equipment(eid):
    row = q("SELECT name FROM equipment WHERE id=?", (eid,), "one")
    if not row:
        return False, "پیدا نشد."
    base = "eq-" + slug(row[0]) + "-"
    for fn in os.listdir(ASSET_DIR):
        if fn.startswith(base):
            try:
                os.remove(os.path.join(ASSET_DIR, fn))
            except OSError:
                pass
    q("DELETE FROM equipment WHERE id=?", (eid,), None, True)
    bump_assets()
    return True, "حذف شد."


def delete_listing(lid):
    row = q("SELECT photos FROM listings WHERE id=?", (lid,), "one")
    if row:
        for fn in json.loads(row[0] or "[]"):
            try:
                os.remove(os.path.join(UPLOAD_DIR, os.path.basename(fn)))
            except OSError:
                pass
    q("DELETE FROM listings WHERE id=?", (lid,), None, True)


def review_listing(lid, ok):
    row = q("SELECT seller_id, tag, status FROM listings WHERE id=?", (lid,), "one")
    if not row or row[2] != "pending":
        return None
    if ok:
        q("UPDATE listings SET status='approved' WHERE id=?", (lid,), None, True)
    else:
        delete_listing(lid)
    try:
        bot.send_message(row[0], "آگهی اکانت %s %s" % (row[1], "تأیید شد و تو فروشگاه نمایش داده می‌شه ✅" if ok else "رد شد ❌"))
    except Exception:
        pass
    return row


def listing_dict(r):
    return {"id": r[0], "tag": r[1], "price": r[2], "th": r[3], "exp_level": r[4], "heroes": json.loads(r[5] or "[]"),
            "abilities": r[6], "abilities_max": r[7], "units_percent": r[8], "map_percent": r[9], "description": r[10],
            "photos": ["/uploads/" + os.path.basename(f) for f in json.loads(r[11] or "[]")], "seller": r[12] or "", "status": r[13],
            "equipment": json.loads(r[14] or "[]")}


LISTING_COLS = ("l.id,l.tag,l.price,l.th,l.exp_level,l.heroes,l.abilities,l.abilities_max,l.units_percent,l.map_percent,"
                "l.description,l.photos,u.username,l.status,l.equipment")


def clans_public():
    return [{"name": r[0], "tag": r[1], "level": r[2], "members": r[3], "description": r[4], "link": clan_link(r[1])}
            for r in q("SELECT name,tag,level,members,description FROM clans WHERE status='approved' ORDER BY id")]


def store_payload():
    return [listing_dict(r) for r in q("SELECT %s FROM listings l LEFT JOIN users u ON u.tg_id=l.seller_id "
                                       "WHERE l.status='approved' ORDER BY l.id DESC LIMIT 200" % LISTING_COLS)]


def listing_action(uid, d):
    try:
        lid = int(d.get("id"))
    except (TypeError, ValueError):
        return False, "آگهی نامعتبره."
    row = q("SELECT status FROM listings WHERE id=? AND seller_id=?", (lid, uid), "one")
    if not row:
        return False, "آگهی پیدا نشد."
    if d.get("action") == "sold" and row[0] == "approved":
        q("UPDATE listings SET status='sold' WHERE id=?", (lid,), None, True)
    elif d.get("action") == "delete":
        delete_listing(lid)
    else:
        return False, "دستور نامعتبر."
    return True, "انجام شد."


# ======================= داده‌های API =======================
def state_payload():
    return {"bot": BOT_USERNAME, "tournaments": tours_list(), "coc": bool(COC_KEY), "heroes": list(HERO_NAMES), "equipment": equipment_list(),
            "av": int(get_setting("asset_v", "0"))}


def me_payload(uid):
    u = q("SELECT first_name, username FROM users WHERE tg_id=?", (uid,), "one") or ("", "")
    regs = [{"tid": r[0], "name": r[1], "type": "solo", "group": r[2]} for r in q(
        "SELECT t.id, t.name, g.group_no FROM t_players p JOIN tournaments t ON t.id=p.tid "
        "LEFT JOIN t_groups g ON g.tid=p.tid AND g.tg_id=p.tg_id WHERE p.tg_id=?", (uid,))]
    regs += [{"tid": r[0], "name": r[1], "type": "clan", "clan": r[2], "seed": r[3]} for r in q(
        "SELECT t.id, t.name, c.name, tc.seed FROM t_clans tc JOIN tournaments t ON t.id=tc.tid "
        "JOIN clans c ON c.id=tc.clan_id WHERE c.owner_id=?", (uid,))]
    clans = [{"id": r[0], "name": r[1], "tag": r[2], "status": r[3]} for r in q(
        "SELECT id,name,tag,status FROM clans WHERE owner_id=? ORDER BY id", (uid,))]
    listings = [{"id": r[0], "tag": r[1], "price": r[2], "th": r[3], "status": r[4]} for r in q(
        "SELECT id,tag,price,th,status FROM listings WHERE seller_id=? AND status!='sold' ORDER BY id DESC", (uid,))]
    return {"id": uid, "name": u[0], "username": u[1], "role": role_of(uid), "regs": regs, "clans": clans, "listings": listings}


def overview_payload(uid):
    cl = lambda s: [{"id": r[0], "name": r[1], "tag": r[2], "level": r[3], "members": r[4], "description": r[5]}
                    for r in q("SELECT id,name,tag,level,members,description FROM clans WHERE status=? ORDER BY id", (s,))]
    ls = lambda s: [listing_dict(r) for r in q("SELECT %s FROM listings l LEFT JOIN users u ON u.tg_id=l.seller_id "
                                               "WHERE l.status=? ORDER BY l.id DESC LIMIT 100" % LISTING_COLS, (s,))]
    out = {"role": role_of(uid), "tournaments": tours_list(), "pendingClans": cl("pending"), "approvedClans": cl("approved"),
           "pendingListings": ls("pending"), "approvedListings": ls("approved")}
    if is_owner(uid):
        out["admins"] = [r[0] for r in q("SELECT tg_id FROM admins")]
    return out


def admin_action(uid, d):
    act = d.get("action")
    owner = is_owner(uid)
    tid = int(d.get("id", 0)) if str(d.get("id", "0")).lstrip("-").isdigit() else 0
    if act in ("approve_clan", "reject_clan"):
        return (True, "انجام شد.") if review_clan(tid, act == "approve_clan") else (False, "پیدا نشد یا قبلاً بررسی شده.")
    if act in ("approve_listing", "reject_listing"):
        return (True, "انجام شد.") if review_listing(tid, act == "approve_listing") else (False, "پیدا نشد یا قبلاً بررسی شده.")
    if act == "add_tournament":
        ok, msg, _ = create_tournament(d.get("name"), d.get("type"), d.get("capacity"))
        return ok, msg
    if act in ("set_reg", "set_live", "del_tournament"):
        t = tour_row(tid)
        if not t:
            return False, "تورنمنت پیدا نشد."
        if act == "set_reg":
            if t["phase"] != "registration":
                return False, "تورنمنت شروع شده."
            q("UPDATE tournaments SET reg_open=? WHERE id=?", (1 if d.get("open") else 0, tid), None, True)
        elif act == "set_live":
            url = safe_live_url(d.get("url"))
            if url is None:
                return False, "فقط لینک https از آپارات، یوتیوب یا توییچ قبوله."
            q("UPDATE tournaments SET live_url=? WHERE id=?", (url, tid), None, True)
        else:
            if not owner and t_count(t) > 0:
                return False, "تورنمنتی که ثبت‌نام داره رو فقط مالک می‌تونه حذف کنه."
            delete_tournament(tid)
        return True, "انجام شد."
    if not owner:
        return False, "این کار فقط برای مالکه."
    if act == "draw":
        return (True, "انجام شد.") if do_draw(tid, forced=True) else (False, "حداقل بازیکن لازمه یا قرعه‌کشی قبلاً انجام شده.")
    if act == "reset":
        reset_tournament(tid)
    elif act == "del_player":
        q("DELETE FROM t_players WHERE tid=? AND tg_id=?", (tid, int(d.get("uid", 0))), None, True)
        q("DELETE FROM t_groups WHERE tid=? AND tg_id=?", (tid, int(d.get("uid", 0))), None, True)
    elif act == "del_clan":
        q("DELETE FROM t_clans WHERE clan_id=?", (tid,), None, True)
        q("DELETE FROM clans WHERE id=?", (tid,), None, True)
    elif act == "del_listing":
        delete_listing(tid)
    elif act == "add_equipment":
        return add_equipment(d)
    elif act == "del_equipment":
        return del_equipment(tid)
    elif act == "set_equip_fa":
        fa = clean(d.get("name_fa") or "", 0, 40)
        if fa is None:
            return False, "اسم فارسی نامعتبره."
        q("UPDATE equipment SET name_fa=? WHERE id=?", (fa, tid), None, True)
    elif act == "del_asset":
        key = d.get("key")
        if not valid_asset_key(key):
            return False, "کلید نامعتبره."
        remove_asset_files(key)
        bump_assets()
    elif act == "add_admin":
        q("INSERT OR IGNORE INTO admins(tg_id) VALUES(?)", (tid,), None, True)
    elif act == "rm_admin":
        q("DELETE FROM admins WHERE tg_id=?", (tid,), None, True)
    else:
        return False, "دستور نامعتبر."
    return True, "انجام شد."


def stats_text():
    n = lambda sql: q(sql, (), "one")[0]
    return "🏆 تورنمنت‌ها: %d (فعال برای ثبت‌نام: %d)\n🛡 کلن: %d تأییدشده | %d منتظر\n🛒 آگهی: %d فعال | %d منتظر" % (
        n("SELECT COUNT(*) FROM tournaments"), n("SELECT COUNT(*) FROM tournaments WHERE phase='registration' AND reg_open=1"),
        n("SELECT COUNT(*) FROM clans WHERE status='approved'"), n("SELECT COUNT(*) FROM clans WHERE status='pending'"),
        n("SELECT COUNT(*) FROM listings WHERE status='approved'"), n("SELECT COUNT(*) FROM listings WHERE status='pending'"))


# ======================= وب‌سرور =======================
class Handler(BaseHTTPRequestHandler):
    server_version = "Warhold"

    def log_message(self, *a):
        pass

    def send(self, code, obj=None, raw=None, ctype="application/json; charset=utf-8", cache="no-store"):
        body = raw if raw is not None else json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(body)

    def err(self, code, msg):
        self.send(code, {"error": msg})

    def ip(self):
        xff = self.headers.get("X-Forwarded-For")
        return xff.split(",")[0].strip() if xff else self.client_address[0]

    def user(self):
        auth = self.headers.get("Authorization", "")
        return user_from_token(auth[7:]) if auth.startswith("Bearer ") and len(auth) > 10 else None

    def json_body(self, limit=20000):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > limit:
                return None
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return None

    def do_OPTIONS(self):
        self.send(204, raw=b"")

    def do_GET(self):
        try:
            url = urllib.parse.urlparse(self.path)
            path, qs = url.path, urllib.parse.parse_qs(url.query)
            qid = int((qs.get("id") or qs.get("tid") or ["0"])[0]) if (qs.get("id") or qs.get("tid") or ["0"])[0].isdigit() else 0
            if path in ("/", "/index.html"):
                if not os.path.exists(SITE_FILE):
                    return self.err(404, "index.html کنار server.py نیست.")
                with open(SITE_FILE, "rb") as f:
                    return self.send(200, raw=f.read(), ctype="text/html; charset=utf-8", cache="no-cache")
            m = re.fullmatch(r"/uploads/(l\d+_\d\.jpg)", path)
            if m:
                fp = os.path.join(UPLOAD_DIR, m.group(1))
                if not os.path.exists(fp):
                    return self.err(404, "پیدا نشد.")
                with open(fp, "rb") as f:
                    return self.send(200, raw=f.read(), ctype="image/jpeg", cache="public, max-age=86400")
            m = re.fullmatch(r"/assets/([a-z0-9-]+)", path)
            if m:
                fp, ext = find_asset(m.group(1))
                if not fp:
                    return self.err(404, "پیدا نشد.")
                ct = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}[ext]
                with open(fp, "rb") as f:
                    return self.send(200, raw=f.read(), ctype=ct,
                                     cache="public, max-age=31536000, immutable" if "v=" in url.query else "public, max-age=300")
            if path == "/api/state":
                return self.send(200, state_payload())
            if path == "/api/tournament":
                d = tournament_detail(qid)
                return self.send(200, d) if d else self.err(404, "تورنمنت پیدا نشد.")
            if path == "/api/clans":
                return self.send(200, {"clans": clans_public()})
            if path == "/api/store":
                return self.send(200, {"listings": store_payload()})
            uid = self.user()
            if path == "/api/me":
                return self.send(200, me_payload(uid)) if uid else self.err(401, "وارد نشده‌اید.")
            if path == "/api/admin/assets":
                if not uid or not is_owner(uid):
                    return self.err(403, "فقط مالک.")
                return self.send(200, {"keys": sorted(fn.rsplit(".", 1)[0] for fn in os.listdir(ASSET_DIR))})
            if path in ("/api/admin/overview", "/api/admin/registrants"):
                if not uid or not is_staff(uid):
                    return self.err(403, "دسترسی ندارید.")
                if path == "/api/admin/overview":
                    return self.send(200, overview_payload(uid))
                t, rows = registrants_rows(qid)
                return self.send(200, {"type": t["type"], "rows": rows}) if t else self.err(404, "تورنمنت پیدا نشد.")
            self.err(404, "پیدا نشد.")
        except Exception as e:
            print("GET error:", e)
            self.err(500, "خطای سرور.")

    def do_POST(self):
        try:
            path = self.path.split("?")[0]
            d = self.json_body(3000000 if path in ("/api/listings", "/api/admin/asset") else 20000)
            if d is None or not isinstance(d, dict):
                return self.err(400, "درخواست نامعتبر یا زیادی بزرگ.")
            if path == "/api/login":
                if limited("login:" + self.ip(), 10, 600):
                    return self.err(429, "تلاش زیاد. چند دقیقه بعد دوباره امتحان کن.")
                code = re.sub(r"\D", "", str(d.get("code", "")))
                res = login_with_code(code) if len(code) == 8 else None
                if not res:
                    return self.err(400, "کد اشتباهه یا منقضی شده.")
                return self.send(200, {"token": res[0], "me": me_payload(res[1])})
            uid = self.user()
            if not uid:
                return self.err(401, "اول وارد شو.")
            if limited("w:%d" % uid, 60, 600):
                return self.err(429, "درخواست زیاد.")
            ok_msg = lambda r: self.send(200, {"ok": True, "message": r[1]}) if r[0] else self.err(400, r[1])
            if path == "/api/logout":
                auth = self.headers.get("Authorization", "")[7:]
                q("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(auth.encode()).hexdigest(),), None, True)
                return self.send(200, {"ok": True})
            tid = d.get("tid")
            tid = int(tid) if str(tid).isdigit() else 0
            if path == "/api/register-player":
                return ok_msg(register_player(tid, uid, d.get("game_name"), d.get("game_tag")))
            if path == "/api/register-clan":
                return ok_msg(register_clan(tid, uid, d.get("clan_id")))
            if path == "/api/cancel-registration":
                return ok_msg(cancel_registration(tid, uid))
            if path == "/api/clans":
                return ok_msg(add_clan(uid, d.get("name"), d.get("tag"), d.get("level"), d.get("members"), d.get("description")))
            if path == "/api/coc-lookup":
                if limited("coc:%d" % uid, 15, 600):
                    return self.err(429, "درخواست زیاد.")
                try:
                    return self.send(200, coc_lookup(d.get("tag")))
                except CocError as e:
                    return self.err(400, str(e))
            if path == "/api/listings":
                return ok_msg(add_listing(uid, d))
            if path == "/api/listing-action":
                return ok_msg(listing_action(uid, d))
            if path == "/api/admin/asset":
                if not is_owner(uid):
                    return self.err(403, "فقط مالک.")
                return ok_msg(upload_asset(d.get("key"), d.get("data")))
            if path == "/api/admin/action":
                if not is_staff(uid):
                    return self.err(403, "دسترسی ندارید.")
                try:
                    return ok_msg(admin_action(uid, d))
                except (TypeError, ValueError):
                    return self.err(400, "ورودی نامعتبر.")
            self.err(404, "پیدا نشد.")
        except Exception as e:
            print("POST error:", e)
            self.err(500, "خطای سرور.")


# ======================= ربات تلگرام =======================
BTN_CODE = "🔑 دریافت کد ورود"
BTN_ME = "👤 وضعیت من"
BTN_SITE = "🌐 باز کردن سایت"
BTN_ADMIN = "⚙️ مدیریت"
A_STATS = "📊 آمار"
A_LIST = "📋 لیست ثبت‌نام‌ها"
A_CLANS = "🛡 کلن‌های منتظر"
A_LISTINGS = "🛒 آگهی‌های منتظر"
A_TOURS = "🏆 تورنمنت‌ها"
A_ADMINS = "🧑‍💼 ادمین‌ها"
A_BACK = "⬅️ بازگشت"
ALL_BTNS = (BTN_CODE, BTN_ME, BTN_SITE, BTN_ADMIN, A_STATS, A_LIST, A_CLANS, A_LISTINGS, A_TOURS, A_ADMINS, A_BACK)


def main_kb(uid):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(BTN_CODE, BTN_ME)
    row = []
    if SITE_URL:
        row.append(types.KeyboardButton(BTN_SITE, web_app=types.WebAppInfo(SITE_URL)))
    if is_staff(uid):
        row.append(BTN_ADMIN)
    if row:
        kb.row(*row)
    return kb


def admin_kb(uid):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(A_STATS, A_LIST)
    kb.row(A_CLANS, A_LISTINGS)
    kb.row(A_TOURS, A_ADMINS) if is_owner(uid) else kb.row(A_TOURS)
    kb.row(A_BACK)
    return kb


def send_code(m):
    code = make_code(m.from_user.id)
    bot.send_message(m.chat.id, "🔑 کد ورود شما:\n\n%s\n\nاین کد ۵ دقیقه اعتبار داره. تو سایت وارد کن و به کسی نده." % code,
                     reply_markup=main_kb(m.from_user.id))


@bot.message_handler(commands=["start"])
def on_start(m):
    upsert_user(m.from_user)
    parts = (m.text or "").split(maxsplit=1)
    if len(parts) == 2 and parts[1].strip() == "login":
        return send_code(m)
    bot.send_message(m.chat.id, "سلام %s! به وارهولد خوش اومدی ⚔️\nبرای ورود به سایت، «%s» رو بزن." %
                     (m.from_user.first_name or "", BTN_CODE), reply_markup=main_kb(m.from_user.id))


@bot.message_handler(commands=["id"])
def on_id(m):
    bot.reply_to(m, "آیدی عددی شما: %d" % m.from_user.id)


@bot.message_handler(func=lambda m: m.text == BTN_CODE)
def on_code(m):
    upsert_user(m.from_user)
    send_code(m)


@bot.message_handler(func=lambda m: m.text == BTN_ME)
def on_me(m):
    me = me_payload(m.from_user.id)
    lines = []
    for r in me["regs"]:
        lines.append("🏆 %s: %s" % (r["name"], ("گروه %d" % r["group"] if r.get("group") else "ثبت‌نام شده") if r["type"] == "solo"
                                    else "کلن %s%s" % (r["clan"], " - سید %d" % r["seed"] if r.get("seed") else "")))
    names = {"pending": "منتظر تأیید", "approved": "تأیید شده", "rejected": "رد شده"}
    for c in me["clans"]:
        lines.append("🛡 کلن %s (%s): %s" % (c["name"], c["tag"], names.get(c["status"], c["status"])))
    for l in me["listings"]:
        lines.append("🛒 آگهی %s (TH%d): %s" % (l["tag"], l["th"], "فعال" if l["status"] == "approved" else "منتظر تأیید"))
    bot.send_message(m.chat.id, "\n".join(lines) or "هنوز تو تورنمنت یا فروشگاه فعالیتی نداری. از سایت شروع کن.",
                     reply_markup=main_kb(m.from_user.id))


def staff_only(m, owner=False):
    uid = m.from_user.id
    if (owner and not is_owner(uid)) or (not owner and not is_staff(uid)):
        bot.send_message(m.chat.id, "این بخش مخصوص %s." % ("مالکه" if owner else "ادمین‌هاست"))
        return False
    return True


@bot.message_handler(func=lambda m: m.text == BTN_ADMIN)
def on_admin(m):
    if staff_only(m):
        bot.send_message(m.chat.id, "⚙️ پنل مدیریت\n\n" + stats_text(), reply_markup=admin_kb(m.from_user.id))


@bot.message_handler(func=lambda m: m.text == A_BACK)
def on_back(m):
    bot.send_message(m.chat.id, "منوی اصلی", reply_markup=main_kb(m.from_user.id))


@bot.message_handler(func=lambda m: m.text == A_STATS)
def on_stats(m):
    if staff_only(m):
        bot.send_message(m.chat.id, stats_text())


def tours_kb(prefix, owner_draw=False):
    kb = types.InlineKeyboardMarkup()
    for t in tours_list():
        label = "%s %s (%d/%d)" % ("🧍" if t["type"] == "solo" else "🛡", t["name"], t["count"], t["capacity"])
        if prefix == "rg":
            kb.row(types.InlineKeyboardButton(label, callback_data="rg:%d" % t["id"]))
        else:
            row = [types.InlineKeyboardButton(label, callback_data="tr:info:%d" % t["id"])]
            if t["phase"] == "registration":
                row.append(types.InlineKeyboardButton("🚪 بستن" if t["reg_open"] else "🚪 باز کن", callback_data="tr:reg:%d" % t["id"]))
                if owner_draw:
                    row.append(types.InlineKeyboardButton("🎲", callback_data="tr:draw:%d" % t["id"]))
            kb.row(*row)
    return kb


@bot.message_handler(func=lambda m: m.text == A_LIST)
def on_list(m):
    if staff_only(m):
        bot.send_message(m.chat.id, "لیست کدوم تورنمنت؟", reply_markup=tours_kb("rg"))


@bot.message_handler(func=lambda m: m.text == A_TOURS)
def on_tours(m):
    if staff_only(m):
        bot.send_message(m.chat.id, "تورنمنت‌ها (🎲 قرعه‌کشی دستی فقط مالک؛ ساخت تورنمنت جدید از پنل سایت):",
                         reply_markup=tours_kb("tr", is_owner(m.from_user.id)))


@bot.message_handler(func=lambda m: m.text == A_CLANS)
def on_pending(m):
    if not staff_only(m):
        return
    rows = q("SELECT id,name,tag,level,members,description FROM clans WHERE status='pending' ORDER BY id")
    if not rows:
        bot.send_message(m.chat.id, "کلن منتظری نیست.")
    for r in rows:
        kb = types.InlineKeyboardMarkup()
        kb.row(types.InlineKeyboardButton("✅ تأیید", callback_data="cl:ok:%d" % r[0]),
               types.InlineKeyboardButton("❌ رد", callback_data="cl:no:%d" % r[0]))
        bot.send_message(m.chat.id, "کلن #%d\nنام: %s\nتگ: %s\nسطح: %d | اعضا: %d\nتوضیح: %s\nلینک: %s" %
                         (r[0], r[1], r[2], r[3], r[4], r[5] or "-", clan_link(r[2])), reply_markup=kb)


@bot.message_handler(func=lambda m: m.text == A_LISTINGS)
def on_pending_listings(m):
    if not staff_only(m):
        return
    rows = q("SELECT id,tag,price,th,seller_id FROM listings WHERE status='pending' ORDER BY id")
    if not rows:
        bot.send_message(m.chat.id, "آگهی منتظری نیست.")
    for r in rows:
        kb = types.InlineKeyboardMarkup()
        kb.row(types.InlineKeyboardButton("✅ تأیید", callback_data="ls:ok:%d" % r[0]),
               types.InlineKeyboardButton("❌ رد", callback_data="ls:no:%d" % r[0]))
        bot.send_message(m.chat.id, "آگهی #%d\nتگ: %s | TH%d | %s تومان\nفروشنده: %s\n(عکس‌ها رو تو پنل سایت ببین)" %
                         (r[0], r[1], r[3], format(r[2], ","), who(r[4])), reply_markup=kb)


def admins_view(chat_id):
    kb = types.InlineKeyboardMarkup()
    rows = q("SELECT tg_id FROM admins")
    for r in rows:
        kb.row(types.InlineKeyboardButton("🗑 %s %d" % (who(r[0]), r[0]), callback_data="adm:rm:%d" % r[0]))
    kb.row(types.InlineKeyboardButton("➕ افزودن ادمین", callback_data="adm:add"))
    bot.send_message(chat_id, "🧑‍💼 ادمین‌ها (برای حذف روی اسم بزن):" if rows else "هنوز ادمینی نیست.", reply_markup=kb)


@bot.message_handler(func=lambda m: m.text == A_ADMINS)
def on_admins(m):
    if staff_only(m, owner=True):
        admins_view(m.chat.id)


def add_admin_step(m):
    t = (m.text or "").strip()
    if t in ALL_BTNS or t.startswith("/"):
        bot.send_message(m.chat.id, "لغو شد.")
        return
    if not t.isdigit():
        msg = bot.send_message(m.chat.id, "آیدی باید فقط عدد باشه. دوباره بفرست (یا یه دکمه بزن تا لغو بشه):")
        bot.register_next_step_handler(msg, add_admin_step)
        return
    q("INSERT OR IGNORE INTO admins(tg_id) VALUES(?)", (int(t),), None, True)
    bot.send_message(m.chat.id, "✅ ادمین اضافه شد. اون شخص باید یک بار ربات رو /start کنه.")
    admins_view(m.chat.id)


@bot.callback_query_handler(func=lambda c: True)
def on_callback(c):
    uid = c.from_user.id
    parts = c.data.split(":")
    if not is_staff(uid):
        return bot.answer_callback_query(c.id, "فقط ادمین", show_alert=True)
    kind = parts[0]
    if kind in ("cl", "ls"):
        ok = parts[1] == "ok"
        row = (review_clan if kind == "cl" else review_listing)(int(parts[2]), ok)
        if not row:
            return bot.answer_callback_query(c.id, "قبلاً بررسی شده یا پیدا نشد")
        bot.answer_callback_query(c.id, "انجام شد")
        try:
            bot.edit_message_text("%s: %s" % ("✅ تأیید شد" if ok else "❌ رد شد", row[1] if kind == "cl" else row[1]),
                                  c.message.chat.id, c.message.message_id)
        except Exception:
            pass
    elif kind == "rg":
        bot.answer_callback_query(c.id)
        send_long(c.message.chat.id, "📋 لیست ثبت‌نام‌ها:\n\n" + registrants_text(int(parts[1])))
    elif kind == "tr":
        tid = int(parts[2])
        t = tour_row(tid)
        if not t:
            return bot.answer_callback_query(c.id, "پیدا نشد")
        if parts[1] == "info":
            bot.answer_callback_query(c.id, "%s | %d/%d | %s" % (t["name"], t_count(t), t["capacity"],
                                                                "شروع شده" if t["phase"] == "started" else ("باز" if t["reg_open"] else "بسته")), show_alert=True)
        elif parts[1] == "reg":
            ok, msg = admin_action(uid, {"action": "set_reg", "id": tid, "open": not t["reg_open"]})
            bot.answer_callback_query(c.id, "انجام شد" if ok else msg, show_alert=not ok)
            try:
                bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=tours_kb("tr", is_owner(uid)))
            except Exception:
                pass
        elif parts[1] == "draw":
            if not is_owner(uid):
                return bot.answer_callback_query(c.id, "فقط مالک", show_alert=True)
            ok, msg = admin_action(uid, {"action": "draw", "id": tid})
            bot.answer_callback_query(c.id, "قرعه‌کشی انجام شد" if ok else msg, show_alert=not ok)
    elif kind == "adm":
        if not is_owner(uid):
            return bot.answer_callback_query(c.id, "فقط مالک", show_alert=True)
        if parts[1] == "rm":
            admin_action(uid, {"action": "rm_admin", "id": int(parts[2])})
            bot.answer_callback_query(c.id, "حذف شد")
            admins_view(c.message.chat.id)
        else:
            bot.answer_callback_query(c.id)
            msg = bot.send_message(c.message.chat.id, "آیدی عددی تلگرام ادمین جدید رو بفرست (اون شخص می‌تونه تو ربات /id بزنه):")
            bot.register_next_step_handler(msg, add_admin_step)


def run_bot():
    global BOT_USERNAME
    try:
        BOT_USERNAME = bot.get_me().username or ""
    except Exception as e:
        print("خطا تو اتصال به تلگرام:", e)
    bot.infinity_polling(skip_pending=True)


# ======================= اجرا =======================
if __name__ == "__main__":
    init_db()
    if BOT_TOKEN:
        threading.Thread(target=run_bot, daemon=True).start()
    else:
        print("BOT_TOKEN خالیه؛ فقط وب‌سایت بالا میاد و ربات خاموشه.")
    if not OWNER_ID:
        print("OWNER_ID خالیه؛ هیچ‌کس مالک نیست.")
    if TELEGRAM_RELAY:
        print("ربات از طریق رله به تلگرام وصل می‌شه.")
    if not COC_KEY:
        print("COC_API_KEY خالیه؛ دریافت خودکار مشخصات اکانت غیرفعاله (فروشنده‌ها دستی وارد می‌کنن).")
    print("سرور روی پورت %d بالا اومد..." % PORT)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
