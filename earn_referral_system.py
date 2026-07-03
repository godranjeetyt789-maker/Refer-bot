import os
import sqlite3
import asyncio
import logging
import threading
from datetime import datetime, timedelta
from functools import wraps

# ── Telegram ──
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes, TypeHandler, ApplicationHandlerStop
)

# ── Flask (Web Admin Panel) ──
from flask import Flask, request, session, redirect, url_for, jsonify
from jinja2 import Environment, DictLoader, select_autoescape
from werkzeug.security import generate_password_hash, check_password_hash

# ==========================================
# CONFIGURATION
# ==========================================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN environment variable set nahi hai! "
        "Render Dashboard -> Environment -> Add Environment Variable mein BOT_TOKEN daalo."
    )
ADMIN_TELEGRAM_ID = int(os.environ.get("ADMIN_ID", "6106058051"))
ADMIN_USERNAME    = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD    = os.environ.get("ADMIN_PASS", "admin123")

DEFAULT_REFERRAL_REWARD = 10
DEFAULT_DAILY_BONUS     = 5
DEFAULT_MIN_WITHDRAW    = 100

DB_FILE = os.environ.get("DB_FILE", "bot_database.db")

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ==========================================
# DATABASE
# ==========================================
def get_conn():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=20)
    conn.row_factory = sqlite3.Row
    return conn

def qdb(query, args=(), one=False, commit=False):
    conn = get_conn()
    cur  = conn.cursor()
    cur.execute(query, args)
    if commit:
        conn.commit()
        last_id = cur.lastrowid
        conn.close()
        return last_id
    rv = cur.fetchall()
    conn.close()
    return (rv[0] if rv else None) if one else rv

def init_db():
    tables = [
        """CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id BIGINT UNIQUE, username TEXT,
            join_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            total_referrals INT DEFAULT 0,
            successful_referrals INT DEFAULT 0,
            balance FLOAT DEFAULT 0.0,
            is_blocked BOOLEAN DEFAULT 0,
            last_bonus TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS referrals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            referrer_id BIGINT, referred_id BIGINT UNIQUE,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT, description TEXT, reward FLOAT, link TEXT,
            is_active BOOLEAN DEFAULT 1
        )""",
        """CREATE TABLE IF NOT EXISTS user_tasks (
            user_id BIGINT, task_id INTEGER,
            completed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, task_id)
        )""",
        """CREATE TABLE IF NOT EXISTS channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_name TEXT, channel_link TEXT, channel_id TEXT,
            is_active BOOLEAN DEFAULT 1
        )""",
        """CREATE TABLE IF NOT EXISTS withdraw_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id BIGINT, amount FLOAT, method TEXT, details TEXT,
            status TEXT DEFAULT 'Pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY, value TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS admin (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE, password TEXT
        )""",
    ]
    for t in tables:
        qdb(t, commit=True)

    defaults = {
        "referral_reward": str(DEFAULT_REFERRAL_REWARD),
        "daily_bonus":     str(DEFAULT_DAILY_BONUS),
        "min_withdraw":    str(DEFAULT_MIN_WITHDRAW),
        "welcome_message": "🎉 Earn Bot mein aapka swagat hai!\nNeeche menu se shuru karo.",
        "force_join":      "1",
    }
    for k, v in defaults.items():
        if not qdb("SELECT 1 FROM settings WHERE key=?", (k,), one=True):
            qdb("INSERT INTO settings (key,value) VALUES (?,?)", (k, v), commit=True)

    if not qdb("SELECT 1 FROM admin", one=True):
        qdb("INSERT INTO admin (username,password) VALUES (?,?)",
            (ADMIN_USERNAME, generate_password_hash(ADMIN_PASSWORD)), commit=True)

    try:
        qdb("ALTER TABLE users ADD COLUMN last_bonus TIMESTAMP", commit=True)
    except Exception:
        pass

init_db()

def get_setting(key, cast=str):
    row = qdb("SELECT value FROM settings WHERE key=?", (key,), one=True)
    try:
        return cast(row["value"]) if row else cast()
    except Exception:
        return cast()

def set_setting(key, value):
    qdb("INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)", (key, str(value)), commit=True)

# ==========================================
# BOT STATE
# ==========================================
USER_STATES    = {}
ADMIN_STATES   = {}
ADMIN_USER_MODE = set()

def is_admin(uid): return uid == ADMIN_TELEGRAM_ID

# ==========================================
# KEYBOARDS
# ==========================================
def main_kb():
    return ReplyKeyboardMarkup([
        [KeyboardButton("👤 Profile"),     KeyboardButton("📋 Tasks")],
        [KeyboardButton("🎁 Daily Bonus"), KeyboardButton("👥 Referrals")],
        [KeyboardButton("💰 Balance"),     KeyboardButton("💳 Withdraw")],
        [KeyboardButton("🏆 Leaderboard"), KeyboardButton("ℹ️ Help")],
    ], resize_keyboard=True)

def admin_kb():
    return ReplyKeyboardMarkup([
        [KeyboardButton("📊 Stats"),        KeyboardButton("👥 Users")],
        [KeyboardButton("📋 Tasks"),        KeyboardButton("📢 Channels")],
        [KeyboardButton("💳 Withdrawals"),  KeyboardButton("📣 Broadcast")],
        [KeyboardButton("⚙️ Settings"),     KeyboardButton("🔙 User View")],
    ], resize_keyboard=True)

def user_mode_kb():
    return ReplyKeyboardMarkup([
        [KeyboardButton("👤 Profile"),     KeyboardButton("📋 Tasks")],
        [KeyboardButton("🎁 Daily Bonus"), KeyboardButton("👥 Referrals")],
        [KeyboardButton("💰 Balance"),     KeyboardButton("💳 Withdraw")],
        [KeyboardButton("🏆 Leaderboard"), KeyboardButton("👑 Admin Panel")],
    ], resize_keyboard=True)

def cancel_kb():
    return ReplyKeyboardMarkup([["❌ Cancel"]], resize_keyboard=True)

# ==========================================
# FORCE JOIN — PROPER VERIFICATION
# ==========================================
async def get_not_joined_channels(context, uid):
    """Returns list of channels the user has NOT joined."""
    channels = qdb("SELECT * FROM channels WHERE is_active=1")
    not_joined = []
    for c in channels:
        try:
            m = await context.bot.get_chat_member(chat_id=c["channel_id"], user_id=uid)
            if m.status not in ("member", "administrator", "creator"):
                not_joined.append(c)
        except Exception as e:
            logger.error(f"Channel check error {c['channel_id']}: {e}")
            not_joined.append(c)
    return not_joined

def force_join_message(not_joined_channels):
    """Build force join text + keyboard."""
    text = (
        "🔒 *Bot use karne ke liye pehle in channels ko join karo:*\n\n"
        "Sab join karne ke baad ✅ button dabao."
    )
    buttons = []
    row = []
    for i, c in enumerate(not_joined_channels):
        row.append(InlineKeyboardButton(f"➕ {c['channel_name']}", url=c["channel_link"]))
        if len(row) == 2 or i == len(not_joined_channels) - 1:
            buttons.append(row)
            row = []
    buttons.append([InlineKeyboardButton("✅ Maine Sab Join Kar Liya", callback_data="check_join")])
    return text, InlineKeyboardMarkup(buttons)

async def check_force_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user:
        return
    uid = update.effective_user.id

    u = qdb("SELECT is_blocked FROM users WHERE user_id=?", (uid,), one=True)
    if u and u["is_blocked"]:
        if update.message:
            await update.message.reply_text("❌ Aap is bot se block hain.")
        elif update.callback_query:
            await update.callback_query.answer("❌ Aap block hain.", show_alert=True)
        raise ApplicationHandlerStop()

    if is_admin(uid):
        return

    if get_setting("force_join") != "1":
        return

    channels = qdb("SELECT * FROM channels WHERE is_active=1")
    if not channels:
        return

    not_joined = await get_not_joined_channels(context, uid)
    if not not_joined:
        return  # All joined, proceed normally

    text, kb = force_join_message(not_joined)
    if update.message:
        await update.message.reply_text(text, reply_markup=kb, parse_mode="Markdown")
    elif update.callback_query and update.callback_query.data != "check_join":
        await update.callback_query.message.reply_text(text, reply_markup=kb, parse_mode="Markdown")
        await update.callback_query.answer()
    raise ApplicationHandlerStop()

