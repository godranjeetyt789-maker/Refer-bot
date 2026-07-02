import os
import sqlite3
import asyncio
import logging
from datetime import datetime, timedelta

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes, TypeHandler, ApplicationHandlerStop
)

# ==========================================
# CONFIGURATION
# ==========================================
BOT_TOKEN         = "8273026626:AAFIcN-Esy0ZUnFr29LSiEDlrYAcnvKqnHg"
ADMIN_TELEGRAM_ID = 6106058051

DEFAULT_REFERRAL_REWARD = 10
DEFAULT_DAILY_BONUS     = 5
DEFAULT_MIN_WITHDRAW    = 100

DB_FILE = os.environ.get("DB_FILE", "bot_database.db")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==========================================
# DATABASE
# ==========================================
def get_conn():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=20)
    conn.row_factory = sqlite3.Row
    return conn

def qdb(query, args=(), one=False, commit=False):
    """
    Run a query.
    - commit=False (default): returns one row or list of rows
    - commit=True           : commits and returns lastrowid (for INSERT/UPDATE/DELETE)
    """
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
            user_id BIGINT UNIQUE,
            username TEXT,
            join_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            total_referrals INT DEFAULT 0,
            successful_referrals INT DEFAULT 0,
            balance FLOAT DEFAULT 0.0,
            is_blocked BOOLEAN DEFAULT 0,
            last_bonus TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS referrals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            referrer_id BIGINT,
            referred_id BIGINT UNIQUE,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            description TEXT,
            reward FLOAT,
            link TEXT,
            is_active BOOLEAN DEFAULT 1
        )""",
        """CREATE TABLE IF NOT EXISTS user_tasks (
            user_id BIGINT,
            task_id INTEGER,
            completed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, task_id)
        )""",
        """CREATE TABLE IF NOT EXISTS channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_name TEXT,
            channel_link TEXT,
            channel_id TEXT,
            is_active BOOLEAN DEFAULT 1
        )""",
        """CREATE TABLE IF NOT EXISTS withdraw_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id BIGINT,
            amount FLOAT,
            method TEXT,
            details TEXT,
            status TEXT DEFAULT 'Pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
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

    # Safe migration for older DBs
    try:
        qdb("ALTER TABLE users ADD COLUMN last_bonus TIMESTAMP", commit=True)
    except Exception:
        pass

init_db()

def get_setting(key, cast=str):
    row = qdb("SELECT value FROM settings WHERE key=?", (key,), one=True)
    if row:
        try:
            return cast(row["value"])
        except Exception:
            pass
    return cast()

def set_setting(key, value):
    qdb("INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)", (key, str(value)), commit=True)

# ==========================================
# STATE STORAGE
# ==========================================
USER_STATES  = {}   # uid -> {"step": ..., ...}
ADMIN_STATES = {}   # uid -> {"step": ..., ...}

# Flag: admin pressed "Exit Admin" → treat them as a normal user temporarily
ADMIN_USER_MODE = set()

def is_admin(uid: int) -> bool:
    return uid == ADMIN_TELEGRAM_ID

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
        [KeyboardButton("📊 Stats"),       KeyboardButton("👥 Users")],
        [KeyboardButton("📋 Tasks"),       KeyboardButton("📢 Channels")],
        [KeyboardButton("💳 Withdrawals"), KeyboardButton("📣 Broadcast")],
        [KeyboardButton("⚙️ Settings"),    KeyboardButton("🔙 User View")],
    ], resize_keyboard=True)

def user_mode_kb():
    """Main keyboard for admin when in user-mode, with a button to return to admin panel."""
    return ReplyKeyboardMarkup([
        [KeyboardButton("👤 Profile"),     KeyboardButton("📋 Tasks")],
        [KeyboardButton("🎁 Daily Bonus"), KeyboardButton("👥 Referrals")],
        [KeyboardButton("💰 Balance"),     KeyboardButton("💳 Withdraw")],
        [KeyboardButton("🏆 Leaderboard"), KeyboardButton("👑 Admin Panel")],
    ], resize_keyboard=True)

def cancel_kb():
    return ReplyKeyboardMarkup([["❌ Cancel"]], resize_keyboard=True)

# ==========================================
# FORCE JOIN CHECK
# ==========================================
async def check_force_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user:
        return
    uid = update.effective_user.id

    # Blocked check
    u = qdb("SELECT is_blocked FROM users WHERE user_id=?", (uid,), one=True)
    if u and u["is_blocked"]:
        if update.message:
            await update.message.reply_text("❌ Aap is bot se block ho gaye hain.")
        elif update.callback_query:
            await update.callback_query.answer("❌ Aap block hain.", show_alert=True)
        raise ApplicationHandlerStop()

    # Admin skips force join
    if is_admin(uid):
        return

    if get_setting("force_join") == "1":
        channels = qdb("SELECT * FROM channels WHERE is_active=1")
        if not channels:
            return
        not_joined = []
        for c in channels:
            try:
                m = await context.bot.get_chat_member(chat_id=c["channel_id"], user_id=uid)
                if m.status not in ("member", "administrator", "creator"):
                    not_joined.append(c)
            except Exception as e:
                logger.error(f"Force join check error for {c['channel_id']}: {e}")
                not_joined.append(c)

        if not_joined:
            buttons = [[InlineKeyboardButton(c["channel_name"], url=c["channel_link"])] for c in not_joined]
            buttons.append([InlineKeyboardButton("✅ Maine Join Kar Liya — Check Karo", callback_data="check_join")])
            msg = "🛑 *Pehle inhe join karo, phir bot use kar sakte ho:*"
            if update.message:
                await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
            elif update.callback_query and update.callback_query.data != "check_join":
                await update.callback_query.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
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
                    qdb("INSERT INTO referrals (referrer_id, referred_id) VALUES (?,?)", (ref_id, uid), commit=True)
                    qdb(
                        "UPDATE users SET balance=balance+?, total_referrals=total_referrals+1, "
                        "successful_referrals=successful_referrals+1 WHERE user_id=?",
                        (reward, ref_id), commit=True
                    )
                    await context.bot.send_message(
                        chat_id=ref_id,
                        text=f"🎉 *Naya Referral!*\nTumhe *{reward} coins* mile!",
                        parse_mode="Markdown"
                    )
                except Exception:
                    pass  # duplicate referral — ignore

    if is_admin(uid):
        ADMIN_USER_MODE.discard(uid)  # reset to admin mode on /start
        await update.message.reply_text(
            "👑 *Admin Panel Mein Aapka Swagat Hai!*\nButtons se sab manage karo.",
            reply_markup=admin_kb(), parse_mode="Markdown"
        )
        return

    welcome = get_setting("welcome_message")
    await update.message.reply_text(welcome, reply_markup=main_kb())

