import os
import sqlite3
import asyncio
import logging
from datetime import datetime, timedelta
from functools import wraps

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes, ConversationHandler
)
from telegram.error import TelegramError

# ==========================================
# CONFIGURATION
# ==========================================
BOT_TOKEN = "8273026626:AAFIcN-Esy0ZUnFr29LSiEDlrYAcnvKqnHg"
ADMIN_IDS = [6106058051]  # Multiple admins support

DB_FILE = os.environ.get("DB_FILE", "bot_database.db")
PORT = int(os.environ.get("PORT", 8080))

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Constants for conversation states
WITHDRAW_AMOUNT, WITHDRAW_METHOD, WITHDRAW_BANK_NAME, WITHDRAW_ACCOUNT_NO, WITHDRAW_IFSC, WITHDRAW_HOLDER, WITHDRAW_UPI, WITHDRAW_CRYPTO = range(8)
ADMIN_BROADCAST, ADMIN_ADD_TASK, ADMIN_ADD_CHANNEL, ADMIN_SET_REWARD, ADMIN_SET_DAILY, ADMIN_SET_MINWITHDRAW = range(6)

# ==========================================
# DATABASE
# ==========================================
def get_db():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=20)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()
    
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        join_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        total_referrals INTEGER DEFAULT 0,
        balance REAL DEFAULT 0,
        is_blocked INTEGER DEFAULT 0,
        last_bonus TIMESTAMP
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS referrals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        referrer_id INTEGER,
        referred_id INTEGER UNIQUE,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT,
        description TEXT,
        reward REAL,
        link TEXT,
        is_active INTEGER DEFAULT 1
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS user_tasks (
        user_id INTEGER,
        task_id INTEGER,
        completed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(user_id, task_id)
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS channels (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        link TEXT,
        chat_id TEXT,
        is_active INTEGER DEFAULT 1
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS withdraw_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        amount REAL,
        method TEXT,
        details TEXT,
        status TEXT DEFAULT 'pending',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )''')
    
    # Default settings
    defaults = {
        'referral_reward': '10',
        'daily_bonus': '5',
        'min_withdraw': '100',
        'welcome_message': 'Welcome to Earn Bot! Invite friends and complete tasks to earn coins.',
        'force_join': '1'
    }
    
    for key, value in defaults.items():
        c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value))
    
    conn.commit()
    conn.close()

init_db()

# ==========================================
# HELPER FUNCTIONS
# ==========================================
def is_admin(user_id):
    return user_id in ADMIN_IDS

def get_setting(key, default=None):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = c.fetchone()
    conn.close()
    if row:
        return row['value']
    return default

def set_setting(key, value):
    conn = get_db()
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
    conn.commit()
    conn.close()

def get_main_keyboard(user_id=None):
    keyboard = [
        [KeyboardButton("👤 Profile"), KeyboardButton("📋 Tasks")],
        [KeyboardButton("🎁 Daily Bonus"), KeyboardButton("👥 Referrals")],
        [KeyboardButton("💰 Balance"), KeyboardButton("💳 Withdraw")],
        [KeyboardButton("🏆 Leaderboard"), KeyboardButton("ℹ️ Help")]
    ]
    
    if user_id and is_admin(user_id):
        keyboard.append([KeyboardButton("⚙️ Admin Panel")])
    
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_admin_keyboard():
    keyboard = [
        [KeyboardButton("📢 Broadcast"), KeyboardButton("➕ Add Task")],
        [KeyboardButton("📋 All Tasks"), KeyboardButton("➕ Add Channel")],
        [KeyboardButton("📊 Statistics"), KeyboardButton("👥 All Users")],
        [KeyboardButton("💳 Withdraw Requests"), KeyboardButton("⚙️ Settings")],
        [KeyboardButton("◀️ Back to Main")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# ==========================================
# FORCE JOIN CHECK
# ==========================================
async def check_force_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if get_setting('force_join') != '1':
        return True
    
    user_id = update.effective_user.id
    
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM channels WHERE is_active = 1")
    channels = c.fetchall()
    conn.close()
    
    if not channels:
        return True
    
    not_joined = []
    for channel in channels:
        try:
            member = await context.bot.get_chat_member(chat_id=channel['chat_id'], user_id=user_id)
            if member.status not in ['member', 'administrator', 'creator']:
                not_joined.append(channel)
        except:
            not_joined.append(channel)
    
    if not_joined:
        buttons = [[InlineKeyboardButton(ch['name'], url=ch['link'])] for ch in not_joined]
        buttons.append([InlineKeyboardButton("✅ Check Again", callback_data="check_join")])
        
        msg = "🔒 *Please join the following channels to use this bot:*"
        if update.message:
            await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(buttons), parse_mode='Markdown')
        elif update.callback_query:
            await update.callback_query.message.edit_text(msg, reply_markup=InlineKeyboardMarkup(buttons), parse_mode='Markdown')
        return False
    
    return True

# ==========================================
# BOT COMMANDS
# ==========================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    
    conn = get_db()
    c = conn.cursor()
    
    c.execute("SELECT * FROM users WHERE user_id = ?", (user.id,))
    existing = c.fetchone()
    
    if not existing:
        c.execute("INSERT INTO users (user_id, username) VALUES (?, ?)", (user.id, user.username))
        
        # Check referral
        args = context.args
        if args and args[0].isdigit():
            referrer_id = int(args[0])
            if referrer_id != user.id:
                reward = float(get_setting('referral_reward', '10'))
                c.execute("INSERT INTO referrals (referrer_id, referred_id) VALUES (?, ?)", (referrer_id, user.id))
                c.execute("UPDATE users SET balance = balance + ?, total_referrals = total_referrals + 1 WHERE user_id = ?", 
                         (reward, referrer_id))
                try:
                    await context.bot.send_message(referrer_id, f"🎉 *New Referral!* +{reward} coins", parse_mode='Markdown')
                except:
                    pass
        
        conn.commit()
    
    conn.close()
    
    welcome = get_setting('welcome_message', 'Welcome to Earn Bot!')
    await update.message.reply_text(welcome, reply_markup=get_main_keyboard(user.id), parse_mode='Markdown')

async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    await update.message.reply_text(f"💰 *Your Balance:* {row['balance']} coins", parse_mode='Markdown')

async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    user = c.fetchone()
    c.execute("SELECT COUNT(*) as count FROM user_tasks WHERE user_id = ?", (user_id,))
    tasks_done = c.fetchone()
    conn.close()
    
    msg = f"👤 *Your Profile*\n\n"
    msg += f"🆔 ID: `{user['user_id']}`\n"
    msg += f"📅 Joined: {user['join_date'][:10]}\n"
    msg += f"💰 Balance: {user['balance']} coins\n"
    msg += f"👥 Referrals: {user['total_referrals']}\n"
    msg += f"📋 Tasks Completed: {tasks_done['count']}\n"
    
    await update.message.reply_text(msg, parse_mode='Markdown')

async def referrals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT total_referrals FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    
    bot_username = context.bot.username
    link = f"https://t.me/{bot_username}?start={user_id}"
    reward = get_setting('referral_reward', '10')
    
    msg = f"👥 *Referral System*\n\n"
    msg += f"💰 Reward per invite: *{reward} coins*\n"
    msg += f"👥 Your invites: *{row['total_referrals']}*\n\n"
    msg += f"🔗 *Your Link:*\n`{link}`\n\n"
    msg += f"Share this link with friends to earn coins!"
    
    await update.message.reply_text(msg, parse_mode='Markdown')

async def daily_bonus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT last_bonus FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    
    now = datetime.now()
    
    if row and row['last_bonus']:
        last = datetime.strptime(row['last_bonus'], "%Y-%m-%d %H:%M:%S.%f")
        if now < last + timedelta(days=1):
            wait = (last + timedelta(days=1)) - now
            hours = wait.seconds // 3600
            minutes = (wait.seconds % 3600) // 60
            await update.message.reply_text(f"⏳ You already claimed today's bonus!\nCome back in *{hours}h {minutes}m*", parse_mode='Markdown')
            conn.close()
            return
    
    bonus = float(get_setting('daily_bonus', '5'))
    c.execute("UPDATE users SET balance = balance + ?, last_bonus = ? WHERE user_id = ?", 
             (bonus, now, user_id))
    conn.commit()
    conn.close()
    
    await update.message.reply_text(f"🎁 *Daily Bonus Claimed!*\n+{bonus} coins", parse_mode='Markdown')

async def tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM tasks WHERE is_active = 1")
    tasks_list = c.fetchall()
    conn.close()
    
    if not tasks_list:
        await update.message.reply_text("📋 No tasks available right now.")
        return
    
    for task in tasks_list:
        conn2 = get_db()
        c2 = conn2.cursor()
        c2.execute("SELECT * FROM user_tasks WHERE user_id = ? AND task_id = ?", (user_id, task['id']))
        done = c2.fetchone()
        conn2.close()
        
        status = "✅ COMPLETED" if done else "🟢 AVAILABLE"
        msg = f"📋 *{task['title']}*\n"
        msg += f"💰 Reward: {task['reward']} coins\n"
        msg += f"📝 {task['description']}\n"
        msg += f"Status: {status}\n"
        
        if not done:
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔗 Start Task", url=task['link']),
                InlineKeyboardButton("✅ Verify", callback_data=f"verify_{task['id']}")
            ]])
            await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=keyboard)
        else:
            await update.message.reply_text(msg, parse_mode='Markdown')

async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT username, total_referrals, balance FROM users WHERE is_blocked = 0 ORDER BY total_referrals DESC, balance DESC LIMIT 10")
    top = c.fetchall()
    conn.close()
    
    msg = "🏆 *Top 10 Users*\n\n"
    for i, user in enumerate(top, 1):
        name = user['username'] or "Anonymous"
        msg += f"{i}. {name} - {user['total_referrals']} refs | {user['balance']} coins\n"
    
    await update.message.reply_text(msg, parse_mode='Markdown')

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = "ℹ️ *How to use this bot*\n\n"
    msg += "1️⃣ *Referrals* - Get your link from Referrals menu and share\n"
    msg += "2️⃣ *Tasks* - Complete tasks to earn coins\n"
    msg += "3️⃣ *Daily Bonus* - Claim daily bonus every 24h\n"
    msg += "4️⃣ *Withdraw* - Minimum " + get_setting('min_withdraw', '100') + " coins required\n\n"
    msg += "Need help? Contact @support"
    
    await update.message.reply_text(msg, parse_mode='Markdown')

# ==========================================
# WITHDRAWAL SYSTEM
# ==========================================
async def withdraw_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
    user = c.fetchone()
    conn.close()
    
    min_withdraw = float(get_setting('min_withdraw', '100'))
    
    if user['balance'] < min_withdraw:
        await update.message.reply_text(f"❌ Minimum withdrawal is *{min_withdraw}* coins\nYour balance: *{user['balance']}* coins", parse_mode='Markdown')
        return
    
    await update.message.reply_text(f"💰 *Your balance:* {user['balance']} coins\n\nEnter amount to withdraw (min: {min_withdraw}):", parse_mode='Markdown')
    return WITHDRAW_AMOUNT

async def withdraw_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text)
        user_id = update.effective_user.id
        
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
        user = c.fetchone()
        conn.close()
        
        min_withdraw = float(get_setting('min_withdraw', '100'))
        
        if amount < min_withdraw:
            await update.message.reply_text(f"❌ Minimum withdrawal is {min_withdraw} coins")
            return WITHDRAW_AMOUNT
        
        if amount > user['balance']:
            await update.message.reply_text(f"❌ Insufficient balance! You have {user['balance']} coins")
            return WITHDRAW_AMOUNT
        
        context.user_data['withdraw_amount'] = amount
        
        keyboard = [
            [KeyboardButton("🏦 Bank Transfer")],
            [KeyboardButton("📱 UPI")],
            [KeyboardButton("💳 Crypto")],
            [KeyboardButton("❌ Cancel")]
        ]
        await update.message.reply_text("Select withdrawal method:", reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))
        return WITHDRAW_METHOD
        
    except ValueError:
        await update.message.reply_text("❌ Please enter a valid number")
        return WITHDRAW_AMOUNT

async def withdraw_method(update: Update, context: ContextTypes.DEFAULT_TYPE):
    method = update.message.text
    
    if method == "❌ Cancel":
        await update.message.reply_text("❌ Withdrawal cancelled", reply_markup=get_main_keyboard(update.effective_user.id))
        return ConversationHandler.END
    
    context.user_data['withdraw_method'] = method
    
    if method == "🏦 Bank Transfer":
        await update.message.reply_text("Enter *Bank Name:*", parse_mode='Markdown')
        return WITHDRAW_BANK_NAME
    elif method == "📱 UPI":
        await update.message.reply_text("Enter your *UPI ID:*", parse_mode='Markdown')
        return WITHDRAW_UPI
    elif method == "💳 Crypto":
        await update.message.reply_text("Enter your *Crypto Wallet Address:*\n(USDT TRC20/BEP20)", parse_mode='Markdown')
        return WITHDRAW_CRYPTO
    else:
        await update.message.reply_text("❌ Please select a valid method")
        return WITHDRAW_METHOD

async def withdraw_bank_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['bank_name'] = update.message.text
    await update.message.reply_text("Enter *Account Number:*", parse_mode='Markdown')
    return WITHDRAW_ACCOUNT_NO

async def withdraw_account_no(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['account_no'] = update.message.text
    await update.message.reply_text("Enter *IFSC Code:*", parse_mode='Markdown')
    return WITHDRAW_IFSC

async def withdraw_ifsc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['ifsc'] = update.message.text
    await update.message.reply_text("Enter *Account Holder Name:*", parse_mode='Markdown')
    return WITHDRAW_HOLDER

async def withdraw_holder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    amount = context.user_data['withdraw_amount']
    method = "Bank Transfer"
    
    details = f"Bank: {context.user_data['bank_name']}\nAccount: {context.user_data['account_no']}\nIFSC: {context.user_data['ifsc']}\nHolder: {update.message.text}"
    
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (amount, user_id))
    c.execute("INSERT INTO withdraw_requests (user_id, amount, method, details) VALUES (?, ?, ?, ?)", 
             (user_id, amount, method, details))
    conn.commit()
    conn.close()
    
    await update.message.reply_text(f"✅ *Withdrawal Request Submitted!*\nAmount: {amount} coins\n\nWill be processed within 24-48 hours.", 
                                   parse_mode='Markdown', reply_markup=get_main_keyboard(user_id))
    
    # Notify admins
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(admin_id, f"💰 *New Withdrawal Request*\nUser: {user_id}\nAmount: {amount}\nMethod: {method}", parse_mode='Markdown')
        except:
            pass
    
    return ConversationHandler.END

async def withdraw_upi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    amount = context.user_data['withdraw_amount']
    method = "UPI"
    details = f"UPI ID: {update.message.text}"
    
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (amount, user_id))
    c.execute("INSERT INTO withdraw_requests (user_id, amount, method, details) VALUES (?, ?, ?, ?)", 
             (user_id, amount, method, details))
    conn.commit()
    conn.close()
    
    await update.message.reply_text(f"✅ *Withdrawal Request Submitted!*\nAmount: {amount} coins", parse_mode='Markdown', reply_markup=get_main_keyboard(user_id))
    
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(admin_id, f"💰 *New Withdrawal Request*\nUser: {user_id}\nAmount: {amount}\nMethod: UPI\nDetails: {update.message.text}", parse_mode='Markdown')
        except:
            pass
    
    return ConversationHandler.END

async def withdraw_crypto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    amount = context.user_data['withdraw_amount']
    method = "Crypto"
    details = f"Wallet: {update.message.text}"
    
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (amount, user_id))
    c.execute("INSERT INTO withdraw_requests (user_id, amount, method, details) VALUES (?, ?, ?, ?)", 
             (user_id, amount, method, details))
    conn.commit()
    conn.close()
    
    await update.message.reply_text(f"✅ *Withdrawal Request Submitted!*\nAmount: {amount} coins", parse_mode='Markdown', reply_markup=get_main_keyboard(user_id))
    
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(admin_id, f"💰 *New Withdrawal Request*\nUser: {user_id}\nAmount: {amount}\nMethod: Crypto\nDetails: {update.message.text}", parse_mode='Markdown')
        except:
            pass
    
    return ConversationHandler.END

async def withdraw_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Withdrawal cancelled", reply_markup=get_main_keyboard(update.effective_user.id))
    return ConversationHandler.END

# ==========================================
# VERIFICATION CALLBACK
# ==========================================
async def verify_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    task_id = int(query.data.split("_")[1])
    
    conn = get_db()
    c = conn.cursor()
    
    c.execute("SELECT * FROM user_tasks WHERE user_id = ? AND task_id = ?", (user_id, task_id))
    if c.fetchone():
        await query.edit_message_text("❌ You have already completed this task!")
        conn.close()
        return
    
    c.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
    task = c.fetchone()
    
    if task:
        c.execute("INSERT INTO user_tasks (user_id, task_id) VALUES (?, ?)", (user_id, task_id))
        c.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (task['reward'], user_id))
        conn.commit()
        
        await query.edit_message_text(f"✅ *Task Verified!*\n+{task['reward']} coins added to your balance!", parse_mode='Markdown')
    
    conn.close()

async def check_join_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if await check_force_join(update, context):
        await query.message.delete()
        await context.bot.send_message(query.from_user.id, "✅ Thank you for joining! You can now use the bot.", reply_markup=get_main_keyboard(query.from_user.id))
    else:
        await query.answer("Please join all channels first!", show_alert=True)

# ==========================================
# ADMIN PANEL (IN-BOT)
# ==========================================
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("❌ You are not an admin!")
        return
    
    await update.message.reply_text("⚙️ *Admin Panel*\nSelect an option:", parse_mode='Markdown', reply_markup=get_admin_keyboard())

async def admin_broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return
    
    await update.message.reply_text("📢 *Send Broadcast Message*\n\nSend me the message you want to broadcast to ALL users.\n(Supports HTML formatting)\n\nSend /cancel to cancel.", parse_mode='Markdown')
    return ADMIN_BROADCAST

async def admin_broadcast_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "/cancel":
        await update.message.reply_text("❌ Broadcast cancelled", reply_markup=get_admin_keyboard())
        return ConversationHandler.END
    
    msg = update.message.text
    user_id = update.effective_user.id
    
    await update.message.reply_text("⏳ Sending broadcast message to all users...")
    
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT user_id FROM users WHERE is_blocked = 0")
    users = c.fetchall()
    conn.close()
    
    success = 0
    failed = 0
    
    for user in users:
        try:
            await context.bot.send_message(user['user_id'], msg, parse_mode='HTML')
            success += 1
            await asyncio.sleep(0.05)  # Avoid flood limit
        except:
            failed += 1
    
    await update.message.reply_text(f"✅ *Broadcast Complete*\n\nSent: {success}\nFailed: {failed}\n\nTotal users: {len(users)}", parse_mode='Markdown', reply_markup=get_admin_keyboard())
    
    return ConversationHandler.END

async def admin_add_task_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return
    
    await update.message.reply_text("➕ *Add New Task*\n\nSend me the task details in this format:\n\n`Title|Description|Reward|Link`\n\nExample:\n`Follow on Twitter|Follow our Twitter|15|https://twitter.com/xxx`\n\nSend /cancel to cancel.", parse_mode='Markdown')
    return ADMIN_ADD_TASK

async def admin_add_task_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "/cancel":
        await update.message.reply_text("❌ Cancelled", reply_markup=get_admin_keyboard())
        return ConversationHandler.END
    
    try:
        parts = update.message.text.split("|")
        if len(parts) != 4:
            await update.message.reply_text("❌ Invalid format! Use: `Title|Description|Reward|Link`", parse_mode='Markdown')
            return ADMIN_ADD_TASK
        
        title, description, reward, link = parts
        
        conn = get_db()
        c = conn.cursor()
        c.execute("INSERT INTO tasks (title, description, reward, link) VALUES (?, ?, ?, ?)", 
                 (title.strip(), description.strip(), float(reward), link.strip()))
        conn.commit()
        conn.close()
        
        await update.message.reply_text(f"✅ *Task Added Successfully!*\n\nTitle: {title}\nReward: {reward} coins", parse_mode='Markdown', reply_markup=get_admin_keyboard())
        
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")
        return ADMIN_ADD_TASK
    
    return ConversationHandler.END

async def admin_all_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return
    
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM tasks ORDER BY id DESC")
    tasks = c.fetchall()
    conn.close()
    
    if not tasks:
        await update.message.reply_text("📋 No tasks found!")
        return
    
    msg = "📋 *All Tasks*\n\n"
    for task in tasks:
        status = "🟢 Active" if task['is_active'] else "🔴 Disabled"
        msg += f"*ID {task['id']}:* {task['title']}\n"
        msg += f"💰 {task['reward']} coins | {status}\n"
        msg += f"🔗 {task['link']}\n\n"
    
    msg += "\nTo toggle/delete tasks, use:\n/toggle_task <id>\n/delete_task <id>"
    
    await update.message.reply_text(msg, parse_mode='Markdown')

async def admin_add_channel_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return
    
    await update.message.reply_text("➕ *Add Force-Join Channel*\n\nSend me channel details in this format:\n\n`Name|Link|Chat_ID`\n\nExample:\n`My Channel|https://t.me/mychannel|@mychannel`\n\nSend /cancel to cancel.", parse_mode='Markdown')
    return ADMIN_ADD_CHANNEL

async def admin_add_channel_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "/cancel":
        await update.message.reply_text("❌ Cancelled", reply_markup=get_admin_keyboard())
        return ConversationHandler.END
    
    try:
        parts = update.message.text.split("|")
        if len(parts) != 3:
            await update.message.reply_text("❌ Invalid format! Use: `Name|Link|Chat_ID`", parse_mode='Markdown')
            return ADMIN_ADD_CHANNEL
        
        name, link, chat_id = parts
        
        conn = get_db()
        c = conn.cursor()
        c.execute("INSERT INTO channels (name, link, chat_id) VALUES (?, ?, ?)", 
                 (name.strip(), link.strip(), chat_id.strip()))
        conn.commit()
        conn.close()
        
        await update.message.reply_text(f"✅ *Channel Added!*\n\nName: {name}\nChat ID: {chat_id}", parse_mode='Markdown', reply_markup=get_admin_keyboard())
        
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")
        return ADMIN_ADD_CHANNEL
    
    return ConversationHandler.END

async def admin_statistics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return
    
    conn = get_db()
    c = conn.cursor()
    
    c.execute("SELECT COUNT(*) as count FROM users")
    total_users = c.fetchone()['count']
    
    c.execute("SELECT COUNT(*) as count FROM users WHERE is_blocked = 1")
    blocked_users = c.fetchone()['count']
    
    c.execute("SELECT COUNT(*) as count FROM referrals")
    total_refs = c.fetchone()['count']
    
    c.execute("SELECT SUM(amount) as total FROM withdraw_requests WHERE status = 'approved'")
    total_withdrawn = c.fetchone()['total'] or 0
    
    c.execute("SELECT COUNT(*) as count FROM withdraw_requests WHERE status = 'pending'")
    pending_withdraws = c.fetchone()['count']
    
    c.execute("SELECT SUM(balance) as total FROM users")
    total_balance = c.fetchone()['total'] or 0
    
    c.execute("SELECT COUNT(*) as count FROM tasks WHERE is_active = 1")
    active_tasks = c.fetchone()['count']
    
    conn.close()
    
    msg = f"📊 *Bot Statistics*\n\n"
    msg += f"👥 Total Users: {total_users}\n"
    msg += f"🚫 Blocked Users: {blocked_users}\n"
    msg += f"👥 Total Referrals: {total_refs}\n"
    msg += f"💰 Total Balance in Bot: {total_balance:.2f} coins\n"
    msg += f"💸 Total Withdrawn: {total_withdrawn:.2f} coins\n"
    msg += f"⏳ Pending Withdrawals: {pending_withdraws}\n"
    msg += f"📋 Active Tasks: {active_tasks}\n"
    
    await update.message.reply_text(msg, parse_mode='Markdown')

async def admin_all_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return
    
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT user_id, username, balance, total_referrals, is_blocked FROM users ORDER BY balance DESC LIMIT 50")
    users = c.fetchall()
    conn.close()
    
    if not users:
        await update.message.reply_text("No users found!")
        return
    
    msg = "👥 *Top 50 Users by Balance*\n\n"
    for i, user in enumerate(users, 1):
        name = user['username'] or f"ID:{user['user_id']}"
        status = "🔴" if user['is_blocked'] else "🟢"
        msg += f"{i}. {status} {name[:20]} - 💰{user['balance']} | 👥{user['total_referrals']}\n"
    
    await update.message.reply_text(msg, parse_mode='Markdown')

async def admin_withdraw_requests(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return
    
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM withdraw_requests WHERE status = 'pending' ORDER BY created_at ASC")
    requests = c.fetchall()
    conn.close()
    
    if not requests:
        await update.message.reply_text("📭 No pending withdrawal requests!")
        return
    
    for req in requests:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Approve", callback_data=f"approve_{req['id']}"),
             InlineKeyboardButton("❌ Reject", callback_data=f"reject_{req['id']}")]
        ])
        
        msg = f"💰 *Withdrawal Request #{req['id']}*\n\n"
        msg += f"👤 User ID: `{req['user_id']}`\n"
        msg += f"💸 Amount: {req['amount']} coins\n"
        msg += f"🏦 Method: {req['method']}\n"
        msg += f"📝 Details:\n`{req['details']}`\n"
        msg += f"📅 Requested: {req['created_at']}"
        
        await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=keyboard)

async def admin_withdraw_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    action, req_id = query.data.split("_")
    req_id = int(req_id)
    
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM withdraw_requests WHERE id = ?", (req_id,))
    req = c.fetchone()
    
    if not req:
        await query.edit_message_text("❌ Request not found!")
        conn.close()
        return
    
    if action == "approve":
        c.execute("UPDATE withdraw_requests SET status = 'approved' WHERE id = ?", (req_id,))
        msg = f"✅ *Withdrawal Approved!*\n\nAmount: {req['amount']} coins\n\nYour withdrawal has been processed successfully!"
        
    else:  # reject
        c.execute("UPDATE withdraw_requests SET status = 'rejected' WHERE id = ?", (req_id,))
        c.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (req['amount'], req['user_id']))
        msg = f"❌ *Withdrawal Rejected!*\n\nAmount: {req['amount']} coins\n\nYour withdrawal was rejected. Amount has been refunded to your balance."
    
    conn.commit()
    conn.close()
    
    await query.edit_message_text(f"✅ Request #{req_id} {action}ed!")
    
    # Notify user
    try:
        await context.bot.send_message(req['user_id'], msg, parse_mode='Markdown')
    except:
        pass

async def admin_settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return
    
    reward = get_setting('referral_reward', '10')
    daily = get_setting('daily_bonus', '5')
    min_w = get_setting('min_withdraw', '100')
    force_join = "ON" if get_setting('force_join') == '1' else "OFF"
    
    keyboard = [
        [InlineKeyboardButton(f"💰 Referral Reward: {reward}", callback_data="set_reward")],
        [InlineKeyboardButton(f"🎁 Daily Bonus: {daily}", callback_data="set_daily")],
        [InlineKeyboardButton(f"💳 Min Withdraw: {min_w}", callback_data="set_minwithdraw")],
        [InlineKeyboardButton(f"🔒 Force Join: {force_join}", callback_data="toggle_forcejoin")],
        [InlineKeyboardButton("◀️ Back", callback_data="admin_back")]
    ]
    
    await update.message.reply_text("⚙️ *Bot Settings*\n\nClick on any setting to change it:", parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))

async def admin_set_reward_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("💰 *Set Referral Reward*\n\nEnter the amount (in coins) users will get per referral:\n\nSend /cancel to cancel.", parse_mode='Markdown')
    return ADMIN_SET_REWARD

async def admin_set_reward_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "/cancel":
        await update.message.reply_text("❌ Cancelled", reply_markup=get_admin_keyboard())
        return ConversationHandler.END
    
    try:
        value = float(update.message.text)
        set_setting('referral_reward', str(value))
        await update.message.reply_text(f"✅ Referral reward set to *{value}* coins!", parse_mode='Markdown', reply_markup=get_admin_keyboard())
    except:
        await update.message.reply_text("❌ Please enter a valid number!")
        return ADMIN_SET_REWARD
    
    return ConversationHandler.END

async def admin_set_daily_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("🎁 *Set Daily Bonus*\n\nEnter the amount (in coins) for daily bonus:\n\nSend /cancel to cancel.", parse_mode='Markdown')
    return ADMIN_SET_DAILY

async def admin_set_daily_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "/cancel":
        await update.message.reply_text("❌ Cancelled", reply_markup=get_admin_keyboard())
        return ConversationHandler.END
    
    try:
        value = float(update.message.text)
        set_setting('daily_bonus', str(value))
        await update.message.reply_text(f"✅ Daily bonus set to *{value}* coins!", parse_mode='Markdown', reply_markup=get_admin_keyboard())
    except:
        await update.message.reply_text("❌ Please enter a valid number!")
        return ADMIN_SET_DAILY
    
    return ConversationHandler.END

async def admin_set_minwithdraw_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("💳 *Set Minimum Withdrawal*\n\nEnter the minimum amount (in coins) required to withdraw:\n\nSend /cancel to cancel.", parse_mode='Markdown')
    return ADMIN_SET_MINWITHDRAW

async def admin_set_minwithdraw_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "/cancel":
        await update.message.reply_text("❌ Cancelled", reply_markup=get_admin_keyboard())
        return ConversationHandler.END
    
    try:
        value = float(update.message.text)
        set_setting('min_withdraw', str(value))
        await update.message.reply_text(f"✅ Minimum withdrawal set to *{value}* coins!", parse_mode='Markdown', reply_markup=get_admin_keyboard())
    except:
        await update.message.reply_text("❌ Please enter a valid number!")
        return ADMIN_SET_MINWITHDRAW
    
    return ConversationHandler.END

async def admin_toggle_forcejoin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    current = get_setting('force_join', '1')
    new = '0' if current == '1' else '1'
    set_setting('force_join', new)
    
    status = "ON" if new == '1' else "OFF"
    await query.edit_message_text(f"✅ Force Join turned {status}!")
    await asyncio.sleep(1)
    
    # Go back to settings menu
    reward = get_setting('referral_reward', '10')
    daily = get_setting('daily_bonus', '5')
    min_w = get_setting('min_withdraw', '100')
    force_join = "ON" if get_setting('force_join') == '1' else "OFF"
    
    keyboard = [
        [InlineKeyboardButton(f"💰 Referral Reward: {reward}", callback_data="set_reward")],
        [InlineKeyboardButton(f"🎁 Daily Bonus: {daily}", callback_data="set_daily")],
        [InlineKeyboardButton(f"💳 Min Withdraw: {min_w}", callback_data="set_minwithdraw")],
        [InlineKeyboardButton(f"🔒 Force Join: {force_join}", callback_data="toggle_forcejoin")],
        [InlineKeyboardButton("◀️ Back", callback_data="admin_back")]
    ]
    
    await context.bot.send_message(query.from_user.id, "⚙️ *Bot Settings*", parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))

async def admin_back_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.delete()
    await context.bot.send_message(query.from_user.id, "⚙️ *Admin Panel*", parse_mode='Markdown', reply_markup=get_admin_keyboard())

async def admin_back_to_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await update.message.reply_text("◀️ Back to main menu", reply_markup=get_main_keyboard(user_id))

# ==========================================
# MAIN
# ==========================================
async def post_init(application: Application):
    logger.info("Bot started successfully!")

def main():
    # Start webhook for Render (keep-alive)
    from telegram.ext import Updater
    import threading
    
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    
    # Force join check middleware
    # Handled in message handlers
    
    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_panel))
    
    # Admin task management commands
    app.add_handler(CommandHandler("toggle_task", admin_toggle_task_command))
    app.add_handler(CommandHandler("delete_task", admin_delete_task_command))
    
    # Message handlers
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^👤 Profile$"), profile))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^💰 Balance$"), balance))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^👥 Referrals$"), referrals))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^🎁 Daily Bonus$"), daily_bonus))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^📋 Tasks$"), tasks))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^🏆 Leaderboard$"), leaderboard))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^ℹ️ Help$"), help_command))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^⚙️ Admin Panel$"), admin_panel))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^◀️ Back to Main$"), admin_back_to_main))
    
    # Admin panel handlers
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^📢 Broadcast$"), admin_broadcast_start))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^➕ Add Task$"), admin_add_task_start))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^📋 All Tasks$"), admin_all_tasks))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^➕ Add Channel$"), admin_add_channel_start))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^📊 Statistics$"), admin_statistics))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^👥 All Users$"), admin_all_users))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^💳 Withdraw Requests$"), admin_withdraw_requests))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^⚙️ Settings$"), admin_settings_menu))
    
    # Withdrawal conversation
    withdraw_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & filters.Regex("^💳 Withdraw$"), withdraw_start)],
        states={
            WITHDRAW_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_amount)],
            WITHDRAW_METHOD: [MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_method)],
            WITHDRAW_BANK_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_bank_name)],
            WITHDRAW_ACCOUNT_NO: [MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_account_no)],
            WITHDRAW_IFSC: [MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_ifsc)],
            WITHDRAW_HOLDER: [MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_holder)],
            WITHDRAW_UPI: [MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_upi)],
            WITHDRAW_CRYPTO: [MessageHandler(filters.TEXT & ~fILTERS.COMMAND, withdraw_crypto)],
        },
        fallbacks=[MessageHandler(filters.TEXT & filters.Regex("^❌ Cancel$"), withdraw_cancel)],
    )
    app.add_handler(withdraw_conv)
    
    # Admin setting conversations
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_set_reward_start, pattern="^set_reward$")],
        states={ADMIN_SET_REWARD: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_set_reward_save)]},
        fallbacks=[CommandHandler("cancel", admin_back_to_main)],
    ))
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_set_daily_start, pattern="^set_daily$")],
        states={ADMIN_SET_DAILY: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_set_daily_save)]},
        fallbacks=[CommandHandler("cancel", admin_back_to_main)],
    ))
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_set_minwithdraw_start, pattern="^set_minwithdraw$")],
        states={ADMIN_SET_MINWITHDRAW: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_set_minwithdraw_save)]},
        fallbacks=[CommandHandler("cancel", admin_back_to_main)],
    ))
    
    # Admin broadcast conversation
    app.add_handler(ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & filters.Regex("^📢 Broadcast$"), admin_broadcast_start)],
        states={ADMIN_BROADCAST: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_broadcast_send)]},
        fallbacks=[CommandHandler("cancel", admin_back_to_main)],
    ))
    
    # Admin add task conversation
    app.add_handler(ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & filters.Regex("^➕ Add Task$"), admin_add_task_start)],
        states={ADMIN_ADD_TASK: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_task_save)]},
        fallbacks=[CommandHandler("cancel", admin_back_to_main)],
    ))
    
    # Admin add channel conversation
    app.add_handler(ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & filters.Regex("^➕ Add Channel$"), admin_add_channel_start)],
        states={ADMIN_ADD_CHANNEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_channel_save)]},
        fallbacks=[CommandHandler("cancel", admin_back_to_main)],
    ))
    
    # Callback handlers
    app.add_handler(CallbackQueryHandler(verify_callback, pattern="^verify_"))
    app.add_handler(CallbackQueryHandler(check_join_callback, pattern="^check_join$"))
    app.add_handler(CallbackQueryHandler(admin_withdraw_action, pattern="^(approve|reject)_"))
    app.add_handler(CallbackQueryHandler(admin_toggle_forcejoin, pattern="^toggle_forcejoin$"))
    app.add_handler(CallbackQueryHandler(admin_back_callback, pattern="^admin_back$"))
    
    # Simple web server for Render keep-alive
    def run_web():
        from flask import Flask
        web = Flask(__name__)
        
        @web.route('/')
        def index():
            return "Bot is running!", 200
        
        @web.route('/ping')
        def ping():
            return "OK", 200
        
        web.run(host='0.0.0.0', port=PORT)
    
    threading.Thread(target=run_web, daemon=True).start()
    logger.info(f"Web server running on port {PORT}")
    
    # Start bot
    logger.info("Starting bot...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

async def admin_toggle_task_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("❌ You are not admin!")
        return
    
    if not context.args:
        await update.message.reply_text("Usage: /toggle_task <task_id>")
        return
    
    task_id = int(context.args[0])
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE tasks SET is_active = CASE WHEN is_active = 1 THEN 0 ELSE 1 END WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()
    
    await update.message.reply_text(f"✅ Task {task_id} toggled!")

async def admin_delete_task_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("❌ You are not admin!")
        return
    
    if not context.args:
        await update.message.reply_text("Usage: /delete_task <task_id>")
        return
    
    task_id = int(context.args[0])
    conn = get_db()
    c = conn.cursor()
    c.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    c.execute("DELETE FROM user_tasks WHERE task_id = ?", (task_id,))
    conn.commit()
    conn.close()
    
    await update.message.reply_text(f"✅ Task {task_id} deleted!")

if __name__ == "__main__":
    main()