# ==========================================
# /start
# ==========================================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user  = update.effective_user
    uid   = user.id
    uname = user.username or user.first_name

    existing = qdb("SELECT 1 FROM users WHERE user_id=?", (uid,), one=True)
    if not existing:
        qdb("INSERT INTO users (user_id, username) VALUES (?,?)", (uid, uname), commit=True)
        args = context.args
        if args and args[0].isdigit():
            ref_id = int(args[0])
            if ref_id != uid:
                reward = get_setting("referral_reward", float)
                try:
                    qdb("INSERT INTO referrals (referrer_id,referred_id) VALUES (?,?)", (ref_id, uid), commit=True)
                    qdb("UPDATE users SET balance=balance+?,total_referrals=total_referrals+1,"
                        "successful_referrals=successful_referrals+1 WHERE user_id=?", (reward, ref_id), commit=True)
                    await context.bot.send_message(ref_id,
                        f"🎉 *Naya Referral!*\nTumhe *{reward} coins* mile!", parse_mode="Markdown")
                except Exception:
                    pass

    if is_admin(uid):
        ADMIN_USER_MODE.discard(uid)
        await update.message.reply_text(
            "👑 *Admin Panel*\nButtons se sab manage karo.",
            reply_markup=admin_kb(), parse_mode="Markdown")
        return

    welcome = get_setting("welcome_message")
    await update.message.reply_text(welcome, reply_markup=main_kb())

# ==========================================
# CALLBACKS
# ==========================================
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data  = query.data
    uid   = query.from_user.id

    # ── Force join verify — ACTUALLY CHECK ──
    if data == "check_join":
        not_joined = await get_not_joined_channels(context, uid)
        if not_joined:
            # Still not joined all channels
            text, kb = force_join_message(not_joined)
            await query.answer("❌ Abhi bhi kuch channels join nahi kiye!", show_alert=True)
            try:
                await query.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")
            except Exception:
                pass
        else:
            # All joined!
            await query.answer("✅ Shukriya! Sab join ho gaye.", show_alert=True)
            try:
                await query.message.delete()
            except Exception:
                pass
            await context.bot.send_message(uid,
                get_setting("welcome_message"), reply_markup=main_kb())
        return

    # ── Task info ──
    if data.startswith("task_info_"):
        task_id = int(data.split("_")[2])
        task = qdb("SELECT * FROM tasks WHERE id=?", (task_id,), one=True)
        if not task:
            await query.answer("Task nahi mila.", show_alert=True); return
        done = qdb("SELECT 1 FROM user_tasks WHERE user_id=? AND task_id=?", (uid, task_id), one=True)
        if done:
            await query.answer("Yeh task pehle hi complete ho gaya!", show_alert=True); return
        await query.answer()
        await query.message.edit_text(
            f"📋 *{task['title']}*\n\n"
            f"🪙 Reward: *{task['reward']} coins*\n"
            f"📝 {task['description'] or ''}\n\n"
            f"Link kholo phir Verify dabao.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔗 Task Open Karo", url=task["link"])],
                [InlineKeyboardButton("✅ Verify & Claim",  callback_data=f"task_done_{task_id}")]
            ])
        )
        return

    if data.startswith("task_done_"):
        task_id = int(data.split("_")[2])
        task = qdb("SELECT * FROM tasks WHERE id=?", (task_id,), one=True)
        if not task:
            await query.answer("Task nahi mila.", show_alert=True); return
        done = qdb("SELECT 1 FROM user_tasks WHERE user_id=? AND task_id=?", (uid, task_id), one=True)
        if done:
            await query.answer("Pehle hi complete!", show_alert=True); return
        qdb("INSERT INTO user_tasks (user_id,task_id) VALUES (?,?)", (uid, task_id), commit=True)
        qdb("UPDATE users SET balance=balance+? WHERE user_id=?", (task["reward"], uid), commit=True)
        await query.answer(f"✅ +{task['reward']} coins!", show_alert=True)
        await query.message.edit_text(
            f"✅ *Task Complete!*\nTumhe *{task['reward']} coins* mile. 🎉", parse_mode="Markdown")
        return

    # ── Withdrawal approve/reject ──
    if data.startswith("wd_"):
        if not is_admin(uid):
            await query.answer("Admin nahi hain.", show_alert=True); return
        parts  = data.split("_")
        action = parts[1]
        req_id = int(parts[2])
        w = qdb("SELECT * FROM withdraw_requests WHERE id=?", (req_id,), one=True)
        if not w or w["status"] != "Pending":
            await query.answer("Pehle hi process ho chuka hai.", show_alert=True); return
        if action == "approve":
            qdb("UPDATE withdraw_requests SET status='Approved' WHERE id=?", (req_id,), commit=True)
            await context.bot.send_message(w["user_id"],
                f"✅ <b>Withdrawal Approved!</b>\n<b>{w['amount']} coins</b> — <b>{w['method']}</b> se process ho gaya.",
                parse_mode="HTML")
            await query.edit_message_text(query.message.text + "\n\n✅ APPROVED")
        else:
            qdb("UPDATE withdraw_requests SET status='Rejected' WHERE id=?", (req_id,), commit=True)
            qdb("UPDATE users SET balance=balance+? WHERE user_id=?", (w["amount"], w["user_id"]), commit=True)
            await context.bot.send_message(w["user_id"],
                f"❌ <b>Withdrawal Rejected!</b>\n<b>{w['amount']} coins</b> wapas aaye.",
                parse_mode="HTML")
            await query.edit_message_text(query.message.text + "\n\n❌ REJECTED — coins refunded")
        await query.answer("Done!")
        return

    # ── Block/Unblock ──
    if data.startswith("block_") or data.startswith("unblock_"):
        if not is_admin(uid):
            await query.answer("Admin nahi hain.", show_alert=True); return
        action  = "block" if data.startswith("block_") else "unblock"
        tgt     = int(data.split("_")[1])
        new_val = 1 if action == "block" else 0
        qdb("UPDATE users SET is_blocked=? WHERE user_id=?", (new_val, tgt), commit=True)
        new_lbl = "🔓 Unblock" if new_val else "🚫 Block"
        new_cb  = f"unblock_{tgt}" if new_val else f"block_{tgt}"
        await query.edit_message_reply_markup(
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(new_lbl,               callback_data=new_cb),
                InlineKeyboardButton("💰 Balance Edit",     callback_data=f"editbal_{tgt}"),
                InlineKeyboardButton("👁 Info",             callback_data=f"uinfo_{tgt}"),
            ]])
        )
        await query.answer("🔴 Blocked" if new_val else "🟢 Unblocked", show_alert=True)
        return

    if data.startswith("editbal_"):
        if not is_admin(uid):
            await query.answer("Admin nahi hain.", show_alert=True); return
        tgt = int(data.split("_")[1])
        ADMIN_STATES[uid] = {"step": "edit_balance", "target": tgt}
        await query.answer()
        await context.bot.send_message(uid,
            f"💰 User `{tgt}` ka naya balance bhejo:", parse_mode="Markdown", reply_markup=cancel_kb())
        return

    if data.startswith("uinfo_"):
        if not is_admin(uid):
            await query.answer("Admin nahi hain.", show_alert=True); return
        await query.answer()
        await send_user_info(context, uid, int(data.split("_")[1]))
        return

    # ── Task toggle/delete ──
    if data.startswith("tog_task_"):
        if not is_admin(uid):
            await query.answer("Admin nahi hain.", show_alert=True); return
        tid = int(data.split("_")[2])
        qdb("UPDATE tasks SET is_active=CASE WHEN is_active=1 THEN 0 ELSE 1 END WHERE id=?", (tid,), commit=True)
        t  = qdb("SELECT * FROM tasks WHERE id=?", (tid,), one=True)
        st = "✅ Active" if t["is_active"] else "❌ Disabled"
        await query.edit_message_text(
            f"📋 *{t['title']}*\n🪙 {t['reward']} coins | {st}\n🔗 {t['link']}",
            parse_mode="Markdown", reply_markup=task_kb(tid))
        await query.answer(st, show_alert=True)
        return

    if data.startswith("del_task_"):
        if not is_admin(uid):
            await query.answer("Admin nahi hain.", show_alert=True); return
        qdb("DELETE FROM tasks WHERE id=?", (int(data.split("_")[2]),), commit=True)
        await query.answer("🗑 Deleted!", show_alert=True)
        await query.edit_message_text("🗑 Task delete ho gaya.")
        return

    # ── Channel toggle/delete ──
    if data.startswith("tog_ch_"):
        if not is_admin(uid):
            await query.answer("Admin nahi hain.", show_alert=True); return
        cid = int(data.split("_")[2])
        qdb("UPDATE channels SET is_active=CASE WHEN is_active=1 THEN 0 ELSE 1 END WHERE id=?", (cid,), commit=True)
        c  = qdb("SELECT * FROM channels WHERE id=?", (cid,), one=True)
        st = "✅ Active" if c["is_active"] else "❌ Disabled"
        await query.edit_message_text(
            f"📢 *{c['channel_name']}*\nID: `{c['channel_id']}` | {st}",
            parse_mode="Markdown", reply_markup=channel_kb(cid))
        await query.answer(st, show_alert=True)
        return

    if data.startswith("del_ch_"):
        if not is_admin(uid):
            await query.answer("Admin nahi hain.", show_alert=True); return
        qdb("DELETE FROM channels WHERE id=?", (int(data.split("_")[2]),), commit=True)
        await query.answer("🗑 Deleted!", show_alert=True)
        await query.edit_message_text("🗑 Channel delete ho gaya.")
        return

    # ── Add flows ──
    if data == "admin_add_task":
        if not is_admin(uid): await query.answer("Admin nahi hain.", show_alert=True); return
        ADMIN_STATES[uid] = {"step": "task_title"}
        await query.answer()
        await context.bot.send_message(uid,
            "📋 *Naya Task — Step 1/4*\n\nTask ka *Title* bhejo:",
            parse_mode="Markdown", reply_markup=cancel_kb())
        return

    if data == "admin_add_channel":
        if not is_admin(uid): await query.answer("Admin nahi hain.", show_alert=True); return
        ADMIN_STATES[uid] = {"step": "ch_name"}
        await query.answer()
        await context.bot.send_message(uid,
            "📢 *Naya Channel — Step 1/3*\n\nChannel ka *Naam* bhejo:",
            parse_mode="Markdown", reply_markup=cancel_kb())
        return

    if data.startswith("setedit_"):
        if not is_admin(uid): await query.answer("Admin nahi hain.", show_alert=True); return
        key = data[len("setedit_"):]
        labels = {
            "referral_reward": "Referral Reward (number)",
            "daily_bonus":     "Daily Bonus (number)",
            "min_withdraw":    "Minimum Withdraw (number)",
            "welcome_message": "Welcome Message (koi bhi text)",
            "force_join":      "Force Join: 1=ON, 0=OFF",
        }
        ADMIN_STATES[uid] = {"step": "set_value", "key": key}
        await query.answer()
        await context.bot.send_message(uid,
            f"⚙️ *{labels.get(key, key)}*\n\nNaya value bhejo:",
            parse_mode="Markdown", reply_markup=cancel_kb())
        return

    if data.startswith("users_page_"):
        if not is_admin(uid): await query.answer("Admin nahi hain.", show_alert=True); return
        await query.answer()
        await send_users_page(query, context, int(data.split("_")[2]), editing=True)
        return

    if data == "user_search":
        if not is_admin(uid): await query.answer("Admin nahi hain.", show_alert=True); return
        ADMIN_STATES[uid] = {"step": "search_user"}
        await query.answer()
        await context.bot.send_message(uid, "🔍 User ID bhejo:", reply_markup=cancel_kb())
        return

    await query.answer()