# ==========================================
# INLINE CALLBACKS
# ==========================================
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data  = query.data
    uid   = query.from_user.id
    # NOTE: Do NOT call query.answer() here at the top.
    # Each branch calls it individually so show_alert=True works correctly.

    # ── Force join re-check ──
    if data == "check_join":
        await query.answer("Check ho raha hai…")
        await query.message.delete()
        await context.bot.send_message(uid, "✅ Shukriya! Ab bot use kar sakte ho.", reply_markup=main_kb())
        return

    # ── User: Task info ──
    if data.startswith("task_info_"):
        task_id = int(data.split("_")[2])
        task = qdb("SELECT * FROM tasks WHERE id=?", (task_id,), one=True)
        if not task:
            await query.answer("Task nahi mila.", show_alert=True); return
        done = qdb("SELECT 1 FROM user_tasks WHERE user_id=? AND task_id=?", (uid, task_id), one=True)
        if done:
            await query.answer("Yeh task pehle hi complete ho gaya!", show_alert=True); return
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔗 Task Open Karo", url=task["link"])],
            [InlineKeyboardButton("✅ Verify & Claim",  callback_data=f"task_done_{task_id}")]
        ])
        await query.answer()
        await query.message.edit_text(
            f"📋 *{task['title']}*\n\n"
            f"🪙 Reward: *{task['reward']} coins*\n"
            f"📝 {task['description'] or ''}\n\n"
            f"Link kholo, phir Verify dabao.",
            parse_mode="Markdown", reply_markup=kb
        )
        return

    if data.startswith("task_done_"):
        task_id = int(data.split("_")[2])
        task = qdb("SELECT * FROM tasks WHERE id=?", (task_id,), one=True)
        if not task:
            await query.answer("Task nahi mila.", show_alert=True); return
        done = qdb("SELECT 1 FROM user_tasks WHERE user_id=? AND task_id=?", (uid, task_id), one=True)
        if done:
            await query.answer("Yeh task pehle hi complete ho gaya!", show_alert=True); return
        qdb("INSERT INTO user_tasks (user_id, task_id) VALUES (?,?)", (uid, task_id), commit=True)
        qdb("UPDATE users SET balance=balance+? WHERE user_id=?", (task["reward"], uid), commit=True)
        await query.answer(f"✅ +{task['reward']} coins!", show_alert=True)
        await query.message.edit_text(
            f"✅ *Task Complete!*\nTumhe *{task['reward']} coins* mile. 🎉",
            parse_mode="Markdown"
        )
        return

    # ── Admin: Withdrawal approve/reject ──
    if data.startswith("wd_"):
        if not is_admin(uid):
            await query.answer("Aap admin nahi hain.", show_alert=True); return
        parts  = data.split("_")
        action = parts[1]   # "approve" or "reject"
        req_id = int(parts[2])
        w = qdb("SELECT * FROM withdraw_requests WHERE id=?", (req_id,), one=True)
        if not w or w["status"] != "Pending":
            await query.answer("Yeh request pehle hi process ho chuki hai.", show_alert=True); return
        if action == "approve":
            qdb("UPDATE withdraw_requests SET status='Approved' WHERE id=?", (req_id,), commit=True)
            await context.bot.send_message(
                chat_id=w["user_id"],
                text=(f"✅ <b>Withdrawal Approved!</b>\n"
                      f"<b>{w['amount']} coins</b> — <b>{w['method']}</b> ke zariye process ho gaya."),
                parse_mode="HTML"
            )
            new_text = query.message.text + "\n\n✅ APPROVED"
            await query.edit_message_text(new_text)
        else:
            qdb("UPDATE withdraw_requests SET status='Rejected' WHERE id=?", (req_id,), commit=True)
            qdb("UPDATE users SET balance=balance+? WHERE user_id=?", (w["amount"], w["user_id"]), commit=True)
            await context.bot.send_message(
                chat_id=w["user_id"],
                text=(f"❌ <b>Withdrawal Rejected!</b>\n"
                      f"<b>{w['amount']} coins</b> wapas aapke balance mein aa gaye."),
                parse_mode="HTML"
            )
            new_text = query.message.text + "\n\n❌ REJECTED — coins refunded"
            await query.edit_message_text(new_text)
        await query.answer("Done!")
        return

    # ── Admin: Block / Unblock ──
    if data.startswith("block_") or data.startswith("unblock_"):
        if not is_admin(uid):
            await query.answer("Aap admin nahi hain.", show_alert=True); return
        action  = "block" if data.startswith("block_") else "unblock"
        tgt     = int(data.split("_")[1])
        new_val = 1 if action == "block" else 0
        qdb("UPDATE users SET is_blocked=? WHERE user_id=?", (new_val, tgt), commit=True)
        new_btn_lbl = "🔓 Unblock" if new_val else "🚫 Block"
        new_btn_cb  = f"unblock_{tgt}" if new_val else f"block_{tgt}"
        await query.edit_message_reply_markup(
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(new_btn_lbl,          callback_data=new_btn_cb),
                InlineKeyboardButton("💰 Balance Edit",    callback_data=f"editbal_{tgt}"),
                InlineKeyboardButton("👁 Info",            callback_data=f"uinfo_{tgt}"),
            ]])
        )
        label = "🔴 Block ho gaya" if new_val else "🟢 Unblock ho gaya"
        await query.answer(label, show_alert=True)
        return

    # ── Admin: Edit balance prompt ──
    if data.startswith("editbal_"):
        if not is_admin(uid):
            await query.answer("Aap admin nahi hain.", show_alert=True); return
        tgt = int(data.split("_")[1])
        ADMIN_STATES[uid] = {"step": "edit_balance", "target": tgt}
        await query.answer()
        await context.bot.send_message(
            uid,
            f"💰 User `{tgt}` ka naya balance enter karo:",
            parse_mode="Markdown", reply_markup=cancel_kb()
        )
        return

    # ── Admin: User info ──
    if data.startswith("uinfo_"):
        if not is_admin(uid):
            await query.answer("Aap admin nahi hain.", show_alert=True); return
        tgt = int(data.split("_")[1])
        await query.answer()
        await send_user_info(context, uid, tgt)
        return

    # ── Admin: Task toggle ──
    if data.startswith("tog_task_"):
        if not is_admin(uid):
            await query.answer("Aap admin nahi hain.", show_alert=True); return
        tid = int(data.split("_")[2])
        qdb("UPDATE tasks SET is_active = CASE WHEN is_active=1 THEN 0 ELSE 1 END WHERE id=?", (tid,), commit=True)
        t  = qdb("SELECT * FROM tasks WHERE id=?", (tid,), one=True)
        st = "✅ Active" if t["is_active"] else "❌ Disabled"
        await query.edit_message_text(
            f"📋 *{t['title']}*\n🪙 {t['reward']} coins | {st}\n🔗 {t['link']}",
            parse_mode="Markdown",
            reply_markup=task_action_kb(tid)
        )
        await query.answer(st, show_alert=True)
        return

    # ── Admin: Task delete ──
    if data.startswith("del_task_"):
        if not is_admin(uid):
            await query.answer("Aap admin nahi hain.", show_alert=True); return
        tid = int(data.split("_")[2])
        qdb("DELETE FROM tasks WHERE id=?", (tid,), commit=True)
        await query.answer("🗑 Delete ho gaya!", show_alert=True)
        await query.edit_message_text("🗑 Task delete ho gaya.")
        return

    # ── Admin: Channel toggle ──
    if data.startswith("tog_ch_"):
        if not is_admin(uid):
            await query.answer("Aap admin nahi hain.", show_alert=True); return
        cid = int(data.split("_")[2])
        qdb("UPDATE channels SET is_active = CASE WHEN is_active=1 THEN 0 ELSE 1 END WHERE id=?", (cid,), commit=True)
        c  = qdb("SELECT * FROM channels WHERE id=?", (cid,), one=True)
        st = "✅ Active" if c["is_active"] else "❌ Disabled"
        await query.edit_message_text(
            f"📢 *{c['channel_name']}*\nID: `{c['channel_id']}` | {st}",
            parse_mode="Markdown",
            reply_markup=channel_action_kb(cid)
        )
        await query.answer(st, show_alert=True)
        return

    # ── Admin: Channel delete ──
    if data.startswith("del_ch_"):
        if not is_admin(uid):
            await query.answer("Aap admin nahi hain.", show_alert=True); return
        cid = int(data.split("_")[2])
        qdb("DELETE FROM channels WHERE id=?", (cid,), commit=True)
        await query.answer("🗑 Delete ho gaya!", show_alert=True)
        await query.edit_message_text("🗑 Channel delete ho gaya.")
        return

    # ── Admin: Start add-task flow ──
    if data == "admin_add_task":
        if not is_admin(uid):
            await query.answer("Aap admin nahi hain.", show_alert=True); return
        ADMIN_STATES[uid] = {"step": "task_title"}
        await query.answer()
        await context.bot.send_message(
            uid,
            "📋 *Naya Task — Step 1/4*\n\nTask ka *Title* bhejo:",
            parse_mode="Markdown", reply_markup=cancel_kb()
        )
        return

    # ── Admin: Start add-channel flow ──
    if data == "admin_add_channel":
        if not is_admin(uid):
            await query.answer("Aap admin nahi hain.", show_alert=True); return
        ADMIN_STATES[uid] = {"step": "ch_name"}
        await query.answer()
        await context.bot.send_message(
            uid,
            "📢 *Naya Channel — Step 1/3*\n\nChannel ka *Naam* bhejo:",
            parse_mode="Markdown", reply_markup=cancel_kb()
        )
        return

    # ── Admin: Settings field edit prompt ──
    if data.startswith("setedit_"):
        if not is_admin(uid):
            await query.answer("Aap admin nahi hain.", show_alert=True); return
        key = data[len("setedit_"):]
        labels = {
            "referral_reward": "Referral Reward (coins mein number)",
            "daily_bonus":     "Daily Bonus (coins mein number)",
            "min_withdraw":    "Minimum Withdraw (coins mein number)",
            "welcome_message": "Welcome Message (koi bhi text)",
            "force_join":      "Force Join (1 = ON, 0 = OFF)",
        }
        ADMIN_STATES[uid] = {"step": "set_value", "key": key}
        await query.answer()
        await context.bot.send_message(
            uid,
            f"⚙️ *{labels.get(key, key)}* ke liye naya value bhejo:",
            parse_mode="Markdown", reply_markup=cancel_kb()
        )
        return

    # ── Admin: Users pagination ──
    if data.startswith("users_page_"):
        if not is_admin(uid):
            await query.answer("Aap admin nahi hain.", show_alert=True); return
        page = int(data.split("_")[2])
        await query.answer()
        # Pass the CallbackQuery object so we can edit the existing message
        await send_users_page(query, context, page, editing=True)
        return

    # ── Admin: Search user prompt ──
    if data == "user_search":
        if not is_admin(uid):
            await query.answer("Aap admin nahi hain.", show_alert=True); return
        ADMIN_STATES[uid] = {"step": "search_user"}
        await query.answer()
        await context.bot.send_message(uid, "🔍 Jis user ko dhundhna hai uska *User ID* bhejo:", parse_mode="Markdown", reply_markup=cancel_kb())
        return

    # Fallback — unknown callback
    await query.answer()

# ==========================================
# ACTION KEYBOARDS
# ==========================================
def task_action_kb(task_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔁 Toggle On/Off", callback_data=f"tog_task_{task_id}"),
        InlineKeyboardButton("🗑 Delete",         callback_data=f"del_task_{task_id}"),
    ]])

def channel_action_kb(ch_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔁 Toggle On/Off", callback_data=f"tog_ch_{ch_id}"),
        InlineKeyboardButton("🗑 Delete",         callback_data=f"del_ch_{ch_id}"),
    ]])

# ==========================================
# ADMIN PANEL SCREENS
# ==========================================
async def admin_stats(update: Update):
    total_users  = qdb("SELECT COUNT(*) as c FROM users", one=True)["c"]
    total_refs   = qdb("SELECT COUNT(*) as c FROM referrals", one=True)["c"]
    paid_out     = qdb("SELECT COALESCE(SUM(amount),0) as c FROM withdraw_requests WHERE status='Approved'", one=True)["c"]
    pending_wd   = qdb("SELECT COUNT(*) as c FROM withdraw_requests WHERE status='Pending'", one=True)["c"]
    active_tasks = qdb("SELECT COUNT(*) as c FROM tasks WHERE is_active=1", one=True)["c"]
    active_ch    = qdb("SELECT COUNT(*) as c FROM channels WHERE is_active=1", one=True)["c"]
    blocked      = qdb("SELECT COUNT(*) as c FROM users WHERE is_blocked=1", one=True)["c"]
    today_users  = qdb(
        "SELECT COUNT(*) as c FROM users WHERE date(join_date)=date('now')", one=True
    )["c"]
    await update.message.reply_text(
        f"📊 *Bot Statistics*\n\n"
        f"👥 Total Users: *{total_users}*\n"
        f"🆕 Aaj Naye Users: *{today_users}*\n"
        f"🔗 Total Referrals: *{total_refs}*\n"
        f"✅ Total Paid Out: *{paid_out} coins*\n"
        f"⏳ Pending Withdrawals: *{pending_wd}*\n"
        f"📋 Active Tasks: *{active_tasks}*\n"
        f"📢 Active Channels: *{active_ch}*\n"
        f"🚫 Blocked Users: *{blocked}*",
        parse_mode="Markdown"
    )