# ==========================================
# ACTION KEYBOARDS
# ==========================================
def task_kb(tid):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔁 Toggle", callback_data=f"tog_task_{tid}"),
        InlineKeyboardButton("🗑 Delete",  callback_data=f"del_task_{tid}"),
    ]])

def channel_kb(cid):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔁 Toggle", callback_data=f"tog_ch_{cid}"),
        InlineKeyboardButton("🗑 Delete",  callback_data=f"del_ch_{cid}"),
    ]])

# ==========================================
# ADMIN BOT SCREENS
# ==========================================
async def admin_stats(update):
    total_users  = qdb("SELECT COUNT(*) as c FROM users", one=True)["c"]
    total_refs   = qdb("SELECT COUNT(*) as c FROM referrals", one=True)["c"]
    paid_out     = qdb("SELECT COALESCE(SUM(amount),0) as c FROM withdraw_requests WHERE status='Approved'", one=True)["c"]
    pending_wd   = qdb("SELECT COUNT(*) as c FROM withdraw_requests WHERE status='Pending'", one=True)["c"]
    active_tasks = qdb("SELECT COUNT(*) as c FROM tasks WHERE is_active=1", one=True)["c"]
    active_ch    = qdb("SELECT COUNT(*) as c FROM channels WHERE is_active=1", one=True)["c"]
    blocked      = qdb("SELECT COUNT(*) as c FROM users WHERE is_blocked=1", one=True)["c"]
    today        = qdb("SELECT COUNT(*) as c FROM users WHERE date(join_date)=date('now')", one=True)["c"]
    await update.message.reply_text(
        f"📊 *Bot Statistics*\n\n"
        f"👥 Total Users: *{total_users}*\n"
        f"🆕 Aaj Naye: *{today}*\n"
        f"🔗 Total Referrals: *{total_refs}*\n"
        f"✅ Paid Out: *{paid_out} coins*\n"
        f"⏳ Pending Withdrawals: *{pending_wd}*\n"
        f"📋 Active Tasks: *{active_tasks}*\n"
        f"📢 Active Channels: *{active_ch}*\n"
        f"🚫 Blocked Users: *{blocked}*",
        parse_mode="Markdown"
    )

async def send_users_page(obj, context, page=0, editing=False):
    PER_PAGE = 8
    users    = qdb("SELECT * FROM users ORDER BY id DESC LIMIT ? OFFSET ?", (PER_PAGE, page*PER_PAGE))
    total    = qdb("SELECT COUNT(*) as c FROM users", one=True)["c"]
    total_p  = max(1, -(-total // PER_PAGE))

    text = f"👥 *Users — Page {page+1}/{total_p}* (Total: {total})\n\n"
    if not users:
        text += "Koi user nahi."
        kb = None
    else:
        for u in users:
            uname  = f"@{u['username']}" if u["username"] else "—"
            status = "🔴" if u["is_blocked"] else "🟢"
            text  += f"{status} `{u['user_id']}` {uname} | 💰{u['balance']}\n"

        rows = []
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"users_page_{page-1}"))
        nav.append(InlineKeyboardButton("🔍 Search", callback_data="user_search"))
        if (page+1) < total_p:
            nav.append(InlineKeyboardButton("Next ▶️", callback_data=f"users_page_{page+1}"))
        rows.append(nav)

        for u in users:
            blbl = "🔓" if u["is_blocked"] else "🚫"
            bcb  = f"unblock_{u['user_id']}" if u["is_blocked"] else f"block_{u['user_id']}"
            rows.append([
                InlineKeyboardButton(f"{blbl} {u['user_id']}", callback_data=bcb),
                InlineKeyboardButton("💰", callback_data=f"editbal_{u['user_id']}"),
                InlineKeyboardButton("👁", callback_data=f"uinfo_{u['user_id']}"),
            ])
        kb = InlineKeyboardMarkup(rows)

    if editing:
        await obj.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)
    else:
        await obj.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)

async def send_user_info(context, admin_uid, tgt):
    u = qdb("SELECT * FROM users WHERE user_id=?", (tgt,), one=True)
    if not u:
        await context.bot.send_message(admin_uid, "❌ User nahi mila."); return
    td    = qdb("SELECT COUNT(*) as c FROM user_tasks WHERE user_id=?", (tgt,), one=True)["c"]
    wdc   = qdb("SELECT COUNT(*) as c FROM withdraw_requests WHERE user_id=?", (tgt,), one=True)["c"]
    st    = "🔴 Blocked" if u["is_blocked"] else "🟢 Active"
    blbl  = "🔓 Unblock" if u["is_blocked"] else "🚫 Block"
    bcb   = f"unblock_{tgt}" if u["is_blocked"] else f"block_{tgt}"
    await context.bot.send_message(admin_uid,
        f"👤 *User Info*\n\n🆔 `{u['user_id']}`\n📛 @{u['username'] or 'N/A'}\n"
        f"📅 {u['join_date'][:10]}\n💰 *{u['balance']} coins*\n"
        f"👥 Refs: {u['total_referrals']} | 📋 Tasks: {td} | 💳 WD: {wdc}\nStatus: {st}",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(blbl,              callback_data=bcb),
            InlineKeyboardButton("💰 Balance Edit", callback_data=f"editbal_{tgt}"),
        ]])
    )

async def admin_tasks_screen(update):
    tasks = qdb("SELECT * FROM tasks ORDER BY id DESC")
    await update.message.reply_text(
        "📋 *Task Management*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("➕ Naya Task Add", callback_data="admin_add_task")
        ]])
    )
    if not tasks:
        await update.message.reply_text("Koi task nahi hai."); return
    for t in tasks:
        st = "✅" if t["is_active"] else "❌"
        await update.message.reply_text(
            f"📋 *{t['title']}*\n🪙 {t['reward']} coins | {st}\n🔗 {t['link']}",
            parse_mode="Markdown", reply_markup=task_kb(t["id"]))

async def admin_channels_screen(update):
    channels = qdb("SELECT * FROM channels ORDER BY id DESC")
    await update.message.reply_text(
        "📢 *Channel Management*\n_Bot ko channel admin banana zaroori hai._",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("➕ Naya Channel Add", callback_data="admin_add_channel")
        ]])
    )
    if not channels:
        await update.message.reply_text("Koi channel nahi hai."); return
    for c in channels:
        st = "✅" if c["is_active"] else "❌"
        await update.message.reply_text(
            f"📢 *{c['channel_name']}*\nID: `{c['channel_id']}` | {st}\n🔗 {c['channel_link']}",
            parse_mode="Markdown", reply_markup=channel_kb(c["id"]))