async def send_users_page(update_or_query, context, page=0, editing=False):
    """
    update_or_query:
      - Update object  → send a new message  (editing=False)
      - CallbackQuery  → edit existing message (editing=True)
    """
    PER_PAGE    = 8
    offset      = page * PER_PAGE
    users       = qdb("SELECT * FROM users ORDER BY id DESC LIMIT ? OFFSET ?", (PER_PAGE, offset))
    total       = qdb("SELECT COUNT(*) as c FROM users", one=True)["c"]
    total_pages = max(1, -(-total // PER_PAGE))

    if not users:
        text = "Koi user nahi mila."
        kb   = None
    else:
        text = f"👥 *Users — Page {page+1}/{total_pages}* (Total: {total})\n\n"
        for u in users:
            uname  = f"@{u['username']}" if u["username"] else "—"
            status = "🔴" if u["is_blocked"] else "🟢"
            text  += f"{status} `{u['user_id']}` {uname} | 💰{u['balance']} | 👥{u['total_referrals']}\n"

        kb_rows = []

        # Nav row
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"users_page_{page-1}"))
        nav.append(InlineKeyboardButton("🔍 User Dhundho", callback_data="user_search"))
        if (page + 1) < total_pages:
            nav.append(InlineKeyboardButton("Next ▶️", callback_data=f"users_page_{page+1}"))
        kb_rows.append(nav)

        # Per-user action buttons
        for u in users:
            block_lbl = "🔓" if u["is_blocked"] else "🚫"
            block_cb  = f"unblock_{u['user_id']}" if u["is_blocked"] else f"block_{u['user_id']}"
            kb_rows.append([
                InlineKeyboardButton(f"{block_lbl} {u['user_id']}", callback_data=block_cb),
                InlineKeyboardButton("💰",                          callback_data=f"editbal_{u['user_id']}"),
                InlineKeyboardButton("👁",                          callback_data=f"uinfo_{u['user_id']}"),
            ])
        kb = InlineKeyboardMarkup(kb_rows)

    if editing:
        # Called from a callback — edit the existing message
        await update_or_query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)
    else:
        # Called from a reply keyboard button — send a new message
        await update_or_query.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)

async def send_user_info(context, admin_uid, tgt_uid):
    u = qdb("SELECT * FROM users WHERE user_id=?", (tgt_uid,), one=True)
    if not u:
        await context.bot.send_message(admin_uid, "❌ User nahi mila."); return
    td        = qdb("SELECT COUNT(*) as c FROM user_tasks WHERE user_id=?", (tgt_uid,), one=True)["c"]
    wd_count  = qdb("SELECT COUNT(*) as c FROM withdraw_requests WHERE user_id=?", (tgt_uid,), one=True)["c"]
    status    = "🔴 Blocked" if u["is_blocked"] else "🟢 Active"
    block_lbl = "🔓 Unblock" if u["is_blocked"] else "🚫 Block"
    block_cb  = f"unblock_{tgt_uid}" if u["is_blocked"] else f"block_{tgt_uid}"
    text = (
        f"👤 *User Info*\n\n"
        f"🆔 ID: `{u['user_id']}`\n"
        f"📛 Username: @{u['username'] or 'N/A'}\n"
        f"📅 Joined: {u['join_date'][:10]}\n\n"
        f"💰 Balance: *{u['balance']} coins*\n"
        f"👥 Referrals: *{u['total_referrals']}*\n"
        f"📋 Tasks Done: *{td}*\n"
        f"💳 Withdrawals: *{wd_count}*\n"
        f"Status: {status}"
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(block_lbl,          callback_data=block_cb),
        InlineKeyboardButton("💰 Balance Edit",  callback_data=f"editbal_{tgt_uid}"),
    ]])
    await context.bot.send_message(admin_uid, text, parse_mode="Markdown", reply_markup=kb)