async def admin_withdrawals_screen(update):
    pending = qdb("SELECT * FROM withdraw_requests WHERE status='Pending' ORDER BY id DESC")
    if not pending:
        await update.message.reply_text("✅ Koi pending withdrawal nahi."); return
    await update.message.reply_text(f"💳 *Pending: {len(pending)}*", parse_mode="Markdown")
    for w in pending:
        u     = qdb("SELECT username FROM users WHERE user_id=?", (w["user_id"],), one=True)
        uname = f"@{u['username']}" if u and u["username"] else f"ID:{w['user_id']}"
        await update.message.reply_text(
            f"💳 <b>#{w['id']}</b> — {uname} (<code>{w['user_id']}</code>)\n"
            f"💰 <b>{w['amount']} coins</b> — <b>{w['method']}</b>\n"
            f"<pre>{w['details']}</pre>\n🕐 {w['created_at']}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Approve", callback_data=f"wd_approve_{w['id']}"),
                InlineKeyboardButton("❌ Reject",  callback_data=f"wd_reject_{w['id']}"),
            ]])
        )

async def admin_settings_screen(update):
    await update.message.reply_text(
        f"⚙️ *Settings*\n\n"
        f"🪙 Referral Reward: `{get_setting('referral_reward')} coins`\n"
        f"🎁 Daily Bonus: `{get_setting('daily_bonus')} coins`\n"
        f"💳 Min Withdraw: `{get_setting('min_withdraw')} coins`\n"
        f"🛑 Force Join: `{'✅ ON' if get_setting('force_join')=='1' else '❌ OFF'}`\n"
        f"👋 Welcome:\n_{get_setting('welcome_message')}_\n\n"
        f"Change karne ke liye button dabao:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🪙 Referral Reward",  callback_data="setedit_referral_reward")],
            [InlineKeyboardButton("🎁 Daily Bonus",      callback_data="setedit_daily_bonus")],
            [InlineKeyboardButton("💳 Min Withdraw",     callback_data="setedit_min_withdraw")],
            [InlineKeyboardButton("🛑 Force Join",       callback_data="setedit_force_join")],
            [InlineKeyboardButton("👋 Welcome Message",  callback_data="setedit_welcome_message")],
        ])
    )

async def do_broadcast(update, context, msg):
    users = qdb("SELECT user_id FROM users WHERE is_blocked=0")
    sent = failed = 0
    sm = await update.message.reply_text(f"📣 {len(users)} users ko bhej raha hoon…")
    for u in users:
        try:
            await context.bot.send_message(u["user_id"], msg, parse_mode="HTML")
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)
    await sm.edit_text(f"📣 *Done!*\n✅ Sent: {sent} | ❌ Failed: {failed}", parse_mode="Markdown")

# ==========================================
# ADMIN STATE HANDLER
# ==========================================
async def handle_admin_state(update, context, uid, text):
    if uid not in ADMIN_STATES: return False
    state = ADMIN_STATES[uid]
    step  = state["step"]

    if step == "broadcast":
        ADMIN_STATES.pop(uid, None)
        await do_broadcast(update, context, text); return True

    if step == "edit_balance":
        ADMIN_STATES.pop(uid, None)
        try:
            val = float(text)
            if val < 0: raise ValueError
            qdb("UPDATE users SET balance=? WHERE user_id=?", (val, state["target"]), commit=True)
            await update.message.reply_text(
                f"✅ `{state['target']}` ka balance *{val} coins* set.", parse_mode="Markdown", reply_markup=admin_kb())
        except ValueError:
            await update.message.reply_text("❌ Valid number bhejo.", reply_markup=admin_kb())
        return True

    if step == "search_user":
        ADMIN_STATES.pop(uid, None)
        try:
            await send_user_info(context, uid, int(text.strip()))
        except ValueError:
            await update.message.reply_text("❌ Valid User ID bhejo.", reply_markup=admin_kb())
        return True

    if step == "set_value":
        key = state["key"]
        val = text.strip()
        ADMIN_STATES.pop(uid, None)
        if key in ("referral_reward","daily_bonus","min_withdraw"):
            try:
                if float(val) < 0: raise ValueError
            except ValueError:
                await update.message.reply_text("❌ Valid positive number bhejo.", reply_markup=admin_kb()); return True
        if key == "force_join" and val not in ("0","1"):
            await update.message.reply_text("❌ Sirf 0 ya 1.", reply_markup=admin_kb()); return True
        set_setting(key, val)
        await update.message.reply_text(
            f"✅ *{key}* = `{val}`", parse_mode="Markdown", reply_markup=admin_kb())
        return True

    # Add task flow
    if step == "task_title":
        state.update({"step":"task_reward","title":text})
        await update.message.reply_text("📋 *Step 2/4* — *Reward* (coins):", parse_mode="Markdown"); return True
    if step == "task_reward":
        try:
            r = float(text)
            if r < 0: raise ValueError
            state.update({"step":"task_link","reward":r})
            await update.message.reply_text("📋 *Step 3/4* — *Link* bhejo:", parse_mode="Markdown")
        except ValueError:
            await update.message.reply_text("❌ Valid number bhejo.")
        return True
    if step == "task_link":
        state.update({"step":"task_desc","link":text})
        await update.message.reply_text("📋 *Step 4/4* — *Description* bhejo:", parse_mode="Markdown"); return True
    if step == "task_desc":
        qdb("INSERT INTO tasks (title,reward,link,description) VALUES (?,?,?,?)",
            (state["title"],state["reward"],state["link"],text), commit=True)
        ADMIN_STATES.pop(uid, None)
        await update.message.reply_text(
            f"✅ *Task Added!*\n📋 {state['title']}\n🪙 {state['reward']} coins",
            parse_mode="Markdown", reply_markup=admin_kb()); return True

    # Add channel flow
    if step == "ch_name":
        state.update({"step":"ch_link","name":text})
        await update.message.reply_text("📢 *Step 2/3* — Channel *Link* bhejo:\n_(e.g. https://t.me/xxx)_",
            parse_mode="Markdown"); return True
    if step == "ch_link":
        state.update({"step":"ch_id","link":text})
        await update.message.reply_text(
            "📢 *Step 3/3* — Channel *ID* bhejo:\n• Public: `@channelname`\n• Private: `-100xxxxxxxxxx`",
            parse_mode="Markdown"); return True
    if step == "ch_id":
        qdb("INSERT INTO channels (channel_name,channel_link,channel_id) VALUES (?,?,?)",
            (state["name"],state["link"],text.strip()), commit=True)
        ADMIN_STATES.pop(uid, None)
        await update.message.reply_text(
            f"✅ *Channel Added!*\n📢 {state['name']}\n🆔 `{text.strip()}`",
            parse_mode="Markdown", reply_markup=admin_kb()); return True

    return False

# ==========================================
# WITHDRAWAL FLOW
# ==========================================
async def process_withdrawal(update, context, uid, amount, method, details):
    qdb("UPDATE users SET balance=balance-? WHERE user_id=?", (amount, uid), commit=True)
    req_id = qdb("INSERT INTO withdraw_requests (user_id,amount,method,details) VALUES (?,?,?,?)",
                 (uid, amount, method, details), commit=True)
    USER_STATES.pop(uid, None)
    await update.message.reply_text(
        "✅ *Request Submit Ho Gaya!*\nAdmin jald review karega.",
        parse_mode="Markdown", reply_markup=main_kb())
    u     = qdb("SELECT username FROM users WHERE user_id=?", (uid,), one=True)
    uname = f"@{u['username']}" if u and u["username"] else f"ID:{uid}"
    try:
        await context.bot.send_message(ADMIN_TELEGRAM_ID,
            f"💳 <b>Naya Withdrawal #{req_id}</b>\n👤 {uname} (<code>{uid}</code>)\n"
            f"💰 <b>{amount} coins</b> — <b>{method}</b>\n<pre>{details}</pre>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Approve", callback_data=f"wd_approve_{req_id}"),
                InlineKeyboardButton("❌ Reject",  callback_data=f"wd_reject_{req_id}"),
            ]])
        )
    except Exception as e:
        logger.error(f"Admin notify fail: {e}")

async def handle_withdrawal_state(update, context, uid, text):
    if uid not in USER_STATES: return False
    state = USER_STATES[uid]
    step  = state["step"]

    if step == "amount":
        try:
            amount  = float(text)
            min_w   = get_setting("min_withdraw", float)
            balance = qdb("SELECT balance FROM users WHERE user_id=?", (uid,), one=True)["balance"]
            if amount <= 0:
                await update.message.reply_text("❌ Amount 0 se zyada hona chahiye.")
                USER_STATES.pop(uid, None); return True
            if amount < min_w:
                await update.message.reply_text(f"❌ Minimum {min_w} coins chahiye.")
                USER_STATES.pop(uid, None); return True
            if amount > balance:
                await update.message.reply_text(f"❌ Balance kam hai ({balance} coins).")
                USER_STATES.pop(uid, None); return True
            state.update({"step":"method","amount":amount})
            await update.message.reply_text(
                f"✅ Amount: *{amount} coins*\n\n🏦 *Method chuno:*", parse_mode="Markdown",
                reply_markup=ReplyKeyboardMarkup([
                    ["🏦 Bank Transfer","📱 UPI"],
                    ["💳 Crypto","❌ Cancel"]
                ], resize_keyboard=True))
        except ValueError:
            await update.message.reply_text("❌ Valid number bhejo.")
            USER_STATES.pop(uid, None)
        return True

    if step == "method":
        if text == "🏦 Bank Transfer":
            state.update({"method":"Bank Transfer","step":"bank_name"})
            await update.message.reply_text("🏦 *Bank Name* bhejo:", parse_mode="Markdown", reply_markup=cancel_kb())
        elif text == "📱 UPI":
            state.update({"method":"UPI","step":"upi_id"})
            await update.message.reply_text("📱 *UPI ID* bhejo:", parse_mode="Markdown", reply_markup=cancel_kb())
        elif text == "💳 Crypto":
            state.update({"method":"Crypto","step":"crypto_addr"})
            await update.message.reply_text("💳 *Wallet Address* bhejo (USDT TRC20):", parse_mode="Markdown", reply_markup=cancel_kb())
        else:
            await update.message.reply_text("❌ Button se chuno.")
        return True

    if step == "bank_name":
        state.update({"bank_name":text,"step":"bank_ac"})
        await update.message.reply_text("🔢 *Account Number:*", parse_mode="Markdown"); return True
    if step == "bank_ac":
        state.update({"bank_ac":text,"step":"bank_ifsc"})
        await update.message.reply_text("🔠 *IFSC Code:*", parse_mode="Markdown"); return True
    if step == "bank_ifsc":
        state.update({"bank_ifsc":text,"step":"bank_holder"})
        await update.message.reply_text("👤 *Account Holder Name:*", parse_mode="Markdown"); return True
    if step == "bank_holder":
        await process_withdrawal(update, context, uid, state["amount"], "Bank Transfer",
            f"Bank: {state['bank_name']}\nA/C: {state['bank_ac']}\nIFSC: {state['bank_ifsc']}\nName: {text}")
        return True
    if step == "upi_id":
        await process_withdrawal(update, context, uid, state["amount"], "UPI", f"UPI ID: {text}"); return True
    if step == "crypto_addr":
        await process_withdrawal(update, context, uid, state["amount"], "Crypto", f"Wallet: {text}"); return True

    return False

# ==========================================
# USER FEATURES
# ==========================================
async def user_balance(update, uid):
    u = qdb("SELECT balance FROM users WHERE user_id=?", (uid,), one=True)
    await update.message.reply_text(f"💰 *Tumhara Balance*\n\n`{u['balance']} coins`", parse_mode="Markdown")

async def user_referrals(update, context, uid):
    u    = qdb("SELECT total_referrals FROM users WHERE user_id=?", (uid,), one=True)
    link = f"https://t.me/{context.bot.username}?start={uid}"
    await update.message.reply_text(
        f"👥 *Referral System*\n\n"
        f"🪙 Har invite pe: *{get_setting('referral_reward')} coins*\n"
        f"📊 Tumhare invites: *{u['total_referrals']}*\n\n"
        f"🔗 *Tumhara Link:*\n`{link}`",
        parse_mode="Markdown")

async def user_daily(update, uid):
    u   = qdb("SELECT last_bonus FROM users WHERE user_id=?", (uid,), one=True)
    now = datetime.now()
    if u["last_bonus"]:
        try:
            last = datetime.strptime(u["last_bonus"], "%Y-%m-%d %H:%M:%S")
            if now < last + timedelta(days=1):
                rem  = (last + timedelta(days=1)) - now
                h, r = divmod(int(rem.total_seconds()), 3600)
                m, _ = divmod(r, 60)
                await update.message.reply_text(
                    f"⏳ *Pehle hi claim ho gaya!*\n\nWapas aana: *{h}h {m}m* mein",
                    parse_mode="Markdown"); return
        except ValueError:
            pass
    bonus = get_setting("daily_bonus", float)
    qdb("UPDATE users SET balance=balance+?,last_bonus=? WHERE user_id=?",
        (bonus, now.strftime("%Y-%m-%d %H:%M:%S"), uid), commit=True)
    await update.message.reply_text(
        f"🎁 *Daily Bonus!*\n\n+*{bonus} coins* aaye! ✅", parse_mode="Markdown")

async def user_tasks(update, uid):
    tasks = qdb("SELECT * FROM tasks WHERE is_active=1")
    if not tasks:
        await update.message.reply_text("😔 Abhi koi task nahi hai."); return
    await update.message.reply_text(
        "📋 *Tasks* — Ek chuno:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(f"🪙{t['reward']} | {t['title']}", callback_data=f"task_info_{t['id']}")]
            for t in tasks
        ])
    )

async def user_withdraw(update, uid):
    u     = qdb("SELECT balance FROM users WHERE user_id=?", (uid,), one=True)
    min_w = get_setting("min_withdraw", float)
    if u["balance"] < min_w:
        await update.message.reply_text(
            f"❌ *Balance Kam Hai*\nTumhara: `{u['balance']} coins`\nMinimum: `{min_w} coins`",
            parse_mode="Markdown"); return
    USER_STATES[uid] = {"step":"amount"}
    await update.message.reply_text(
        f"💳 *Withdrawal*\nBalance: `{u['balance']} coins` | Min: `{min_w}`\n\nAmount bhejo:",
        parse_mode="Markdown", reply_markup=cancel_kb())

async def user_leaderboard(update):
    top = qdb("SELECT username,total_referrals,balance FROM users WHERE is_blocked=0 "
              "ORDER BY total_referrals DESC,balance DESC LIMIT 10")
    msg = "🏆 <b>Top 10</b>\n\n"
    for i, t in enumerate(top, 1):
        n = (t["username"] or "Anonymous").replace("<","&lt;").replace(">","&gt;")
        msg += f"<b>{i}.</b> {n} — {t['total_referrals']} refs | {t['balance']} coins\n"
    await update.message.reply_text(msg, parse_mode="HTML")

async def user_profile(update, uid):
    u  = qdb("SELECT * FROM users WHERE user_id=?", (uid,), one=True)
    td = qdb("SELECT COUNT(*) as c FROM user_tasks WHERE user_id=?", (uid,), one=True)["c"]
    await update.message.reply_text(
        f"👤 *Profile*\n\n🆔 `{u['user_id']}`\n📛 @{u['username'] or 'N/A'}\n"
        f"📅 {u['join_date'][:10]}\n\n💰 *{u['balance']} coins*\n"
        f"👥 Refs: {u['total_referrals']} | 📋 Tasks: {td}",
        parse_mode="Markdown")

async def user_help(update):
    await update.message.reply_text(
        "ℹ️ *Help*\n\n"
        "👤 Profile | 📋 Tasks | 🎁 Daily Bonus\n"
        "👥 Referrals | 💰 Balance | 💳 Withdraw\n"
        "🏆 Leaderboard", parse_mode="Markdown")

# ==========================================
# MAIN MESSAGE HANDLER
# ==========================================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return
    text = update.message.text
    uid  = update.effective_user.id

    if text == "❌ Cancel":
        USER_STATES.pop(uid, None)
        ADMIN_STATES.pop(uid, None)
        if is_admin(uid) and uid not in ADMIN_USER_MODE:
            await update.message.reply_text("❌ Cancel.", reply_markup=admin_kb())
        else:
            await update.message.reply_text("❌ Cancel.", reply_markup=user_mode_kb() if is_admin(uid) else main_kb())
        return

    if is_admin(uid) and uid in ADMIN_STATES:
        if await handle_admin_state(update, context, uid, text): return

    if uid in USER_STATES:
        if await handle_withdrawal_state(update, context, uid, text): return

    if is_admin(uid) and text == "👑 Admin Panel":
        ADMIN_USER_MODE.discard(uid)
        await update.message.reply_text("👑 *Admin Panel*", parse_mode="Markdown", reply_markup=admin_kb()); return

    if is_admin(uid) and uid not in ADMIN_USER_MODE:
        if   text == "📊 Stats":       await admin_stats(update)
        elif text == "👥 Users":       await send_users_page(update, context, 0, editing=False)
        elif text == "📋 Tasks":       await admin_tasks_screen(update)
        elif text == "📢 Channels":    await admin_channels_screen(update)
        elif text == "💳 Withdrawals": await admin_withdrawals_screen(update)
        elif text == "📣 Broadcast":
            ADMIN_STATES[uid] = {"step":"broadcast"}
            await update.message.reply_text(
                "📣 *Broadcast*\n\nSab users ko jaane wala message bhejo:\n_(HTML allowed)_",
                parse_mode="Markdown", reply_markup=cancel_kb())
        elif text == "⚙️ Settings":   await admin_settings_screen(update)
        elif text == "🔙 User View":
            ADMIN_USER_MODE.add(uid)
            await update.message.reply_text(
                "🔙 User view mein ho.\n👑 wapas jaane ke liye last button dabao.",
                reply_markup=user_mode_kb())
        else:
            await update.message.reply_text("⬆️ Button se karo.", reply_markup=admin_kb())
        return

    if   text == "👤 Profile":     await user_profile(update, uid)
    elif text == "📋 Tasks":       await user_tasks(update, uid)
    elif text == "🎁 Daily Bonus": await user_daily(update, uid)
    elif text == "👥 Referrals":   await user_referrals(update, context, uid)
    elif text == "💰 Balance":     await user_balance(update, uid)
    elif text == "💳 Withdraw":    await user_withdraw(update, uid)
    elif text == "🏆 Leaderboard": await user_leaderboard(update)
    elif text == "ℹ️ Help":        await user_help(update)