async def admin_tasks_screen(update: Update):
    tasks = qdb("SELECT * FROM tasks ORDER BY id DESC")
    await update.message.reply_text(
        "📋 *Task Management*\n\nMojood tasks neeche hain.\nNaya task add karne ke liye button dabao:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("➕ Naya Task Add Karo", callback_data="admin_add_task")
        ]])
    )
    if not tasks:
        await update.message.reply_text("Abhi koi task nahi hai.")
        return
    for t in tasks:
        st   = "✅ Active" if t["is_active"] else "❌ Disabled"
        text = f"📋 *{t['title']}*\n🪙 {t['reward']} coins | {st}\n🔗 {t['link']}"
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=task_action_kb(t["id"]))

async def admin_channels_screen(update: Update):
    channels = qdb("SELECT * FROM channels ORDER BY id DESC")
    await update.message.reply_text(
        "📢 *Channel Management*\n\n⚠️ Bot ko channel ka admin banana zaroori hai.\nNaya channel add karne ke liye button dabao:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("➕ Naya Channel Add Karo", callback_data="admin_add_channel")
        ]])
    )
    if not channels:
        await update.message.reply_text("Abhi koi channel nahi hai.")
        return
    for c in channels:
        st   = "✅ Active" if c["is_active"] else "❌ Disabled"
        text = f"📢 *{c['channel_name']}*\nID: `{c['channel_id']}` | {st}\n🔗 {c['channel_link']}"
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=channel_action_kb(c["id"]))

async def admin_withdrawals_screen(update: Update):
    pending = qdb("SELECT * FROM withdraw_requests WHERE status='Pending' ORDER BY id DESC")
    if not pending:
        await update.message.reply_text("✅ Koi pending withdrawal nahi hai."); return
    await update.message.reply_text(f"💳 *Pending Withdrawals: {len(pending)}*", parse_mode="Markdown")
    for w in pending:
        u     = qdb("SELECT username FROM users WHERE user_id=?", (w["user_id"],), one=True)
        uname = f"@{u['username']}" if u and u["username"] else f"ID:{w['user_id']}"
        text  = (
            f"💳 <b>Request #{w['id']}</b>\n"
            f"👤 {uname} (<code>{w['user_id']}</code>)\n"
            f"💰 <b>{w['amount']} coins</b> — <b>{w['method']}</b>\n"
            f"📋 Details:\n<pre>{w['details']}</pre>\n"
            f"🕐 {w['created_at']}"
        )
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Approve", callback_data=f"wd_approve_{w['id']}"),
            InlineKeyboardButton("❌ Reject",  callback_data=f"wd_reject_{w['id']}"),
        ]])
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)

async def admin_settings_screen(update: Update):
    ref     = get_setting("referral_reward")
    daily   = get_setting("daily_bonus")
    min_w   = get_setting("min_withdraw")
    fj      = get_setting("force_join")
    welcome = get_setting("welcome_message")
    await update.message.reply_text(
        f"⚙️ *Current Settings*\n\n"
        f"🪙 Referral Reward: `{ref} coins`\n"
        f"🎁 Daily Bonus: `{daily} coins`\n"
        f"💳 Min Withdraw: `{min_w} coins`\n"
        f"🛑 Force Join: `{'✅ ON' if fj == '1' else '❌ OFF'}`\n\n"
        f"👋 Welcome Message:\n_{welcome}_\n\n"
        f"Koi bhi setting change karne ke liye button dabao:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🪙 Referral Reward",   callback_data="setedit_referral_reward")],
            [InlineKeyboardButton("🎁 Daily Bonus",       callback_data="setedit_daily_bonus")],
            [InlineKeyboardButton("💳 Min Withdraw",      callback_data="setedit_min_withdraw")],
            [InlineKeyboardButton("🛑 Force Join Toggle", callback_data="setedit_force_join")],
            [InlineKeyboardButton("👋 Welcome Message",   callback_data="setedit_welcome_message")],
        ])
    )

async def admin_broadcast_prompt(update: Update, uid: int):
    ADMIN_STATES[uid] = {"step": "broadcast"}
    await update.message.reply_text(
        "📣 *Broadcast*\n\n"
        "Woh message type karo jo <b>saare users</b> ko jaana chahiye.\n"
        "_(HTML tags allowed: &lt;b&gt;bold&lt;/b&gt;, &lt;i&gt;italic&lt;/i&gt;)_",
        parse_mode="Markdown", reply_markup=cancel_kb()
    )

async def do_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE, msg: str):
    users = qdb("SELECT user_id FROM users WHERE is_blocked=0")
    sent = failed = 0
    sm = await update.message.reply_text(f"📣 {len(users)} users ko bhej raha hoon…")
    for u in users:
        try:
            await context.bot.send_message(chat_id=u["user_id"], text=msg, parse_mode="HTML")
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)  # Telegram rate limit
    await sm.edit_text(
        f"📣 *Broadcast Ho Gaya!*\n✅ Bheja: {sent}\n❌ Failed: {failed}",
        parse_mode="Markdown"
    )