# ==========================================
# FLASK WEB ADMIN PANEL
# ==========================================
HTML = {
"base.html": """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Bot Admin Panel</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',sans-serif;background:#0f0f1a;color:#e0e0e0;display:flex;min-height:100vh}
.sidebar{width:220px;background:#1a1a2e;padding:20px 0;flex-shrink:0}
.sidebar h2{color:#7c6aff;text-align:center;padding:10px 20px 20px;font-size:18px}
.sidebar a{display:block;padding:12px 24px;color:#ccc;text-decoration:none;transition:.2s}
.sidebar a:hover,.sidebar a.active{background:#7c6aff22;color:#7c6aff;border-left:3px solid #7c6aff}
.main{flex:1;padding:30px;overflow:auto}
.card{background:#1a1a2e;border-radius:12px;padding:24px;margin-bottom:20px}
.card h3{color:#7c6aff;margin-bottom:16px}
.stat-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:12px;margin-bottom:20px}
.stat{background:#12122a;border-radius:10px;padding:16px;text-align:center}
.stat .val{font-size:28px;font-weight:700;color:#7c6aff}
.stat .lbl{font-size:12px;color:#888;margin-top:4px}
table{width:100%;border-collapse:collapse}
th,td{padding:10px 12px;text-align:left;border-bottom:1px solid #2a2a4a;font-size:13px}
th{color:#7c6aff;font-weight:600}
tr:hover td{background:#12122a}
input,textarea,select{background:#12122a;border:1px solid #2a2a4a;color:#e0e0e0;padding:8px 12px;border-radius:6px;width:100%;margin-bottom:8px;font-size:14px}
.btn{padding:8px 18px;border-radius:6px;border:none;cursor:pointer;font-size:13px;font-weight:600}
.btn-primary{background:#7c6aff;color:#fff}
.btn-success{background:#22c55e;color:#fff}
.btn-danger{background:#ef4444;color:#fff}
.btn-sm{padding:4px 10px;font-size:12px}
.badge{display:inline-block;padding:2px 8px;border-radius:20px;font-size:11px;font-weight:600}
.badge-green{background:#22c55e22;color:#22c55e}
.badge-red{background:#ef444422;color:#ef4444}
.badge-yellow{background:#f59e0b22;color:#f59e0b}
.flash{padding:10px 16px;border-radius:8px;margin-bottom:16px;font-size:14px;background:#22c55e22;color:#22c55e;border:1px solid #22c55e44}
.flash.err{background:#ef444422;color:#ef4444;border-color:#ef444444}
form.inline{display:inline}
</style>
</head>
<body>
<div class="sidebar">
  <h2>🤖 Admin</h2>
  <a href="/admin" class="{{ 'active' if active=='dashboard' }}">📊 Dashboard</a>
  <a href="/admin/users" class="{{ 'active' if active=='users' }}">👥 Users</a>
  <a href="/admin/tasks" class="{{ 'active' if active=='tasks' }}">📋 Tasks</a>
  <a href="/admin/channels" class="{{ 'active' if active=='channels' }}">📢 Channels</a>
  <a href="/admin/withdrawals" class="{{ 'active' if active=='withdrawals' }}">💳 Withdrawals</a>
  <a href="/admin/broadcast" class="{{ 'active' if active=='broadcast' }}">📣 Broadcast</a>
  <a href="/admin/settings" class="{{ 'active' if active=='settings' }}">⚙️ Settings</a>
  <a href="/admin/logout" style="color:#ef4444;margin-top:20px">🚪 Logout</a>
</div>
<div class="main">
{% for f in get_flashed() %}<div class="flash {{ 'err' if f[0]=='error' }}">{{ f[1] }}</div>{% endfor %}
{% block content %}{% endblock %}
</div>
</body></html>""",

"login.html": """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Login</title>
<style>
body{background:#0f0f1a;display:flex;align-items:center;justify-content:center;min-height:100vh;font-family:'Segoe UI',sans-serif}
.box{background:#1a1a2e;padding:40px;border-radius:16px;width:340px}
h2{color:#7c6aff;text-align:center;margin-bottom:24px}
input{background:#12122a;border:1px solid #2a2a4a;color:#e0e0e0;padding:10px 14px;border-radius:8px;width:100%;margin-bottom:12px;font-size:14px}
button{width:100%;padding:12px;background:#7c6aff;color:#fff;border:none;border-radius:8px;font-size:15px;font-weight:700;cursor:pointer}
.err{color:#ef4444;font-size:13px;margin-bottom:12px;text-align:center}
</style></head>
<body>
<div class="box">
  <h2>🤖 Admin Login</h2>
  {% if error %}<div class="err">{{ error }}</div>{% endif %}
  <form method="POST">
    <input name="username" placeholder="Username" required>
    <input name="password" type="password" placeholder="Password" required>
    <button type="submit">Login</button>
  </form>
</div>
</body></html>""",

"dashboard.html": """{% extends 'base.html' %}{% block content %}
<h2 style="margin-bottom:20px;color:#fff">📊 Dashboard</h2>
<div class="stat-grid">
  <div class="stat"><div class="val">{{ stats.users }}</div><div class="lbl">Total Users</div></div>
  <div class="stat"><div class="val">{{ stats.today }}</div><div class="lbl">Today Users</div></div>
  <div class="stat"><div class="val">{{ stats.refs }}</div><div class="lbl">Referrals</div></div>
  <div class="stat"><div class="val">{{ stats.paid }}</div><div class="lbl">Coins Paid</div></div>
  <div class="stat"><div class="val">{{ stats.pending }}</div><div class="lbl">Pending WD</div></div>
  <div class="stat"><div class="val">{{ stats.tasks }}</div><div class="lbl">Active Tasks</div></div>
  <div class="stat"><div class="val">{{ stats.channels }}</div><div class="lbl">Channels</div></div>
  <div class="stat"><div class="val">{{ stats.blocked }}</div><div class="lbl">Blocked</div></div>
</div>
{% endblock %}""",

"users.html": """{% extends 'base.html' %}{% block content %}
<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:20px">
  <h2 style="color:#fff">👥 Users</h2>
  <form method="GET" style="display:flex;gap:8px">
    <input name="q" value="{{ q }}" placeholder="Search ID/username" style="width:200px;margin:0">
    <button class="btn btn-primary" type="submit">Search</button>
  </form>
</div>
<div class="card">
<table>
  <tr><th>ID</th><th>Username</th><th>Balance</th><th>Refs</th><th>Joined</th><th>Status</th><th>Actions</th></tr>
  {% for u in users %}
  <tr>
    <td><code>{{ u.user_id }}</code></td>
    <td>{{ '@'+u.username if u.username else '—' }}</td>
    <td>{{ u.balance }} coins</td>
    <td>{{ u.total_referrals }}</td>
    <td>{{ u.join_date[:10] }}</td>
    <td>{% if u.is_blocked %}<span class="badge badge-red">Blocked</span>{% else %}<span class="badge badge-green">Active</span>{% endif %}</td>
    <td>
      <form class="inline" method="POST" action="/admin/users/block">
        <input type="hidden" name="user_id" value="{{ u.user_id }}">
        <input type="hidden" name="block" value="{{ '0' if u.is_blocked else '1' }}">
        <button class="btn btn-sm {{ 'btn-success' if u.is_blocked else 'btn-danger' }}">{{ 'Unblock' if u.is_blocked else 'Block' }}</button>
      </form>
      <form class="inline" method="POST" action="/admin/users/balance" style="display:inline-flex;gap:4px">
        <input type="hidden" name="user_id" value="{{ u.user_id }}">
        <input name="balance" value="{{ u.balance }}" style="width:80px;margin:0;padding:4px 6px">
        <button class="btn btn-sm btn-primary">Set</button>
      </form>
    </td>
  </tr>
  {% endfor %}
</table>
<div style="margin-top:16px;display:flex;gap:8px">
  {% if page > 0 %}<a class="btn btn-primary" href="?page={{ page-1 }}&q={{ q }}">◀ Prev</a>{% endif %}
  <span style="padding:8px;color:#888">Page {{ page+1 }}</span>
  {% if has_next %}<a class="btn btn-primary" href="?page={{ page+1 }}&q={{ q }}">Next ▶</a>{% endif %}
</div>
</div>{% endblock %}""",

"tasks.html": """{% extends 'base.html' %}{% block content %}
<h2 style="color:#fff;margin-bottom:20px">📋 Tasks</h2>
<div class="card">
  <h3>➕ Naya Task</h3>
  <form method="POST" action="/admin/tasks/add">
    <input name="title" placeholder="Title" required>
    <input name="reward" placeholder="Reward (coins)" type="number" step="0.1" required>
    <input name="link" placeholder="Link (https://...)" required>
    <textarea name="description" placeholder="Description" rows="2"></textarea>
    <button class="btn btn-primary" type="submit">Add Task</button>
  </form>
</div>
<div class="card">
  <h3>📋 All Tasks</h3>
  <table>
    <tr><th>Title</th><th>Reward</th><th>Status</th><th>Actions</th></tr>
    {% for t in tasks %}
    <tr>
      <td>{{ t.title }}</td><td>{{ t.reward }} coins</td>
      <td>{% if t.is_active %}<span class="badge badge-green">Active</span>{% else %}<span class="badge badge-red">Disabled</span>{% endif %}</td>
      <td>
        <form class="inline" method="POST" action="/admin/tasks/toggle"><input type="hidden" name="id" value="{{ t.id }}"><button class="btn btn-sm btn-primary">Toggle</button></form>
        <form class="inline" method="POST" action="/admin/tasks/delete"><input type="hidden" name="id" value="{{ t.id }}"><button class="btn btn-sm btn-danger">Delete</button></form>
      </td>
    </tr>{% endfor %}
  </table>
</div>{% endblock %}""",

"channels.html": """{% extends 'base.html' %}{% block content %}
<h2 style="color:#fff;margin-bottom:20px">📢 Channels</h2>
<div class="card">
  <h3>➕ Naya Channel</h3>
  <p style="color:#888;font-size:13px;margin-bottom:12px">⚠️ Bot ko channel ka admin banana zaroori hai</p>
  <form method="POST" action="/admin/channels/add">
    <input name="name" placeholder="Channel Name" required>
    <input name="link" placeholder="Channel Link (https://t.me/...)" required>
    <input name="channel_id" placeholder="Channel ID (@username ya -100xxxxxxxxxx)" required>
    <button class="btn btn-primary" type="submit">Add Channel</button>
  </form>
</div>
<div class="card">
  <h3>📢 All Channels</h3>
  <table>
    <tr><th>Name</th><th>ID</th><th>Status</th><th>Actions</th></tr>
    {% for c in channels %}
    <tr>
      <td>{{ c.channel_name }}</td><td><code>{{ c.channel_id }}</code></td>
      <td>{% if c.is_active %}<span class="badge badge-green">Active</span>{% else %}<span class="badge badge-red">Disabled</span>{% endif %}</td>
      <td>
        <form class="inline" method="POST" action="/admin/channels/toggle"><input type="hidden" name="id" value="{{ c.id }}"><button class="btn btn-sm btn-primary">Toggle</button></form>
        <form class="inline" method="POST" action="/admin/channels/delete"><input type="hidden" name="id" value="{{ c.id }}"><button class="btn btn-sm btn-danger">Delete</button></form>
      </td>
    </tr>{% endfor %}
  </table>
</div>{% endblock %}""",

"withdrawals.html": """{% extends 'base.html' %}{% block content %}
<h2 style="color:#fff;margin-bottom:20px">💳 Withdrawals</h2>
<div class="card">
<table>
  <tr><th>#</th><th>User</th><th>Amount</th><th>Method</th><th>Details</th><th>Date</th><th>Status</th><th>Action</th></tr>
  {% for w in withdrawals %}
  <tr>
    <td>{{ w.id }}</td>
    <td><code>{{ w.user_id }}</code></td>
    <td>{{ w.amount }} coins</td>
    <td>{{ w.method }}</td>
    <td style="max-width:180px;white-space:pre-wrap;font-size:12px">{{ w.details }}</td>
    <td style="font-size:12px">{{ w.created_at[:16] }}</td>
    <td>
      {% if w.status == 'Pending' %}<span class="badge badge-yellow">Pending</span>
      {% elif w.status == 'Approved' %}<span class="badge badge-green">Approved</span>
      {% else %}<span class="badge badge-red">Rejected</span>{% endif %}
    </td>
    <td>
      {% if w.status == 'Pending' %}
      <form class="inline" method="POST" action="/admin/withdrawals/action">
        <input type="hidden" name="id" value="{{ w.id }}">
        <button name="action" value="approve" class="btn btn-sm btn-success">✅</button>
        <button name="action" value="reject"  class="btn btn-sm btn-danger">❌</button>
      </form>
      {% endif %}
    </td>
  </tr>{% endfor %}
</table>
</div>{% endblock %}""",

"broadcast.html": """{% extends 'base.html' %}{% block content %}
<h2 style="color:#fff;margin-bottom:20px">📣 Broadcast</h2>
<div class="card">
  <h3>Message Bhejo</h3>
  <p style="color:#888;font-size:13px;margin-bottom:12px">HTML tags allowed: &lt;b&gt;bold&lt;/b&gt;, &lt;i&gt;italic&lt;/i&gt;, &lt;a href=""&gt;link&lt;/a&gt;</p>
  <form method="POST" action="/admin/broadcast/send">
    <textarea name="message" rows="6" placeholder="Message likhو..." required></textarea>
    <button class="btn btn-primary" type="submit">📣 Sab Users Ko Bhejo</button>
  </form>
</div>{% endblock %}""",

"settings.html": """{% extends 'base.html' %}{% block content %}
<h2 style="color:#fff;margin-bottom:20px">⚙️ Settings</h2>
<div class="card">
  <form method="POST" action="/admin/settings/save">
    <label style="color:#888;font-size:12px">🪙 Referral Reward (coins)</label>
    <input name="referral_reward" value="{{ s.referral_reward }}" type="number" step="0.1">
    <label style="color:#888;font-size:12px">🎁 Daily Bonus (coins)</label>
    <input name="daily_bonus" value="{{ s.daily_bonus }}" type="number" step="0.1">
    <label style="color:#888;font-size:12px">💳 Minimum Withdraw (coins)</label>
    <input name="min_withdraw" value="{{ s.min_withdraw }}" type="number" step="0.1">
    <label style="color:#888;font-size:12px">🛑 Force Join (1=ON, 0=OFF)</label>
    <select name="force_join"><option value="1" {{ 'selected' if s.force_join=='1' }}>ON</option><option value="0" {{ 'selected' if s.force_join=='0' }}>OFF</option></select>
    <label style="color:#888;font-size:12px">👋 Welcome Message</label>
    <textarea name="welcome_message" rows="3">{{ s.welcome_message }}</textarea>
    <label style="color:#888;font-size:12px">🔑 Naya Admin Password (khali choro agar nahi badalna)</label>
    <input name="new_password" type="password" placeholder="New password...">
    <button class="btn btn-primary" type="submit" style="margin-top:8px">💾 Save Settings</button>
  </form>
</div>{% endblock %}""",
}

flask_app = Flask(__name__)
flask_app.secret_key = os.environ.get("FLASK_SECRET_KEY", os.urandom(24))
flask_app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax')

env = Environment(loader=DictLoader(HTML), autoescape=select_autoescape(['html']))

LOGIN_ATTEMPTS = {}

def render(tmpl, **ctx):
    ctx.setdefault("active", "")
    ctx["get_flashed"] = lambda: session.pop("_flashes", [])
    t = env.get_template(tmpl)
    return t.render(**ctx)

def flash(msg, cat="info"):
    session.setdefault("_flashes", []).append((cat, msg))

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    return decorated

@flask_app.route("/")
def index():
    return redirect(url_for("admin_dashboard"))

@flask_app.route("/admin/login", methods=["GET","POST"])
def admin_login():
    if request.method == "POST":
        ip  = request.remote_addr or "?"
        att, locked = LOGIN_ATTEMPTS.get(ip, (0, 0))
        import time
        if time.time() < locked:
            return render("login.html", error=f"Too many attempts. Wait {int(locked-time.time())}s.")
        username = request.form.get("username","")
        password = request.form.get("password","")
        admin    = qdb("SELECT * FROM admin WHERE username=?", (username,), one=True)
        valid    = False
        if admin:
            try:
                valid = check_password_hash(admin["password"], password)
            except Exception:
                valid = admin["password"] == password
                if valid:
                    qdb("UPDATE admin SET password=? WHERE username=?",
                        (generate_password_hash(password), username), commit=True)
        if valid:
            LOGIN_ATTEMPTS.pop(ip, None)
            session.clear()
            session["admin_logged_in"] = True
            session["admin_user"] = username
            return redirect(url_for("admin_dashboard"))
        att += 1
        if att >= 5:
            LOGIN_ATTEMPTS[ip] = (att, time.time()+300)
        else:
            LOGIN_ATTEMPTS[ip] = (att, 0)
        return render("login.html", error="Invalid credentials.")
    return render("login.html")

@flask_app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))

@flask_app.route("/admin")
@login_required
def admin_dashboard():
    stats = {
        "users":    qdb("SELECT COUNT(*) as c FROM users", one=True)["c"],
        "today":    qdb("SELECT COUNT(*) as c FROM users WHERE date(join_date)=date('now')", one=True)["c"],
        "refs":     qdb("SELECT COUNT(*) as c FROM referrals", one=True)["c"],
        "paid":     qdb("SELECT COALESCE(SUM(amount),0) as c FROM withdraw_requests WHERE status='Approved'", one=True)["c"],
        "pending":  qdb("SELECT COUNT(*) as c FROM withdraw_requests WHERE status='Pending'", one=True)["c"],
        "tasks":    qdb("SELECT COUNT(*) as c FROM tasks WHERE is_active=1", one=True)["c"],
        "channels": qdb("SELECT COUNT(*) as c FROM channels WHERE is_active=1", one=True)["c"],
        "blocked":  qdb("SELECT COUNT(*) as c FROM users WHERE is_blocked=1", one=True)["c"],
    }
    return render("dashboard.html", stats=stats, active="dashboard")