# ==========================================
# ADMIN STATE HANDLER
# ==========================================
async def handle_admin_state(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: int, text: str) -> bool:
    if uid not in ADMIN_STATES:
        return False
    state = ADMIN_STATES[uid]
    step  = state["step"]

    if step == "broadcast":
        ADMIN_STATES.pop(uid, None)
        await do_broadcast(update, context, text)
        return True

    if step == "edit_balance":
        try:
            val = float(text)
            if val < 0: raise ValueError
            qdb("UPDATE users SET balance=? WHERE user_id=?", (val, state["target"]), commit=True)
            ADMIN_STATES.pop(uid, None)
            await update.message.reply_text(
                f"✅ User `{state['target']}` ka balance *{val} coins* set ho gaya.",
                parse_mode="Markdown", reply_markup=admin_kb()
            )
        except ValueError:
            ADMIN_STATES.pop(uid, None)
            await update.message.reply_text("❌ Valid number bhejo. (e.g. 100 ya 50.5)", reply_markup=admin_kb())
        return True

    if step == "search_user":
        ADMIN_STATES.pop(uid, None)
        try:
            tgt = int(text.strip())
            await send_user_info(context, uid, tgt)
            await update.message.reply_text("⬆️ User info upar hai.", reply_markup=admin_kb())
        except ValueError:
            await update.message.reply_text("❌ Valid User ID (number) bhejo.", reply_markup=admin_kb())
        return True

    if step == "set_value":
        key = state["key"]
        val = text.strip()
        ADMIN_STATES.pop(uid, None)
        if key in ("referral_reward", "daily_bonus", "min_withdraw"):
            try:
                fval = float(val)
                if fval < 0: raise ValueError
            except ValueError:
                await update.message.reply_text("❌ Valid positive number bhejo.", reply_markup=admin_kb())
                return True
        elif key == "force_join":
            if val not in ("0", "1"):
                await update.message.reply_text("❌ Sirf 0 (OFF) ya 1 (ON) bhejo.", reply_markup=admin_kb())
                return True
        set_setting(key, val)
        await update.message.reply_text(
            f"✅ *{key}* update ho gaya!\nNaya value: `{val}`",
            parse_mode="Markdown", reply_markup=admin_kb()
        )
        return True

    # Add Task flow
    if step == "task_title":
        state.update({"step": "task_reward", "title": text})
        await update.message.reply_text("📋 *Step 2/4* — Task ka *Reward* (coins) bhejo:", parse_mode="Markdown")
        return True
    if step == "task_reward":
        try:
            reward = float(text)
            if reward < 0: raise ValueError
            state.update({"step": "task_link", "reward": reward})
            await update.message.reply_text("📋 *Step 3/4* — Task ka *Link* bhejo:", parse_mode="Markdown")
        except ValueError:
            await update.message.reply_text("❌ Valid number bhejo. (e.g. 10 ya 5.5)")
        return True
    if step == "task_link":
        state.update({"step": "task_desc", "link": text})
        await update.message.reply_text("📋 *Step 4/4* — Task ka *Description* bhejo:", parse_mode="Markdown")
        return True
    if step == "task_desc":
        qdb(
            "INSERT INTO tasks (title, reward, link, description) VALUES (?,?,?,?)",
            (state["title"], state["reward"], state["link"], text), commit=True
        )
        ADMIN_STATES.pop(uid, None)
        await update.message.reply_text(
            f"✅ *Task Add Ho Gaya!*\n\n"
            f"📋 Title: {state['title']}\n"
            f"🪙 Reward: {state['reward']} coins\n"
            f"🔗 Link: {state['link']}",
            parse_mode="Markdown", reply_markup=admin_kb()
        )
        return True

    # Add Channel flow
    if step == "ch_name":
        state.update({"step": "ch_link", "name": text})
        await update.message.reply_text(
            "📢 *Step 2/3* — Channel ka *Link* bhejo:\n_(e.g. https://t.me/yourchannel)_",
            parse_mode="Markdown"
        )
        return True
    if step == "ch_link":
        state.update({"step": "ch_id", "link": text})
        await update.message.reply_text(
            "📢 *Step 3/3* — Channel ka *ID ya @username* bhejo:\n\n"
            "• Public channel: `@channelname`\n"
            "• Private channel: `-100xxxxxxxxxx`\n\n"
            "_Channel ID jaanne ke liye @userinfobot se pata karo._",
            parse_mode="Markdown"
        )
        return True
    if step == "ch_id":
        qdb(
            "INSERT INTO channels (channel_name, channel_link, channel_id) VALUES (?,?,?)",
            (state["name"], state["link"], text.strip()), commit=True
        )
        ADMIN_STATES.pop(uid, None)
        await update.message.reply_text(
            f"✅ *Channel Add Ho Gaya!*\n\n"
            f"📢 Name: {state['name']}\n"
            f"🆔 ID: `{text.strip()}`",
            parse_mode="Markdown", reply_markup=admin_kb()
        )
        return True

    return False