@flask_app.route("/admin/users")
@login_required
def admin_users():
    q    = request.args.get("q","")
    page = int(request.args.get("page",0))
    PER  = 20
    if q:
        users = qdb("SELECT * FROM users WHERE user_id LIKE ? OR username LIKE ? ORDER BY id DESC LIMIT ? OFFSET ?",
                    (f"%{q}%",f"%{q}%",PER,page*PER))
    else:
        users = qdb("SELECT * FROM users ORDER BY id DESC LIMIT ? OFFSET ?", (PER,page*PER))
    return render("users.html", users=users, q=q, page=page, has_next=len(users)==PER, active="users")

@flask_app.route("/admin/users/block", methods=["POST"])
@login_required
def admin_block_user():
    qdb("UPDATE users SET is_blocked=? WHERE user_id=?",
        (int(request.form["block"]), int(request.form["user_id"])), commit=True)
    return redirect(url_for("admin_users"))

@flask_app.route("/admin/users/balance", methods=["POST"])
@login_required
def admin_set_balance():
    try:
        bal = float(request.form["balance"])
        if bal < 0: raise ValueError
        qdb("UPDATE users SET balance=? WHERE user_id=?", (bal, int(request.form["user_id"])), commit=True)
    except ValueError:
        flash("Valid number bhejo.", "error")
    return redirect(url_for("admin_users"))

@flask_app.route("/admin/tasks")
@login_required
def admin_tasks():
    return render("tasks.html", tasks=qdb("SELECT * FROM tasks ORDER BY id DESC"), active="tasks")

@flask_app.route("/admin/tasks/add", methods=["POST"])
@login_required
def admin_add_task():
    try:
        reward = float(request.form["reward"])
        if reward < 0: raise ValueError
        qdb("INSERT INTO tasks (title,reward,link,description) VALUES (?,?,?,?)",
            (request.form["title"],reward,request.form["link"],request.form.get("description","")), commit=True)
        flash("Task add ho gaya!")
    except Exception as e:
        flash(f"Error: {e}", "error")
    return redirect(url_for("admin_tasks"))

@flask_app.route("/admin/tasks/toggle", methods=["POST"])
@login_required
def admin_toggle_task():
    qdb("UPDATE tasks SET is_active=CASE WHEN is_active=1 THEN 0 ELSE 1 END WHERE id=?",
        (int(request.form["id"]),), commit=True)
    return redirect(url_for("admin_tasks"))

@flask_app.route("/admin/tasks/delete", methods=["POST"])
@login_required
def admin_delete_task():
    qdb("DELETE FROM tasks WHERE id=?", (int(request.form["id"]),), commit=True)
    return redirect(url_for("admin_tasks"))

@flask_app.route("/admin/channels")
@login_required
def admin_channels():
    return render("channels.html", channels=qdb("SELECT * FROM channels ORDER BY id DESC"), active="channels")

@flask_app.route("/admin/channels/add", methods=["POST"])
@login_required
def admin_add_channel():
    n = request.form.get("name","").strip()
    l = request.form.get("link","").strip()
    i = request.form.get("channel_id","").strip()
    if not (n and l and i):
        flash("Sab fields zaroori hain.", "error")
        return redirect(url_for("admin_channels"))
    qdb("INSERT INTO channels (channel_name,channel_link,channel_id) VALUES (?,?,?)", (n,l,i), commit=True)
    flash("Channel add ho gaya!")
    return redirect(url_for("admin_channels"))

@flask_app.route("/admin/channels/toggle", methods=["POST"])
@login_required
def admin_toggle_channel():
    qdb("UPDATE channels SET is_active=CASE WHEN is_active=1 THEN 0 ELSE 1 END WHERE id=?",
        (int(request.form["id"]),), commit=True)
    return redirect(url_for("admin_channels"))

@flask_app.route("/admin/channels/delete", methods=["POST"])
@login_required
def admin_delete_channel():
    qdb("DELETE FROM channels WHERE id=?", (int(request.form["id"]),), commit=True)
    return redirect(url_for("admin_channels"))

@flask_app.route("/admin/withdrawals")
@login_required
def admin_withdrawals():
    status = request.args.get("status","Pending")
    wds    = qdb("SELECT * FROM withdraw_requests WHERE status=? ORDER BY id DESC", (status,))
    return render("withdrawals.html", withdrawals=wds, active="withdrawals")

@flask_app.route("/admin/withdrawals/action", methods=["POST"])
@login_required
def admin_wd_action():
    action = request.form.get("action")
    if action not in ("approve","reject"):
        return redirect(url_for("admin_withdrawals"))
    req_id = int(request.form["id"])
    w = qdb("SELECT * FROM withdraw_requests WHERE id=?", (req_id,), one=True)
    if not w or w["status"] != "Pending":
        flash("Pehle hi process ho gaya.", "error")
        return redirect(url_for("admin_withdrawals"))
    if action == "approve":
        qdb("UPDATE withdraw_requests SET status='Approved' WHERE id=?", (req_id,), commit=True)
    else:
        qdb("UPDATE withdraw_requests SET status='Rejected' WHERE id=?", (req_id,), commit=True)
        qdb("UPDATE users SET balance=balance+? WHERE user_id=?", (w["amount"], w["user_id"]), commit=True)
    flash(f"Request {'approved' if action=='approve' else 'rejected'}!")
    return redirect(url_for("admin_withdrawals"))

@flask_app.route("/admin/broadcast")
@login_required
def admin_broadcast_page():
    return render("broadcast.html", active="broadcast")

@flask_app.route("/admin/broadcast/send", methods=["POST"])
@login_required
def admin_broadcast_send():
    msg   = request.form.get("message","").strip()
    if not msg:
        flash("Message khali nahi ho sakta.", "error")
        return redirect(url_for("admin_broadcast_page"))
    users = qdb("SELECT user_id FROM users WHERE is_blocked=0")
    sent = failed = 0
    bot_token = BOT_TOKEN
    import requests as req_lib
    for u in users:
        try:
            r = req_lib.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={"chat_id": u["user_id"], "text": msg, "parse_mode": "HTML"},
                timeout=5
            )
            if r.ok: sent += 1
            else: failed += 1
        except Exception:
            failed += 1
    flash(f"Broadcast done! ✅ Sent: {sent} | ❌ Failed: {failed}")
    return redirect(url_for("admin_broadcast_page"))

@flask_app.route("/admin/settings")
@login_required
def admin_settings():
    s = {k: get_setting(k) for k in ["referral_reward","daily_bonus","min_withdraw","force_join","welcome_message"]}
    return render("settings.html", s=s, active="settings")

@flask_app.route("/admin/settings/save", methods=["POST"])
@login_required
def admin_save_settings():
    for k in ["referral_reward","daily_bonus","min_withdraw"]:
        if k in request.form:
            try:
                v = float(request.form[k])
                if v < 0: raise ValueError
                set_setting(k, v)
            except ValueError:
                flash(f"{k} valid number hona chahiye.", "error")
                return redirect(url_for("admin_settings"))
    fj = request.form.get("force_join","")
    if fj in ("0","1"):
        set_setting("force_join", fj)
    wm = request.form.get("welcome_message","").strip()
    if wm:
        set_setting("welcome_message", wm)
    np = request.form.get("new_password","").strip()
    if np:
        admin_user = session.get("admin_user", ADMIN_USERNAME)
        qdb("UPDATE admin SET password=? WHERE username=?",
            (generate_password_hash(np), admin_user), commit=True)
        flash("Password bhi update ho gaya!")
    flash("Settings save ho gayi!")
    return redirect(url_for("admin_settings"))

@flask_app.route("/health")
def health():
    return jsonify({"status":"ok"})

def run_bot():
    """Telegram bot ko polling mode mein background thread me chalao."""
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(TypeHandler(Update, check_force_join), group=-1)
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot polling mode start ho raha hai...")
    # stop_signals=None zaroori hai kyunki ye main thread nahi hai
    # (signal handlers sirf main thread me install ho sakte hain)
    app.run_polling(drop_pending_updates=True, stop_signals=None)

# ==========================================
# MAIN
# ==========================================
def main():
    logger.info("App start ho raha hai...")

    # Bot ko background thread me polling ke saath chalao
    t = threading.Thread(target=run_bot, daemon=True)
    t.start()
    logger.info("Bot background thread started.")

    # Flask ko Render ke public PORT pe chalao (main thread) taaki
    # admin panel bahar se accessible ho. Render har web service ke
    # liye is $PORT env var ko set karta hai.
    port = int(os.environ.get("PORT", os.environ.get("FLASK_PORT", 5000)))
    logger.info(f"Flask admin panel starting on port {port}")
    flask_app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

if __name__ == "__main__":
    main()