# ==========================================
# USER WITHDRAWAL FLOW
# ==========================================
async def process_withdrawal(update: Update, context: ContextTypes.DEFAULT_TYPE,
                              uid: int, amount: float, method: str, details: str):
    qdb("UPDATE users SET balance=balance-? WHERE user_id=?", (amount, uid), commit=True)
    req_id = qdb(
        "INSERT INTO withdraw_requests (user_id, amount, method, details) VALUES (?,?,?,?)",
        (uid, amount, method, details), commit=True
    )
    USER_STATES.pop(uid, None)
    await update.message.reply_text(
        "✅ *Withdrawal Request Submit Ho Gaya!*\n\nAdmin jald review karega. Wait karo.",
        parse_mode="Markdown", reply_markup=main_kb()
    )
    # Notify admin
    u     = qdb("SELECT username FROM users WHERE user_id=?", (uid,), one=True)
    uname = f"@{u['username']}" if u and u["username"] else f"ID:{uid}"
    text  = (
        f"💳 <b>Naya Withdrawal Request #{req_id}</b>\n\n"
        f"👤 {uname} (<code>{uid}</code>)\n"
        f"💰 Amount: <b>{amount} coins</b>\n"
        f"🏦 Method: <b>{method}</b>\n"
        f"📋 Details:\n<pre>{details}</pre>"
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"wd_approve_{req_id}"),
        InlineKeyboardButton("❌ Reject",  callback_data=f"wd_reject_{req_id}"),
    ]])
    try:
        await context.bot.send_message(ADMIN_TELEGRAM_ID, text, parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        logger.error(f"Admin notify fail: {e}")

async def handle_withdrawal_state(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                   uid: int, text: str) -> bool:
    if uid not in USER_STATES:
        return False
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
                await update.message.reply_text(f"❌ Balance nahi hai. Tumhara balance: {balance} coins.")
                USER_STATES.pop(uid, None); return True
            state.update({"step": "method", "amount": amount})
            await update.message.reply_text(
                f"✅ Amount: *{amount} coins*\n\n🏦 *Payment method chuno:*",
                parse_mode="Markdown",
                reply_markup=ReplyKeyboardMarkup([
                    ["🏦 Bank Transfer", "📱 UPI"],
                    ["💳 Crypto",        "❌ Cancel"]
                ], resize_keyboard=True)
            )
        except ValueError:
            await update.message.reply_text("❌ Valid number bhejo. (e.g. 100)")
            USER_STATES.pop(uid, None)
        return True

    if step == "method":
        if text == "🏦 Bank Transfer":
            state.update({"method": "Bank Transfer", "step": "bank_name"})
            await update.message.reply_text("🏦 *Bank ka naam* bhejo:", parse_mode="Markdown", reply_markup=cancel_kb())
        elif text == "📱 UPI":
            state.update({"method": "UPI", "step": "upi_id"})
            await update.message.reply_text("📱 *UPI ID* bhejo: (e.g. name@bank)", parse_mode="Markdown", reply_markup=cancel_kb())
        elif text == "💳 Crypto":
            state.update({"method": "Crypto", "step": "crypto_addr"})
            await update.message.reply_text("💳 *Crypto Wallet Address* bhejo (USDT TRC20):", parse_mode="Markdown", reply_markup=cancel_kb())
        else:
            await update.message.reply_text("❌ Neeche se ek button dabao.")
        return True

    if step == "bank_name":
        state.update({"bank_name": text, "step": "bank_ac"})
        await update.message.reply_text("🔢 *Account Number* bhejo:", parse_mode="Markdown"); return True
    if step == "bank_ac":
        state.update({"bank_ac": text, "step": "bank_ifsc"})
        await update.message.reply_text("🔠 *IFSC Code* bhejo:", parse_mode="Markdown"); return True
    if step == "bank_ifsc":
        state.update({"bank_ifsc": text, "step": "bank_holder"})
        await update.message.reply_text("👤 *Account Holder ka Naam* bhejo:", parse_mode="Markdown"); return True
    if step == "bank_holder":
        details = (
            f"Bank: {state['bank_name']}\n"
            f"A/C: {state['bank_ac']}\n"
            f"IFSC: {state['bank_ifsc']}\n"
            f"Name: {text}"
        )
        await process_withdrawal(update, context, uid, state["amount"], "Bank Transfer", details)
        return True
    if step == "upi_id":
        await process_withdrawal(update, context, uid, state["amount"], "UPI", f"UPI ID: {text}")
        return True
    if step == "crypto_addr":
        await process_withdrawal(update, context, uid, state["amount"], "Crypto", f"Wallet: {text}")
        return True

    return False

# ==========================================
# USER FEATURES
# ==========================================
async def user_balance(update: Update, uid: int):
    u = qdb("SELECT balance FROM users WHERE user_id=?", (uid,), one=True)
    await update.message.reply_text(
        f"💰 *Tumhara Balance*\n\n`{u['balance']} coins`",
        parse_mode="Markdown"
    )

async def user_referrals(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: int):
    u      = qdb("SELECT total_referrals, successful_referrals FROM users WHERE user_id=?", (uid,), one=True)
    link   = f"https://t.me/{context.bot.username}?start={uid}"
    reward = get_setting("referral_reward")
    await update.message.reply_text(
        f"👥 *Referral System*\n\n"
        f"🪙 Har invite pe reward: *{reward} coins*\n"
        f"📊 Tumhare total invites: *{u['total_referrals']}*\n\n"
        f"🔗 *Tumhara Referral Link:*\n`{link}`\n\n"
        f"Is link ko share karo aur coins kamao!",
        parse_mode="Markdown"
    )

async def user_daily(update: Update, uid: int):
    u   = qdb("SELECT last_bonus FROM users WHERE user_id=?", (uid,), one=True)
    now = datetime.now()
    if u["last_bonus"]:
        try:
            last = datetime.strptime(u["last_bonus"], "%Y-%m-%d %H:%M:%S")
            if now < last + timedelta(days=1):
                rem  = (last + timedelta(days=1)) - now
                h, r = divmod(int(rem.total_seconds()), 3600)
                m, s = divmod(r, 60)
                await update.message.reply_text(
                    f"⏳ *Daily bonus pehle hi le liya!*\n\n"
                    f"Wapas aana: *{h} ghante {m} minute* mein",
                    parse_mode="Markdown"
                )
                return
        except ValueError:
            pass  # bad date format in DB, allow claim
    bonus = get_setting("daily_bonus", float)
    qdb(
        "UPDATE users SET balance=balance+?, last_bonus=? WHERE user_id=?",
        (bonus, now.strftime("%Y-%m-%d %H:%M:%S"), uid), commit=True
    )
    await update.message.reply_text(
        f"🎁 *Daily Bonus Claim Ho Gaya!*\n\n+*{bonus} coins* aapke account mein aaye. ✅",
        parse_mode="Markdown"
    )

async def user_tasks(update: Update, uid: int):
    tasks = qdb("SELECT * FROM tasks WHERE is_active=1")
    if not tasks:
        await update.message.reply_text("😔 Abhi koi task nahi hai. Baad mein aana."); return
    buttons = [
        [InlineKeyboardButton(f"🪙 {t['reward']} coins | {t['title']}", callback_data=f"task_info_{t['id']}")]
        for t in tasks
    ]
    await update.message.reply_text(
        "📋 *Available Tasks*\n\nEk task chuno:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

async def user_withdraw(update: Update, uid: int):
    u     = qdb("SELECT balance FROM users WHERE user_id=?", (uid,), one=True)
    min_w = get_setting("min_withdraw", float)
    if u["balance"] < min_w:
        await update.message.reply_text(
            f"❌ *Balance Kam Hai*\n\n"
            f"Tumhara balance: `{u['balance']} coins`\n"
            f"Minimum chahiye: `{min_w} coins`\n\n"
            f"Pehle tasks karo ya referrals lao!",
            parse_mode="Markdown"
        ); return
    USER_STATES[uid] = {"step": "amount"}
    await update.message.reply_text(
        f"💳 *Withdrawal Request*\n\n"
        f"Tumhara balance: `{u['balance']} coins`\n"
        f"Minimum: `{min_w} coins`\n\n"
        f"Kitna withdraw karna hai? *Amount* bhejo:",
        parse_mode="Markdown", reply_markup=cancel_kb()
    )

async def user_leaderboard(update: Update):
    top = qdb(
        "SELECT username, total_referrals, balance FROM users "
        "WHERE is_blocked=0 ORDER BY total_referrals DESC, balance DESC LIMIT 10"
    )
    if not top:
        await update.message.reply_text("Abhi koi data nahi hai."); return
    msg = "🏆 <b>Top 10 Leaderboard</b>\n\n"
    for i, t in enumerate(top, 1):
        name = (t["username"] or "Anonymous").replace("<", "&lt;").replace(">", "&gt;")
        msg += f"<b>{i}.</b> {name} — {t['total_referrals']} refs | {t['balance']} coins\n"
    await update.message.reply_text(msg, parse_mode="HTML")

async def user_profile(update: Update, uid: int):
    u  = qdb("SELECT * FROM users WHERE user_id=?", (uid,), one=True)
    td = qdb("SELECT COUNT(*) as c FROM user_tasks WHERE user_id=?", (uid,), one=True)["c"]
    await update.message.reply_text(
        f"👤 *Tumhara Profile*\n\n"
        f"🆔 ID: `{u['user_id']}`\n"
        f"📛 Username: @{u['username'] or 'N/A'}\n"
        f"📅 Join Date: {u['join_date'][:10]}\n\n"
        f"💰 Balance: *{u['balance']} coins*\n"
        f"👥 Referrals: *{u['total_referrals']}*\n"
        f"📋 Tasks Complete: *{td}*",
        parse_mode="Markdown"
    )

async def user_help(update: Update):
    await update.message.reply_text(
        "ℹ️ *Help & Guide*\n\n"
        "👤 *Profile* — Apni details dekho\n"
        "📋 *Tasks* — Tasks complete karke coins kamao\n"
        "🎁 *Daily Bonus* — Har 24 ghante free coins lo\n"
        "👥 *Referrals* — Link share karo, invite pe coins\n"
        "💰 *Balance* — Apna balance dekho\n"
        "💳 *Withdraw* — Minimum balance hone pe cashout karo\n"
        "🏆 *Leaderboard* — Top earners dekho",
        parse_mode="Markdown"
    )

# ==========================================
# MAIN MESSAGE HANDLER
# ==========================================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    text = update.message.text
    uid  = update.effective_user.id

    # ── Cancel (works everywhere) ──
    if text == "❌ Cancel":
        USER_STATES.pop(uid, None)
        ADMIN_STATES.pop(uid, None)
        if is_admin(uid) and uid not in ADMIN_USER_MODE:
            await update.message.reply_text("❌ Cancel ho gaya.", reply_markup=admin_kb())
        else:
            kb = user_mode_kb() if is_admin(uid) else main_kb()
            await update.message.reply_text("❌ Cancel ho gaya.", reply_markup=kb)
        return

    # ── Admin state flows (multi-step) ──
    if is_admin(uid) and uid in ADMIN_STATES:
        if await handle_admin_state(update, context, uid, text):
            return

    # ── User withdrawal state ──
    if uid in USER_STATES:
        if await handle_withdrawal_state(update, context, uid, text):
            return

    # ── Admin: return to admin panel from user mode ──
    if is_admin(uid) and text == "👑 Admin Panel":
        ADMIN_USER_MODE.discard(uid)
        await update.message.reply_text(
            "👑 *Admin Panel*", parse_mode="Markdown", reply_markup=admin_kb()
        )
        return

    # ── Admin menu buttons ──
    if is_admin(uid) and uid not in ADMIN_USER_MODE:
        if text == "📊 Stats":
            await admin_stats(update); return
        if text == "👥 Users":
            await send_users_page(update, context, 0, editing=False); return
        if text == "📋 Tasks":
            await admin_tasks_screen(update); return
        if text == "📢 Channels":
            await admin_channels_screen(update); return
        if text == "💳 Withdrawals":
            await admin_withdrawals_screen(update); return
        if text == "📣 Broadcast":
            await admin_broadcast_prompt(update, uid); return
        if text == "⚙️ Settings":
            await admin_settings_screen(update); return
        if text == "🔙 User View":
            ADMIN_USER_MODE.add(uid)
            await update.message.reply_text(
                "🔙 User view mein aa gaye.\n👑 Admin Panel wapas jaane ke liye last button dabao.",
                reply_markup=user_mode_kb()
            )
            return
        # Unknown text while in admin mode — ignore or show hint
        await update.message.reply_text("⬆️ Upar se button dabao.", reply_markup=admin_kb())
        return

    # ── User menu buttons ──
    if text == "👤 Profile":
        await user_profile(update, uid)
    elif text == "📋 Tasks":
        await user_tasks(update, uid)
    elif text == "🎁 Daily Bonus":
        await user_daily(update, uid)
    elif text == "👥 Referrals":
        await user_referrals(update, context, uid)
    elif text == "💰 Balance":
        await user_balance(update, uid)
    elif text == "💳 Withdraw":
        await user_withdraw(update, uid)
    elif text == "🏆 Leaderboard":
        await user_leaderboard(update)
    elif text == "ℹ️ Help":
        await user_help(update)

# ==========================================
# MAIN
# ==========================================
def main():
    logger.info("Bot start ho raha hai — Pure Telegram button mode…")
    app = Application.builder().token(BOT_TOKEN).build()

    # Force join runs before every update
    app.add_handler(TypeHandler(Update, check_force_join), group=-1)

    # Only /start command — everything else is buttons
    app.add_handler(CommandHandler("start", start_cmd))

    # Inline keyboard callbacks
    app.add_handler(CallbackQueryHandler(handle_callback))

    # Reply keyboard text messages
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot chal raha hai. Ctrl+C se band karo.")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
