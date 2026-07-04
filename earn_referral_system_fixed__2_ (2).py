import os
import sqlite3
import threading
import time
import asyncio
import logging
import requests
import json
import io
from datetime import datetime, timedelta
from functools import wraps

# Flask imports
from flask import Flask, request, session, redirect, url_for, render_template_string, send_file
from jinja2 import Environment, DictLoader
from werkzeug.middleware.proxy_fix import ProxyFix  # Render HTTPS Proxy Fix

# Telegram imports
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes, TypeHandler, ApplicationHandlerStop
)
from telegram.error import TelegramError

# ==========================================
# CONFIGURATION SECTION
# ==========================================
# Naya Bot Token
BOT_TOKEN = "8273026626:AAFIcN-Esy0ZUnFr29LSiEDlrYAcnvKqnHg"

ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "admin123"
ADMIN_TELEGRAM_ID = "6106058051"

DEFAULT_REFERRAL_REWARD = 10
DEFAULT_DAILY_BONUS = 5
DEFAULT_MIN_WITHDRAW = 100

# RENDER FIX: Allow dynamic database path and PORT
DB_FILE = os.environ.get("DB_FILE", "bot_database.db")
FLASK_PORT = int(os.environ.get("PORT", 5000))

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# ==========================================
# DATABASE SYSTEM
# ==========================================
def get_db_connection():
    # timeout=20 added so it doesn't lock when bot & admin access concurrently
    conn = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=20)
    conn.row_factory = sqlite3.Row
    return conn

def query_db(query, args=(), one=False, commit=False):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(query, args)
    rv = cur.fetchall()
    if commit:
        conn.commit()
    last_id = cur.lastrowid
    conn.close()
    return last_id if commit else (rv[0] if rv else None) if one else rv

def init_db():
    queries =[
        """CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id BIGINT UNIQUE, username TEXT, 
            join_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP, total_referrals INT DEFAULT 0, 
            successful_referrals INT DEFAULT 0, balance FLOAT DEFAULT 0.0, 
            is_blocked BOOLEAN DEFAULT 0, last_bonus TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS referrals (
            id INTEGER PRIMARY KEY AUTOINCREMENT, referrer_id BIGINT, referred_id BIGINT UNIQUE, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, description TEXT, reward FLOAT, link TEXT, is_active BOOLEAN DEFAULT 1
        )""",
        """CREATE TABLE IF NOT EXISTS user_tasks (
            user_id BIGINT, task_id INTEGER, completed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, UNIQUE(user_id, task_id)
        )""",
        """CREATE TABLE IF NOT EXISTS channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT, channel_name TEXT, channel_link TEXT, channel_id TEXT, is_active BOOLEAN DEFAULT 1
        )""",
        """CREATE TABLE IF NOT EXISTS withdraw_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id BIGINT, amount FLOAT, method TEXT, details TEXT, status TEXT DEFAULT 'Pending', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY, value TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS broadcast_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT, message TEXT, status TEXT DEFAULT 'Pending'
        )""",
        """CREATE TABLE IF NOT EXISTS admin (
            username TEXT PRIMARY KEY, password TEXT
        )"""
    ]
    for q in queries: query_db(q, commit=True)

    if not query_db("SELECT * FROM admin", one=True):
        query_db("INSERT INTO admin (username, password) VALUES (?, ?)", (ADMIN_USERNAME, ADMIN_PASSWORD), commit=True)
    
    defaults = {
        'referral_reward': str(DEFAULT_REFERRAL_REWARD),
        'daily_bonus': str(DEFAULT_DAILY_BONUS),
        'min_withdraw': str(DEFAULT_MIN_WITHDRAW),
        'join_bonus': '0',
        'welcome_message': "👋 <b>Welcome to Earn Bot!</b>\n\n"
                            "Complete simple tasks, invite friends, and claim your daily bonus to earn coins — then withdraw once you hit the minimum.\n\n"
                            "Use the menu below to get started 👇",
        'force_join': "1",
        'force_join_batch_size': "6",
        'admin_panel_url': "",
        'bot_token': BOT_TOKEN
    }
    for k, v in defaults.items():
        if not query_db("SELECT * FROM settings WHERE key=?", (k,), one=True):
            query_db("INSERT INTO settings (key, value) VALUES (?, ?)", (k, v), commit=True)

    current_db_token = query_db("SELECT value FROM settings WHERE key='bot_token'", one=True)
    if current_db_token and current_db_token['value'] == "8440702378:AAFWNjRA5ry4cx3MF8WsYIPtgAj4I69xcuA":
        query_db("UPDATE settings SET value=? WHERE key='bot_token'", (BOT_TOKEN,), commit=True)

    try:
        query_db("ALTER TABLE users ADD COLUMN last_bonus TIMESTAMP", commit=True)
    except sqlite3.OperationalError:
        pass

    try:
        query_db("ALTER TABLE users ADD COLUMN join_bonus_claimed BOOLEAN DEFAULT 0", commit=True)
    except sqlite3.OperationalError:
        pass

def get_setting(key, default_type=str):
    res = query_db("SELECT value FROM settings WHERE key=?", (key,), one=True)
    if res:
        if default_type == int: return int(float(res['value']))
        if default_type == float: return float(res['value'])
        return res['value']
    return default_type()

def set_setting(key, value):
    query_db("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)), commit=True)

init_db()

# ==========================================
# ADMIN PANEL EMBEDDED TEMPLATES
# ==========================================
HTML_TEMPLATES = {
    'base.html': """
    <!DOCTYPE html>
    <html lang="en" class="dark">
    <head>
        <meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Admin Dashboard</title>
        <script src="https://cdn.tailwindcss.com"></script>
        <script>tailwind.config = { darkMode: 'class', theme: { extend: { colors: { gray: { 850: '#1f2937', 900: '#111827' }}}}}</script>
        <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    </head>
    <body class="bg-gray-900 text-gray-100 font-sans antialiased flex h-screen overflow-hidden">
        {% if session.get('admin_logged_in') %}
        <aside class="w-64 bg-gray-850 flex flex-col h-full border-r border-gray-700">
            <div class="h-16 flex items-center px-6 border-b border-gray-700 font-bold text-xl text-blue-400">
                <i class="fas fa-robot mr-2"></i> Bot Admin
            </div>
            <nav class="flex-1 px-4 py-6 space-y-2 overflow-y-auto">
                <a href="/admin" class="block px-4 py-2 rounded text-gray-300 hover:bg-gray-700 hover:text-white"><i class="fas fa-tachometer-alt w-6"></i> Dashboard</a>
                <a href="/admin/users" class="block px-4 py-2 rounded text-gray-300 hover:bg-gray-700 hover:text-white"><i class="fas fa-users w-6"></i> Users</a>
                <a href="/admin/tasks" class="block px-4 py-2 rounded text-gray-300 hover:bg-gray-700 hover:text-white"><i class="fas fa-tasks w-6"></i> Tasks</a>
                <a href="/admin/channels" class="block px-4 py-2 rounded text-gray-300 hover:bg-gray-700 hover:text-white"><i class="fas fa-bullhorn w-6"></i> Channels</a>
                <a href="/admin/withdraws" class="block px-4 py-2 rounded text-gray-300 hover:bg-gray-700 hover:text-white"><i class="fas fa-wallet w-6"></i> Withdrawals</a>
                <a href="/admin/broadcast" class="block px-4 py-2 rounded text-gray-300 hover:bg-gray-700 hover:text-white"><i class="fas fa-paper-plane w-6"></i> Broadcast</a>
                <a href="/admin/settings" class="block px-4 py-2 rounded text-gray-300 hover:bg-gray-700 hover:text-white"><i class="fas fa-cogs w-6"></i> Settings</a>
                <a href="/admin/backup" class="block px-4 py-2 rounded text-gray-300 hover:bg-gray-700 hover:text-white"><i class="fas fa-database w-6"></i> Backup / Restore</a>
            </nav>
            <div class="p-4 border-t border-gray-700">
                <a href="/admin/logout" class="block px-4 py-2 bg-red-600 text-white rounded text-center hover:bg-red-700"><i class="fas fa-sign-out-alt"></i> Logout</a>
            </div>
        </aside>
        <main class="flex-1 h-full overflow-y-auto p-8 bg-gray-900">
            {% block content %}{% endblock %}
        </main>
        {% else %}
            {% block login %}{% endblock %}
        {% endif %}
    </body>
    </html>
    """,
    'login.html': """
    {% extends "base.html" %}
    {% block login %}
    <div class="flex items-center justify-center w-full h-screen bg-gray-900">
        <div class="bg-gray-800 p-8 rounded-lg shadow-xl w-96 border border-gray-700">
            <h2 class="text-2xl font-bold mb-6 text-center text-blue-400">Admin Login</h2>
            {% if error %}<p class="text-red-500 mb-4 text-center">{{ error }}</p>{% endif %}
            <form method="POST">
                <div class="mb-4"><label class="block text-gray-400 mb-2">Username</label><input type="text" name="username" class="w-full px-4 py-2 bg-gray-700 border border-gray-600 rounded text-white focus:outline-none focus:border-blue-500" required></div>
                <div class="mb-6"><label class="block text-gray-400 mb-2">Password</label><input type="password" name="password" class="w-full px-4 py-2 bg-gray-700 border border-gray-600 rounded text-white focus:outline-none focus:border-blue-500" required></div>
                <button type="submit" class="w-full bg-blue-600 hover:bg-blue-700 text-white font-bold py-2 px-4 rounded transition duration-200">Login</button>
            </form>
        </div>
    </div>
    {% endblock %}
    """,
    'dashboard.html': """
    {% extends "base.html" %}
    {% block content %}
    <h1 class="text-3xl font-bold mb-8 text-white">Dashboard Analytics</h1>
    <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6 mb-8">
        <div class="bg-gray-800 p-6 rounded-lg border border-gray-700 shadow"><div class="text-gray-400 mb-1">Total Users</div><div class="text-3xl font-bold text-blue-400">{{ stats.users }}</div></div>
        <div class="bg-gray-800 p-6 rounded-lg border border-gray-700 shadow"><div class="text-gray-400 mb-1">Total Referrals</div><div class="text-3xl font-bold text-green-400">{{ stats.referrals }}</div></div>
        <div class="bg-gray-800 p-6 rounded-lg border border-gray-700 shadow"><div class="text-gray-400 mb-1">Total Withdrawn</div><div class="text-3xl font-bold text-yellow-400">{{ stats.withdrawn }}</div></div>
        <div class="bg-gray-800 p-6 rounded-lg border border-gray-700 shadow"><div class="text-gray-400 mb-1">Pending Withdraws</div><div class="text-3xl font-bold text-red-400">{{ stats.pending_withdraws }}</div></div>
    </div>
    <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <div class="bg-gray-800 p-6 rounded-lg border border-gray-700"><h3 class="text-xl font-bold mb-4">Recent Users</h3>
            <table class="w-full text-left text-sm"><tr class="text-gray-400 border-b border-gray-700"><th>ID</th><th>Username</th><th>Balance</th></tr>
            {% for u in recent_users %} <tr class="border-b border-gray-700"> <td class="py-2">{{ u.user_id }}</td> <td>{{ u.username }}</td> <td>{{ u.balance }}</td> </tr> {% endfor %}
            </table>
        </div>
    </div>
    {% endblock %}
    """,
    'users.html': """
    {% extends "base.html" %}
    {% block content %}
    <h1 class="text-3xl font-bold mb-6">Manage Users</h1>
    <div class="bg-gray-800 rounded-lg p-6 border border-gray-700 overflow-x-auto">
        <table class="w-full text-left"><thead class="border-b border-gray-700 text-gray-400"><tr><th class="py-3">User ID</th><th>Username</th><th>Balance</th><th>Referrals</th><th>Status</th><th>Actions</th></tr></thead>
        <tbody>
            {% for u in users %}
            <tr class="border-b border-gray-700">
                <td class="py-3">{{ u.user_id }}</td><td>{{ u.username }}</td>
                <td>
                    <form method="POST" action="/admin/users/balance" class="flex items-center gap-2">
                        <input type="hidden" name="user_id" value="{{ u.user_id }}">
                        <input type="number" step="0.01" name="balance" value="{{ u.balance }}" class="w-20 px-2 py-1 bg-gray-700 rounded text-sm text-white border border-gray-600">
                        <button type="submit" class="bg-blue-600 px-2 py-1 rounded text-xs text-white">Save</button>
                    </form>
                </td>
                <td>{{ u.total_referrals }}</td>
                <td><span class="{{ 'text-red-400' if u.is_blocked else 'text-green-400' }}">{{ 'Blocked' if u.is_blocked else 'Active' }}</span></td>
                <td>
                    <form method="POST" action="/admin/users/toggle_block">
                        <input type="hidden" name="user_id" value="{{ u.user_id }}">
                        <button type="submit" class="{{ 'bg-green-600' if u.is_blocked else 'bg-red-600' }} px-3 py-1 rounded text-xs text-white">{{ 'Unblock' if u.is_blocked else 'Block' }}</button>
                    </form>
                </td>
            </tr>
            {% endfor %}
        </tbody></table>
    </div>
    {% endblock %}
    """,
    'channels.html': """
    {% extends "base.html" %}
    {% block content %}
    <h1 class="text-3xl font-bold mb-6">Force Join Channels</h1>
    <div class="bg-gray-800 rounded-lg p-6 border border-gray-700 mb-6">
        <h2 class="text-xl font-bold mb-4">Add Channel</h2>
        <form method="POST" action="/admin/channels/add" class="grid grid-cols-1 md:grid-cols-4 gap-4">
            <div><label class="block text-sm mb-1">Name</label><input type="text" name="name" class="w-full p-2 bg-gray-700 rounded border border-gray-600" required></div>
            <div><label class="block text-sm mb-1">Link (URL)</label><input type="text" name="link" class="w-full p-2 bg-gray-700 rounded border border-gray-600" required></div>
            <div><label class="block text-sm mb-1">Channel ID / @username</label><input type="text" name="channel_id" class="w-full p-2 bg-gray-700 rounded border border-gray-600" required></div>
            <div class="flex items-end"><button type="submit" class="w-full bg-blue-600 py-2 rounded font-bold hover:bg-blue-700">Add Channel</button></div>
        </form>
        <p class="text-xs text-yellow-400 mt-2">* Bot must be an admin in the channel to verify membership.</p>
    </div>
    <div class="bg-gray-800 rounded-lg p-6 border border-gray-700 overflow-x-auto">
        <table class="w-full text-left"><thead class="border-b border-gray-700 text-gray-400"><tr><th class="py-3">Name</th><th>Link</th><th>Chat ID</th><th>Status</th><th>Actions</th></tr></thead>
        <tbody>
            {% for c in channels %}
            <tr class="border-b border-gray-700">
                <td class="py-3">{{ c.channel_name }}</td><td><a href="{{ c.channel_link }}" target="_blank" class="text-blue-400">{{ c.channel_link }}</a></td>
                <td>{{ c.channel_id }}</td>
                <td><span class="{{ 'text-green-400' if c.is_active else 'text-red-400' }}">{{ 'Active' if c.is_active else 'Disabled' }}</span></td>
                <td class="flex gap-2 py-3">
                    <form method="POST" action="/admin/channels/toggle"><input type="hidden" name="id" value="{{ c.id }}"><button type="submit" class="bg-yellow-600 px-3 py-1 rounded text-xs">Toggle</button></form>
                    <form method="POST" action="/admin/channels/delete"><input type="hidden" name="id" value="{{ c.id }}"><button type="submit" class="bg-red-600 px-3 py-1 rounded text-xs">Delete</button></form>
                </td>
            </tr>
            {% endfor %}
        </tbody></table>
    </div>
    {% endblock %}
    """,
    'tasks.html': """
    {% extends "base.html" %}
    {% block content %}
    <h1 class="text-3xl font-bold mb-6">Task Management</h1>
    <div class="bg-gray-800 rounded-lg p-6 border border-gray-700 mb-6">
        <h2 class="text-xl font-bold mb-4">Create Task</h2>
        <form method="POST" action="/admin/tasks/add" class="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div><label class="block text-sm mb-1">Title</label><input type="text" name="title" class="w-full p-2 bg-gray-700 rounded border border-gray-600" required></div>
            <div><label class="block text-sm mb-1">Reward Coins</label><input type="number" step="0.1" name="reward" class="w-full p-2 bg-gray-700 rounded border border-gray-600" required></div>
            <div class="md:col-span-2"><label class="block text-sm mb-1">Link (URL)</label><input type="url" name="link" class="w-full p-2 bg-gray-700 rounded border border-gray-600" required></div>
            <div class="md:col-span-2"><label class="block text-sm mb-1">Description</label><textarea name="description" class="w-full p-2 bg-gray-700 rounded border border-gray-600"></textarea></div>
            <div class="md:col-span-2"><button type="submit" class="bg-blue-600 px-6 py-2 rounded font-bold hover:bg-blue-700">Add Task</button></div>
        </form>
    </div>
    <div class="bg-gray-800 rounded-lg p-6 border border-gray-700 overflow-x-auto">
        <table class="w-full text-left"><thead class="border-b border-gray-700 text-gray-400"><tr><th class="py-3">Title</th><th>Reward</th><th>Link</th><th>Status</th><th>Actions</th></tr></thead>
        <tbody>
            {% for t in tasks %}
            <tr class="border-b border-gray-700">
                <td class="py-3">{{ t.title }}</td><td>{{ t.reward }}</td><td><a href="{{ t.link }}" target="_blank" class="text-blue-400">View Link</a></td>
                <td><span class="{{ 'text-green-400' if t.is_active else 'text-red-400' }}">{{ 'Active' if t.is_active else 'Disabled' }}</span></td>
                <td class="flex gap-2 py-3">
                    <form method="POST" action="/admin/tasks/toggle"><input type="hidden" name="id" value="{{ t.id }}"><button type="submit" class="bg-yellow-600 px-3 py-1 rounded text-xs">Toggle</button></form>
                    <form method="POST" action="/admin/tasks/delete"><input type="hidden" name="id" value="{{ t.id }}"><button type="submit" class="bg-red-600 px-3 py-1 rounded text-xs">Delete</button></form>
                </td>
            </tr>
            {% endfor %}
        </tbody></table>
    </div>
    {% endblock %}
    """,
    'withdraw.html': """
    {% extends "base.html" %}
    {% block content %}
    <h1 class="text-3xl font-bold mb-6">Withdraw Requests</h1>
    <div class="bg-gray-800 rounded-lg p-6 border border-gray-700 overflow-x-auto">
        <table class="w-full text-left"><thead class="border-b border-gray-700 text-gray-400"><tr><th class="py-3">ID</th><th>User ID</th><th>Amount</th><th>Method & Details</th><th>Status</th><th>Actions</th></tr></thead>
        <tbody>
            {% for w in requests %}
            <tr class="border-b border-gray-700">
                <td class="py-3">{{ w.id }}</td><td>{{ w.user_id }}</td><td class="font-bold text-yellow-400">{{ w.amount }}</td>
                <td><b>{{ w.method }}</b><br><span class="text-sm text-gray-400" style="white-space: pre-line;">{{ w.details }}</span></td>
                <td>
                    {% if w.status == 'Pending' %}<span class="text-yellow-400 bg-yellow-900 px-2 py-1 rounded text-xs">Pending</span>
                    {% elif w.status == 'Approved' %}<span class="text-green-400 bg-green-900 px-2 py-1 rounded text-xs">Approved</span>
                    {% else %}<span class="text-red-400 bg-red-900 px-2 py-1 rounded text-xs">Rejected</span>{% endif %}
                </td>
                <td class="flex gap-2 py-3">
                    {% if w.status == 'Pending' %}
                    <form method="POST" action="/admin/withdraw/action"><input type="hidden" name="id" value="{{ w.id }}"><input type="hidden" name="action" value="Approve"><button type="submit" class="bg-green-600 hover:bg-green-700 px-3 py-1 rounded text-xs text-white">Approve</button></form>
                    <form method="POST" action="/admin/withdraw/action"><input type="hidden" name="id" value="{{ w.id }}"><input type="hidden" name="action" value="Reject"><button type="submit" class="bg-red-600 hover:bg-red-700 px-3 py-1 rounded text-xs text-white">Reject</button></form>
                    {% endif %}
                </td>
            </tr>
            {% endfor %}
        </tbody></table>
    </div>
    {% endblock %}
    """,
    'settings.html': """
    {% extends "base.html" %}
    {% block content %}
    <h1 class="text-3xl font-bold mb-6">System Settings</h1>
    <div class="bg-gray-800 rounded-lg p-6 border border-gray-700">
        <form method="POST" action="/admin/settings/save" class="space-y-6">
            <div class="grid grid-cols-1 md:grid-cols-2 gap-6">
                <div><label class="block text-sm mb-2 text-gray-400">Referral Reward</label><input type="number" step="0.1" name="referral_reward" value="{{ settings.referral_reward }}" class="w-full p-3 bg-gray-700 rounded border border-gray-600 focus:border-blue-500"></div>
                <div><label class="block text-sm mb-2 text-gray-400">Daily Bonus</label><input type="number" step="0.1" name="daily_bonus" value="{{ settings.daily_bonus }}" class="w-full p-3 bg-gray-700 rounded border border-gray-600 focus:border-blue-500"></div>
                <div>
                    <label class="block text-sm mb-2 text-gray-400">Join Bonus <span class="text-green-400 font-semibold">(Channels Join Karne Par)</span></label>
                    <input type="number" step="0.1" name="join_bonus" value="{{ settings.join_bonus }}" class="w-full p-3 bg-gray-700 rounded border border-gray-600 focus:border-blue-500">
                    <p class="text-xs text-gray-500 mt-1">Jab user sabhi required channels join kare to ye coins milenge (sirf ek baar). 0 = disabled.</p>
                </div>
                <div><label class="block text-sm mb-2 text-gray-400">Minimum Withdraw</label><input type="number" step="0.1" name="min_withdraw" value="{{ settings.min_withdraw }}" class="w-full p-3 bg-gray-700 rounded border border-gray-600 focus:border-blue-500"></div>
                <div>
                    <label class="block text-sm mb-2 text-gray-400">Force Join Global Toggle</label>
                    <select name="force_join" class="w-full p-3 bg-gray-700 rounded border border-gray-600 focus:border-blue-500">
                        <option value="1" {% if settings.force_join == '1' %}selected{% endif %}>Enabled</option>
                        <option value="0" {% if settings.force_join == '0' %}selected{% endif %}>Disabled</option>
                    </select>
                </div>
                <div>
                    <label class="block text-sm mb-2 text-gray-400">Force Join Batch Size</label>
                    <input type="number" min="1" step="1" name="force_join_batch_size" value="{{ settings.force_join_batch_size }}" class="w-full p-3 bg-gray-700 rounded border border-gray-600 focus:border-blue-500">
                    <p class="text-xs text-gray-500 mt-1">How many channels to show at once when a user must join several. Remaining channels appear in the next step after "I've Joined" is tapped.</p>
                </div>
                <div>
                    <label class="block text-sm mb-2 text-gray-400">Admin Panel URL</label>
                    <input type="text" name="admin_panel_url" value="{{ settings.admin_panel_url }}" placeholder="https://yourapp.onrender.com" class="w-full p-3 bg-gray-700 rounded border border-gray-600 focus:border-blue-500">
                    <p class="text-xs text-gray-500 mt-1">Your site's public URL. Needed so the "🛠 Admin Panel" button inside the bot can open this dashboard.</p>
                </div>
                <div class="md:col-span-2"><label class="block text-sm mb-2 text-gray-400">Welcome Message</label><textarea name="welcome_message" rows="3" class="w-full p-3 bg-gray-700 rounded border border-gray-600 focus:border-blue-500">{{ settings.welcome_message }}</textarea></div>
                <div class="md:col-span-2"><label class="block text-sm mb-2 text-gray-400">Bot Token</label><input type="text" name="bot_token" value="{{ settings.bot_token }}" class="w-full p-3 bg-gray-700 rounded border border-gray-600 focus:border-blue-500"><p class="text-xs text-red-400 mt-1">If you change the token, you MUST restart the python script manually from the terminal for it to take effect.</p></div>
                <div class="md:col-span-2 pt-4 border-t border-gray-700">
                    <h3 class="text-lg font-bold mb-4">Change Admin Password</h3>
                    <div class="grid grid-cols-2 gap-4">
                        <div><input type="password" name="new_password" placeholder="New Password (leave blank to keep current)" class="w-full p-3 bg-gray-700 rounded border border-gray-600"></div>
                    </div>
                </div>
            </div>
            <button type="submit" class="bg-blue-600 hover:bg-blue-700 text-white font-bold py-3 px-8 rounded shadow-lg transition duration-200">Save All Settings</button>
        </form>
    </div>
    {% endblock %}
    """,
    'broadcast.html': """
    {% extends "base.html" %}
    {% block content %}
    <h1 class="text-3xl font-bold mb-6">Broadcast Message</h1>
    <div class="bg-gray-800 rounded-lg p-6 border border-gray-700">
        <form method="POST" action="/admin/broadcast/send" class="space-y-4">
            <div><label class="block text-sm mb-2 text-gray-400">Message to all users (HTML allowed)</label><textarea name="message" rows="6" class="w-full p-4 bg-gray-700 rounded border border-gray-600 focus:outline-none focus:border-blue-500" required placeholder="Hello everyone!"></textarea></div>
            <button type="submit" class="bg-blue-600 hover:bg-blue-700 text-white font-bold py-3 px-8 rounded shadow-lg transition duration-200">Send to All Users</button>
        </form>
    </div>
    {% endblock %}
    """,
    'backup.html': """
    {% extends "base.html" %}
    {% block content %}
    <h1 class="text-3xl font-bold mb-6">Backup / Restore Data</h1>
    {% if msg %}<div class="bg-green-800 text-green-100 p-4 rounded mb-6">{{ msg }}</div>{% endif %}
    <div class="bg-gray-800 rounded-lg p-6 border border-gray-700 mb-6">
        <h2 class="text-xl font-bold mb-2">Download Backup</h2>
        <p class="text-sm text-gray-400 mb-4">Downloads all users, referrals, tasks, channels, withdrawals and settings as one JSON file. Do this regularly, especially before redeploying (free hosting often wipes the database on redeploy).</p>
        <a href="/admin/backup/download" class="inline-block bg-blue-600 hover:bg-blue-700 text-white font-bold py-3 px-8 rounded shadow-lg">⬇️ Download Backup (.json)</a>
    </div>
    <div class="bg-gray-800 rounded-lg p-6 border border-gray-700">
        <h2 class="text-xl font-bold mb-2">Restore From Backup</h2>
        <p class="text-sm text-yellow-400 mb-4">⚠️ This will overwrite existing data with what's inside the backup file. Make sure you're uploading the right file.</p>
        <form method="POST" action="/admin/backup/restore" enctype="multipart/form-data" class="flex items-center gap-4">
            <input type="file" name="backup_file" accept="application/json" required class="text-sm">
            <button type="submit" onclick="return confirm('This will overwrite current data. Continue?');" class="bg-red-600 hover:bg-red-700 text-white font-bold py-3 px-8 rounded shadow-lg">⬆️ Restore</button>
        </form>
    </div>
    {% endblock %}
    """
}

# ==========================================
# FLASK WEB SERVER (ADMIN PANEL)
# ==========================================
app = Flask(__name__)
app.secret_key = os.urandom(24)

# RENDER FIX: Trust the reverse proxy headers so Flask works cleanly behind HTTPS on Render
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

env = Environment(loader=DictLoader(HTML_TEMPLATES))

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('admin_logged_in'):
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated_function

def render(template_name, **context):
    return env.get_template(template_name).render(session=session, **context)

def send_telegram_message(chat_id, text):
    token = get_setting('bot_token') or BOT_TOKEN
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        logger.error(f"Failed to send notification via HTTP: {e}")

# RENDER FIX: Health check endpoint so Render knows the app is running
@app.route('/ping')
def ping():
    return "OK", 200

@app.route('/')
def index():
    return redirect(url_for('admin_login'))

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        admin = query_db("SELECT * FROM admin WHERE username=? AND password=?", (username, password), one=True)
        if admin:
            session['admin_logged_in'] = True
            return redirect(url_for('admin_dashboard'))
        return render('login.html', error="Invalid Credentials")
    return render('login.html')

@app.route('/admin/logout')
def admin_logout():
    session.clear()
    return redirect(url_for('admin_login'))

@app.route('/admin')
@login_required
def admin_dashboard():
    stats = {
        'users': query_db("SELECT COUNT(*) as c FROM users", one=True)['c'],
        'referrals': query_db("SELECT COUNT(*) as c FROM referrals", one=True)['c'],
        'withdrawn': query_db("SELECT SUM(amount) as c FROM withdraw_requests WHERE status='Approved'", one=True)['c'] or 0,
        'pending_withdraws': query_db("SELECT COUNT(*) as c FROM withdraw_requests WHERE status='Pending'", one=True)['c']
    }
    recent_users = query_db("SELECT * FROM users ORDER BY join_date DESC LIMIT 5")
    return render('dashboard.html', stats=stats, recent_users=recent_users)

@app.route('/admin/users')
@login_required
def admin_users():
    users = query_db("SELECT * FROM users ORDER BY id DESC")
    return render('users.html', users=users)

@app.route('/admin/users/balance', methods=['POST'])
@login_required
def admin_edit_balance():
    query_db("UPDATE users SET balance=? WHERE user_id=?", (request.form['balance'], request.form['user_id']), commit=True)
    return redirect(url_for('admin_users'))

@app.route('/admin/users/toggle_block', methods=['POST'])
@login_required
def admin_toggle_block():
    query_db("UPDATE users SET is_blocked = CASE WHEN is_blocked=1 THEN 0 ELSE 1 END WHERE user_id=?", (request.form['user_id'],), commit=True)
    return redirect(url_for('admin_users'))

@app.route('/admin/channels', methods=['GET'])
@login_required
def admin_channels():
    channels = query_db("SELECT * FROM channels ORDER BY id DESC")
    return render('channels.html', channels=channels)

@app.route('/admin/channels/add', methods=['POST'])
@login_required
def admin_add_channel():
    query_db("INSERT INTO channels (channel_name, channel_link, channel_id) VALUES (?, ?, ?)", 
             (request.form['name'], request.form['link'], request.form['channel_id']), commit=True)
    # ✅ FIX: Naya channel add hua — sab users ka verified cache reset karo
    # Taaki sab users ko naya channel bhi join karna pade
    FORCE_JOIN_VERIFIED_USERS.clear()
    return redirect(url_for('admin_channels'))

@app.route('/admin/channels/toggle', methods=['POST'])
@login_required
def admin_toggle_channel():
    query_db("UPDATE channels SET is_active = CASE WHEN is_active=1 THEN 0 ELSE 1 END WHERE id=?", (request.form['id'],), commit=True)
    # ✅ FIX: Channel status change hua — verified cache reset karo
    FORCE_JOIN_VERIFIED_USERS.clear()
    return redirect(url_for('admin_channels'))

@app.route('/admin/channels/delete', methods=['POST'])
@login_required
def admin_delete_channel():
    query_db("DELETE FROM channels WHERE id=?", (request.form['id'],), commit=True)
    # ✅ FIX: Channel delete hua — verified cache reset karo
    FORCE_JOIN_VERIFIED_USERS.clear()
    return redirect(url_for('admin_channels'))

@app.route('/admin/tasks', methods=['GET'])
@login_required
def admin_tasks():
    tasks = query_db("SELECT * FROM tasks ORDER BY id DESC")
    return render('tasks.html', tasks=tasks)

@app.route('/admin/tasks/add', methods=['POST'])
@login_required
def admin_add_task():
    query_db("INSERT INTO tasks (title, reward, link, description) VALUES (?, ?, ?, ?)", 
             (request.form['title'], request.form['reward'], request.form['link'], request.form['description']), commit=True)
    return redirect(url_for('admin_tasks'))

@app.route('/admin/tasks/toggle', methods=['POST'])
@login_required
def admin_toggle_task():
    query_db("UPDATE tasks SET is_active = CASE WHEN is_active=1 THEN 0 ELSE 1 END WHERE id=?", (request.form['id'],), commit=True)
    return redirect(url_for('admin_tasks'))

@app.route('/admin/tasks/delete', methods=['POST'])
@login_required
def admin_delete_task():
    query_db("DELETE FROM tasks WHERE id=?", (request.form['id'],), commit=True)
    return redirect(url_for('admin_tasks'))

@app.route('/admin/withdraws', methods=['GET'])
@login_required
def admin_withdraws():
    reqs = query_db("SELECT * FROM withdraw_requests ORDER BY id DESC")
    return render('withdraw.html', requests=reqs)

@app.route('/admin/withdraw/action', methods=['POST'])
@login_required
def admin_withdraw_action():
    action = request.form['action'] 
    req_id = request.form['id']
    
    w = query_db("SELECT user_id, amount FROM withdraw_requests WHERE id=?", (req_id,), one=True)
    if not w:
        return redirect(url_for('admin_withdraws'))
        
    user_id = w['user_id']
    amount = w['amount']

    final_status = 'Approved' if action == 'Approve' else 'Rejected'
    query_db("UPDATE withdraw_requests SET status=? WHERE id=?", (final_status, req_id), commit=True)
    
    if action == 'Approve':
        text = f"✅ <b>Withdrawal Approved!</b>\nYour withdrawal request of <b>{amount} coins</b> has been successfully processed."
        send_telegram_message(user_id, text)
    elif action == 'Reject':
        query_db("UPDATE users SET balance = balance + ? WHERE user_id=?", (amount, user_id), commit=True)
        text = f"❌ <b>Withdrawal Rejected!</b>\nYour withdrawal request of <b>{amount} coins</b> was declined. The coins have been refunded to your bot balance."
        send_telegram_message(user_id, text)
        
    return redirect(url_for('admin_withdraws'))

@app.route('/admin/settings', methods=['GET'])
@login_required
def admin_settings():
    rows = query_db("SELECT key, value FROM settings")
    settings = {r['key']: r['value'] for r in rows}
    return render('settings.html', settings=settings)

@app.route('/admin/settings/save', methods=['POST'])
@login_required
def admin_save_settings():
    keys =['referral_reward', 'daily_bonus', 'join_bonus', 'min_withdraw', 'welcome_message', 'force_join', 'force_join_batch_size', 'admin_panel_url', 'bot_token']
    for k in keys:
        if k in request.form:
            set_setting(k, request.form[k])
    
    new_pass = request.form.get('new_password')
    if new_pass:
        query_db("UPDATE admin SET password=? WHERE username=?", (new_pass, ADMIN_USERNAME), commit=True)
        
    return redirect(url_for('admin_settings'))

@app.route('/admin/broadcast', methods=['GET'])
@login_required
def admin_broadcast():
    return render('broadcast.html')

@app.route('/admin/broadcast/send', methods=['POST'])
@login_required
def admin_broadcast_send():
    msg = request.form['message']
    query_db("INSERT INTO broadcast_queue (message) VALUES (?)", (msg,), commit=True)
    return redirect(url_for('admin_broadcast'))

BACKUP_TABLES = ['users', 'referrals', 'tasks', 'user_tasks', 'channels', 'withdraw_requests', 'settings']

@app.route('/admin/backup', methods=['GET'])
@login_required
def admin_backup():
    return render('backup.html', msg=request.args.get('msg'))

@app.route('/admin/backup/download', methods=['GET'])
@login_required
def admin_backup_download():
    data = {}
    for t in BACKUP_TABLES:
        rows = query_db(f"SELECT * FROM {t}")
        data[t] = [dict(r) for r in rows]
    buf = io.BytesIO(json.dumps(data, indent=2, default=str).encode('utf-8'))
    filename = f"bot_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    return send_file(buf, mimetype='application/json', as_attachment=True, download_name=filename)

@app.route('/admin/backup/restore', methods=['POST'])
@login_required
def admin_backup_restore():
    f = request.files.get('backup_file')
    if not f:
        return redirect(url_for('admin_backup', msg="No file uploaded."))
    try:
        data = json.load(f)
    except Exception:
        return redirect(url_for('admin_backup', msg="Invalid JSON file."))

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        for t in BACKUP_TABLES:
            if t not in data:
                continue
            cur.execute(f"DELETE FROM {t}")
            rows = data[t]
            if not rows:
                continue
            cols = list(rows[0].keys())
            placeholders = ",".join(["?"] * len(cols))
            col_names = ",".join(cols)
            for row in rows:
                cur.execute(f"INSERT INTO {t} ({col_names}) VALUES ({placeholders})", [row[c] for c in cols])
        conn.commit()
    except Exception as e:
        conn.rollback()
        conn.close()
        return redirect(url_for('admin_backup', msg=f"Restore failed: {e}"))
    conn.close()
    return redirect(url_for('admin_backup', msg="✅ Restore completed successfully."))


def run_flask():
    # RENDER FIX: Bind to 0.0.0.0 and dynamically assigned PORT
    app.run(host='0.0.0.0', port=FLASK_PORT, debug=False, use_reloader=False)

# ==========================================
# TELEGRAM BOT SYSTEM
# ==========================================

USER_STATES = {}

# ✅ FIX: In-memory cache — jo users ek baar sab channels join kar chuke hain
# Unhe /start pe dobara channels nahi dikhenge jab tak channels change na ho
FORCE_JOIN_VERIFIED_USERS = set()

def get_main_keyboard(user_id=None):
    # YAHAN AAPKA NAYA BUTTON LAYOUT SET HAI (Ekdum Perfect)
    keyboard = [[KeyboardButton("👤 Profile"), KeyboardButton("📋 Tasks")],
        [KeyboardButton("🎁 Daily Bonus"), KeyboardButton("👥 Referrals")],
        [KeyboardButton("💰 Balance"), KeyboardButton("💳 Withdraw")],[KeyboardButton("🏆 Leaderboard"), KeyboardButton("ℹ️ Help")]
    ]
    if user_id is not None and str(user_id) == str(ADMIN_TELEGRAM_ID):
        keyboard.append([KeyboardButton("🛠 Admin Panel")])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def build_join_prompt(channels, not_joined_ids):
    """
    ✅ FIX: SAARE active channels dikhata hai — jo already join ho chuke hain
    unhe ✅ mark karke dikhata hai (HIDE nahi karta), aur jo nahi joined
    unhe join-button ke saath dikhata hai. force_join_batch_size sirf
    NOT-joined channels ki batch size control karta hai (bahut zyada channel
    hone par ek saath sab na aa jaaye) — already-joined channels hamesha
    poori list mein dikhte rehte hain, batching se unpar farak nahi padta.
    Returns: (message_text, InlineKeyboardMarkup)
    """
    pending = [c for c in channels if c['id'] in not_joined_ids]

    batch_size = get_setting('force_join_batch_size', int)
    if batch_size <= 0:
        batch_size = len(pending) if pending else 1
    current_pending = pending[:batch_size]
    current_pending_ids = {c['id'] for c in current_pending}
    remaining_after = len(pending) - len(current_pending)

    # Original order maintain: joined channels + current batch ke pending channels
    display_list = [c for c in channels if c['id'] not in not_joined_ids or c['id'] in current_pending_ids]

    row, buttons = [], []
    for c in display_list:
        # Koi mark nahi — sirf plain channel naam
        row.append(InlineKeyboardButton(c['channel_name'], url=c['channel_link']))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    # Hamesha "I've Joined" button dikhao — chahe sab join ho ya nahi
    buttons.append([InlineKeyboardButton("✅ I've Joined", callback_data="check_join")])
    markup = InlineKeyboardMarkup(buttons)

    if pending:
        msg = "🛑 *Neeche diye channels join karein, phir \"I've Joined\" dabayein.*"
        if remaining_after > 0:
            msg += f"\n\n_{remaining_after} aur channel(s) is step ke baad join karne honge._"
    else:
        msg = "📢 *Hamare channels join karein aur \"I've Joined\" dabayein continue karne ke liye.*"
    return msg, markup

async def check_force_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user: return
    user_id = update.effective_user.id
    
    u = query_db("SELECT is_blocked FROM users WHERE user_id=?", (user_id,), one=True)
    if u and u['is_blocked']:
        if update.message: await update.message.reply_text("❌ You are blocked from using this bot.")
        elif update.callback_query: await update.callback_query.answer("❌ You are blocked.", show_alert=True)
        raise TelegramError("User blocked")

    if get_setting('force_join') == '1':
        channels = query_db("SELECT * FROM channels WHERE is_active=1")
        if channels:
            # ✅ FIX: /start hamesha FRESH check karega aur SAARI channels dikhayega
            # (already-joined channels bhi) — sirf non-start interactions (normal
            # message/button click) purane verified-cache se skip hote hain, taaki
            # har click par Telegram API na maara jaaye.
            is_start = False
            if update.message and update.message.text:
                words = update.message.text.split()
                if words and words[0].split('@')[0].lower() == '/start':
                    is_start = True

            if user_id in FORCE_JOIN_VERIFIED_USERS and not is_start:
                return

            not_joined = []
            for c in channels:
                try:
                    member = await context.bot.get_chat_member(chat_id=c['channel_id'], user_id=user_id)
                    if member.status not in ['member', 'administrator', 'creator']:
                        not_joined.append(c)
                except Exception as e:
                    logger.error(f"Force join check failed for {c['channel_id']}: {e}")
                    not_joined.append(c)

            not_joined_ids = {c['id'] for c in not_joined}

            # Sab channels join hain — is_start ke hisaab se alag behavior
            if not not_joined_ids:
                if not is_start:
                    # Non-start message: cache mein add karo, join bonus do, aur through jaane do
                    FORCE_JOIN_VERIFIED_USERS.add(user_id)
                    join_bonus_amount = get_setting('join_bonus', float)
                    if join_bonus_amount > 0:
                        user_data = query_db("SELECT join_bonus_claimed FROM users WHERE user_id=?", (user_id,), one=True)
                        if user_data and not user_data['join_bonus_claimed']:
                            query_db("UPDATE users SET balance = balance + ?, join_bonus_claimed = 1 WHERE user_id=?",
                                     (join_bonus_amount, user_id), commit=True)
                            if update.message:
                                await update.message.reply_text(
                                    f"🎁 *Join Bonus!*\nSabhi channels join karne ke liye aapko *{join_bonus_amount} coins* mile!",
                                    parse_mode='Markdown'
                                )
                    return

                # /start: sab joined hain lekin phir bhi BLOCK karo — "I've Joined" click MANDATORY har baar
                FORCE_JOIN_VERIFIED_USERS.discard(user_id)
                msg, markup = build_join_prompt(channels, not_joined_ids)
                if update.message:
                    await update.message.reply_text(msg, reply_markup=markup, parse_mode='Markdown')
                raise ApplicationHandlerStop()

            # Kam se kam ek channel abhi bhi join nahi hua — agar pehle verified tha
            # (aur baad mein channel leave kar diya) to cache se hata do
            FORCE_JOIN_VERIFIED_USERS.discard(user_id)

            # ✅ FIX: poori channel list dikhao — joined channels ✅ ke saath, HIDE nahi
            msg, markup = build_join_prompt(channels, not_joined_ids)

            if update.message:
                await update.message.reply_text(msg, reply_markup=markup, parse_mode='Markdown')
            elif update.callback_query:
                if update.callback_query.data == "check_join":
                    # User clicked "I've Joined" bina join kiye — alert + buttons fir dikhao
                    await update.callback_query.answer("❌ Pehle sabhi channels join karo!", show_alert=True)
                    try:
                        await update.callback_query.message.edit_text(msg, reply_markup=markup, parse_mode='Markdown')
                    except Exception:
                        await update.callback_query.message.reply_text(msg, reply_markup=markup, parse_mode='Markdown')
                else:
                    await update.callback_query.message.reply_text(msg, reply_markup=markup, parse_mode='Markdown')
                    await update.callback_query.answer()

            raise ApplicationHandlerStop()

async def broadcast_job(context: ContextTypes.DEFAULT_TYPE):
    item = query_db("SELECT * FROM broadcast_queue WHERE status='Pending' LIMIT 1", one=True)
    if item:
        query_db("UPDATE broadcast_queue SET status='Processing' WHERE id=?", (item['id'],), commit=True)
        users = query_db("SELECT user_id FROM users WHERE is_blocked=0")
        for u in users:
            try:
                await context.bot.send_message(chat_id=u['user_id'], text=item['message'], parse_mode='HTML')
                await asyncio.sleep(0.05) 
            except:
                pass
        query_db("UPDATE broadcast_queue SET status='Done' WHERE id=?", (item['id'],), commit=True)

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    u = query_db("SELECT * FROM users WHERE user_id=?", (user.id,), one=True)
    
    if not u:
        query_db("INSERT INTO users (user_id, username) VALUES (?, ?)", (user.id, user.username), commit=True)
        
        args = context.args
        if args and args[0].isdigit():
            referrer_id = int(args[0])
            if referrer_id != user.id:
                reward = get_setting('referral_reward', float)
                query_db("INSERT INTO referrals (referrer_id, referred_id) VALUES (?, ?)", (referrer_id, user.id), commit=True)
                query_db("UPDATE users SET balance = balance + ?, total_referrals = total_referrals + 1, successful_referrals = successful_referrals + 1 WHERE user_id=?", 
                         (reward, referrer_id), commit=True)
                try:
                    await context.bot.send_message(chat_id=referrer_id, text=f"🎉 *New Referral!* You earned {reward} coins.", parse_mode='Markdown')
                except: pass

    welcome = get_setting('welcome_message')
    await update.message.reply_text(welcome, reply_markup=get_main_keyboard(user.id), parse_mode='HTML')


async def process_withdrawal(update, user_id, amount, method, details):
    query_db("UPDATE users SET balance = balance - ? WHERE user_id=?", (amount, user_id), commit=True)
    query_db("INSERT INTO withdraw_requests (user_id, amount, method, details) VALUES (?, ?, ?, ?)", 
             (user_id, amount, method, details), commit=True)
    
    if user_id in USER_STATES:
        del USER_STATES[user_id]
        
    await update.message.reply_text("✅ *Withdrawal Request Submitted Successfully!*\nIt will be reviewed by an admin.", parse_mode='Markdown', reply_markup=get_main_keyboard(user_id))


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_id = update.effective_user.id
    
    if text == "❌ Cancel":
        if user_id in USER_STATES: 
            del USER_STATES[user_id]
        await update.message.reply_text("❌ Action cancelled.", reply_markup=get_main_keyboard(user_id))
        return

    if text == "🛠 Admin Panel":
        if str(user_id) != str(ADMIN_TELEGRAM_ID):
            return  # not the admin, silently ignore
        panel_url = get_setting('admin_panel_url')
        if not panel_url:
            await update.message.reply_text(
                "⚠️ Admin panel URL not set yet.\nOpen /admin/settings on your deployed site once "
                "and set the *Admin Panel URL* field (e.g. https://yourapp.onrender.com), then this button will work.",
                parse_mode='Markdown')
            return
        panel_url = panel_url.rstrip('/')
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("🛠 Open Admin Panel", url=f"{panel_url}/admin/login")]])
        await update.message.reply_text("🛠 *Admin Panel*\nTap below to open your dashboard:", parse_mode='Markdown', reply_markup=markup)
        return

    if user_id in USER_STATES:
        state = USER_STATES[user_id]
        
        if state['step'] == 'amount':
            try:
                amount = float(text)
                min_w = get_setting('min_withdraw', float)
                user_data = query_db("SELECT balance FROM users WHERE user_id=?", (user_id,), one=True)
                
                if amount < min_w:
                    await update.message.reply_text(f"❌ Minimum withdrawal is {min_w} coins.")
                    del USER_STATES[user_id]
                    return
                if amount > user_data['balance']:
                    await update.message.reply_text("❌ Insufficient balance.")
                    del USER_STATES[user_id]
                    return
                
                USER_STATES[user_id]['amount'] = amount
                USER_STATES[user_id]['step'] = 'method'
                
                method_kb = ReplyKeyboardMarkup([[KeyboardButton("🏦 Bank Transfer"), KeyboardButton("📱 UPI")],[KeyboardButton("💳 Crypto"), KeyboardButton("❌ Cancel")]
                ], resize_keyboard=True)
                
                await update.message.reply_text("🏦 *Select Payment Method:*", parse_mode='Markdown', reply_markup=method_kb)
            except ValueError:
                await update.message.reply_text("❌ Please enter a valid number.")
            return
            
        elif state['step'] == 'method':
            if text == "🏦 Bank Transfer":
                USER_STATES[user_id]['method'] = 'Bank Transfer'
                USER_STATES[user_id]['step'] = 'bank_name'
                await update.message.reply_text("🏦 *Enter Bank Name:*", parse_mode='Markdown', reply_markup=ReplyKeyboardMarkup([["❌ Cancel"]], resize_keyboard=True))
            
            elif text == "📱 UPI":
                USER_STATES[user_id]['method'] = 'UPI'
                USER_STATES[user_id]['step'] = 'upi_id'
                await update.message.reply_text("📱 *Enter your UPI ID:*", parse_mode='Markdown', reply_markup=ReplyKeyboardMarkup([["❌ Cancel"]], resize_keyboard=True))
            
            elif text == "💳 Crypto":
                USER_STATES[user_id]['method'] = 'Crypto'
                USER_STATES[user_id]['step'] = 'crypto_address'
                await update.message.reply_text("💳 *Enter your Crypto Address (e.g. USDT TRC20):*", parse_mode='Markdown', reply_markup=ReplyKeyboardMarkup([["❌ Cancel"]], resize_keyboard=True))
            
            else:
                await update.message.reply_text("❌ Please select a valid method from the keyboard.")
            return
            
        elif state['step'] == 'bank_name':
            USER_STATES[user_id]['bank_name'] = text
            USER_STATES[user_id]['step'] = 'bank_ac_num'
            await update.message.reply_text("🔢 *Enter Account Number:*", parse_mode='Markdown')
            return
            
        elif state['step'] == 'bank_ac_num':
            USER_STATES[user_id]['bank_ac_num'] = text
            USER_STATES[user_id]['step'] = 'bank_ifsc'
            await update.message.reply_text("🔠 *Enter IFSC Code:*", parse_mode='Markdown')
            return
            
        elif state['step'] == 'bank_ifsc':
            USER_STATES[user_id]['bank_ifsc'] = text
            USER_STATES[user_id]['step'] = 'bank_holder'
            await update.message.reply_text("👤 *Enter Account Holder Name:*", parse_mode='Markdown')
            return
            
        elif state['step'] == 'bank_holder':
            USER_STATES[user_id]['bank_holder'] = text
            details = f"Bank Name: {USER_STATES[user_id]['bank_name']}\nA/C No: {USER_STATES[user_id]['bank_ac_num']}\nIFSC: {USER_STATES[user_id]['bank_ifsc']}\nHolder: {USER_STATES[user_id]['bank_holder']}"
            await process_withdrawal(update, user_id, state['amount'], "Bank Transfer", details)
            return

        elif state['step'] == 'upi_id':
            details = f"UPI ID: {text}"
            await process_withdrawal(update, user_id, state['amount'], "UPI", details)
            return
            
        elif state['step'] == 'crypto_address':
            details = f"Wallet Address: {text}"
            await process_withdrawal(update, user_id, state['amount'], "Crypto", details)
            return


    if text == "💰 Balance":
        u = query_db("SELECT balance FROM users WHERE user_id=?", (user_id,), one=True)
        await update.message.reply_text(f"💰 *Your Balance:* {u['balance']} coins", parse_mode='Markdown')
        
    elif text == "👥 Referrals":
        u = query_db("SELECT total_referrals, successful_referrals FROM users WHERE user_id=?", (user_id,), one=True)
        bot_username = context.bot.username
        link = f"https://t.me/{bot_username}?start={user_id}"
        reward = get_setting('referral_reward')
        msg = f"👥 *Referral System*\n\n" \
              f"Reward per invite: *{reward} coins*\n" \
              f"Your Total Invites: *{u['total_referrals']}*\n\n" \
              f"🔗 *Your Referral Link:*\n`{link}`\n\n" \
              f"Share this link with your friends to earn!"
        await update.message.reply_text(msg, parse_mode='Markdown')

    elif text == "🎁 Daily Bonus":
        u = query_db("SELECT last_bonus FROM users WHERE user_id=?", (user_id,), one=True)
        now = datetime.now()
        can_claim = True
        if u['last_bonus']:
            last = datetime.strptime(u['last_bonus'], "%Y-%m-%d %H:%M:%S")
            if now < last + timedelta(days=1):
                can_claim = False
                wait_time = (last + timedelta(days=1)) - now
                hours, remainder = divmod(wait_time.seconds, 3600)
                minutes, _ = divmod(remainder, 60)
                await update.message.reply_text(f"⏳ You have already claimed your daily bonus.\nPlease wait *{hours}h {minutes}m*.", parse_mode='Markdown')

        if can_claim:
            bonus = get_setting('daily_bonus', float)
            query_db("UPDATE users SET balance = balance + ?, last_bonus = ? WHERE user_id=?", 
                     (bonus, now.strftime("%Y-%m-%d %H:%M:%S"), user_id), commit=True)
            await update.message.reply_text(f"🎁 *Daily Bonus Claimed!*\nYou received *{bonus}* coins.", parse_mode='Markdown')

    elif text == "📋 Tasks":
        tasks = query_db("SELECT * FROM tasks WHERE is_active=1")
        if not tasks:
            await update.message.reply_text("There are no available tasks right now.")
            return
            
        markup =[]
        for t in tasks:
            markup.append([InlineKeyboardButton(f"🪙 {t['reward']} | {t['title']}", callback_data=f"task_info_{t['id']}")])
        await update.message.reply_text("📋 *Available Tasks*\nSelect a task below to complete it:", parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(markup))

    elif text == "💳 Withdraw":
        u = query_db("SELECT balance FROM users WHERE user_id=?", (user_id,), one=True)
        min_w = get_setting('min_withdraw', float)
        
        if u['balance'] < min_w:
            await update.message.reply_text(f"❌ *Insufficient balance.*\n\nYour balance: {u['balance']}\nMinimum withdraw: {min_w}", parse_mode='Markdown')
            return
            
        USER_STATES[user_id] = {'step': 'amount'}
        await update.message.reply_text(f"💳 *Withdrawal Process*\n\nYour balance: {u['balance']} coins\nMinimum: {min_w} coins\n\nPlease enter the *AMOUNT* you want to withdraw:", parse_mode='Markdown', reply_markup=ReplyKeyboardMarkup([["❌ Cancel"]], resize_keyboard=True))

    elif text == "🏆 Leaderboard":
        top = query_db("SELECT username, total_referrals, balance FROM users WHERE is_blocked=0 ORDER BY total_referrals DESC, balance DESC LIMIT 10")
        msg = "🏆 <b>Top 10 Users Leaderboard</b>\n\n"
        for i, t in enumerate(top, 1):
            name = t['username'] or "Unknown"
            name = name.replace('<', '&lt;').replace('>', '&gt;')
            msg += f"<b>{i}.</b> {name} - {t['total_referrals']} Refs | {t['balance']} Coins\n"
        await update.message.reply_text(msg, parse_mode='HTML')

    elif text == "👤 Profile":
        u = query_db("SELECT * FROM users WHERE user_id=?", (user_id,), one=True)
        tasks_done = query_db("SELECT COUNT(*) as c FROM user_tasks WHERE user_id=?", (user_id,), one=True)['c']
        msg = f"👤 *Your Profile*\n\n" \
              f"🆔 ID: `{u['user_id']}`\n" \
              f"📅 Joined: {u['join_date'][:10]}\n\n" \
              f"💰 Balance: *{u['balance']} coins*\n" \
              f"👥 Referrals: *{u['total_referrals']}*\n" \
              f"📋 Tasks Completed: *{tasks_done}*\n"
        await update.message.reply_text(msg, parse_mode='Markdown')

    elif text == "ℹ️ Help":
        msg = "ℹ️ *Help & Information*\n\n" \
              "1. *Referral*: Get your link from the 👥 Referrals menu. Share it to earn coins.\n" \
              "2. *Tasks*: Complete simple tasks to earn extra coins.\n" \
              "3. *Daily Bonus*: Claim free coins every 24 hours.\n" \
              "4. *Withdraw*: Once you reach the minimum amount, click 💳 Withdraw and follow the steps."
        await update.message.reply_text(msg, parse_mode='Markdown')


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user_id = query.from_user.id

    if data == "check_join":
        await query.answer("✅ Checking membership...")
        # Verify membership in all active channels
        channels = query_db("SELECT * FROM channels WHERE is_active=1")
        not_joined = []
        for c in channels:
            try:
                member = await context.bot.get_chat_member(chat_id=c['channel_id'], user_id=user_id)
                if member.status not in ['member', 'administrator', 'creator']:
                    not_joined.append(c)
            except Exception as e:
                logger.error(f"check_join verify failed for {c['channel_id']}: {e}")
                not_joined.append(c)

        not_joined_ids = {c['id'] for c in not_joined}

        if not_joined_ids:
            # Still not joined — ✅ FIX: poori channel list dikhao (joined ✅ ke
            # saath, kabhi hide nahi hote), sirf pending wale batch_size se limit hote hain
            FORCE_JOIN_VERIFIED_USERS.discard(user_id)
            msg, markup = build_join_prompt(channels, not_joined_ids)
            msg = "❌ *Abhi bhi kuch channels join nahi kiye!*\n\n" + msg

            try:
                await query.message.edit_text(msg, reply_markup=markup, parse_mode='Markdown')
            except Exception:
                await context.bot.send_message(chat_id=user_id, text=msg, reply_markup=markup, parse_mode='Markdown')
            return

        # ✅ FIX: Sab channels join ho gaye — verified cache mein add karo
        FORCE_JOIN_VERIFIED_USERS.add(user_id)

        # 🎁 JOIN BONUS: "I've Joined" press kiya aur sab channels verified — bonus do agar set hai
        join_bonus_amount = get_setting('join_bonus', float)
        bonus_text = ""
        if join_bonus_amount > 0:
            user_bonus_data = query_db("SELECT join_bonus_claimed FROM users WHERE user_id=?", (user_id,), one=True)
            if user_bonus_data and not user_bonus_data['join_bonus_claimed']:
                query_db("UPDATE users SET balance = balance + ?, join_bonus_claimed = 1 WHERE user_id=?",
                         (join_bonus_amount, user_id), commit=True)
                bonus_text = f"\n\n🎁 *Join Bonus:* Aapko *{join_bonus_amount} coins* mile sabhi channels join karne ke liye!"

        try:
            await query.message.delete()
        except Exception:
            pass
        await context.bot.send_message(
            chat_id=user_id,
            text=f"✅ *Thank you for joining!* Aap ab bot use kar sakte hain.{bonus_text}",
            reply_markup=get_main_keyboard(user_id),
            parse_mode='Markdown'
        )
        return

    if data.startswith("task_info_"):
        task_id = int(data.split("_")[2])
        task = query_db("SELECT * FROM tasks WHERE id=?", (task_id,), one=True)
        if not task:
            await query.answer("Task not found.", show_alert=True)
            return
            
        done = query_db("SELECT * FROM user_tasks WHERE user_id=? AND task_id=?", (user_id, task_id), one=True)
        if done:
            await query.answer("You already completed this task!", show_alert=True)
            return

        msg = f"📋 *{task['title']}*\n\n" \
              f"Reward: {task['reward']} coins\n" \
              f"Description: {task['description']}\n\n" \
              f"Click the button below to complete the task."
              
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔗 Open Task Link", url=task['link'])],[InlineKeyboardButton("✅ Verify & Complete", callback_data=f"task_done_{task_id}")]
        ])
        await query.message.edit_text(msg, parse_mode='Markdown', reply_markup=markup)

    elif data.startswith("task_done_"):
        task_id = int(data.split("_")[2])
        task = query_db("SELECT * FROM tasks WHERE id=?", (task_id,), one=True)
        
        done = query_db("SELECT * FROM user_tasks WHERE user_id=? AND task_id=?", (user_id, task_id), one=True)
        if done:
            await query.answer("Already completed!", show_alert=True)
            return

        query_db("INSERT INTO user_tasks (user_id, task_id) VALUES (?, ?)", (user_id, task_id), commit=True)
        query_db("UPDATE users SET balance = balance + ? WHERE user_id=?", (task['reward'], user_id), commit=True)
        
        await query.answer(f"Task completed! You earned {task['reward']} coins.", show_alert=True)
        await query.message.edit_text(f"✅ *Task Completed!*\nYou earned {task['reward']} coins.", parse_mode='Markdown')

# ==========================================
# MAIN EXECUTION
# ==========================================
def main():
    logger.info(f"Starting Flask Server in Background Thread on Port {FLASK_PORT}...")
    threading.Thread(target=run_flask, daemon=True).start()

    logger.info("Initializing Telegram Bot...")
    token = get_setting('bot_token')
    
    if not token or token.strip() == "":
        token = BOT_TOKEN
        
    application = Application.builder().token(token).build()

    application.add_handler(TypeHandler(Update, check_force_join), group=-1)

    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(handle_callback))

    job_queue = application.job_queue
    if job_queue:
        job_queue.run_repeating(broadcast_job, interval=10, first=5)
    else:
        # This means python-telegram-bot was installed WITHOUT the job-queue extra.
        # Fix: pip install "python-telegram-bot[job-queue]"  (or add it to requirements.txt)
        # Without this, admin panel broadcasts are queued in the DB but NEVER sent.
        logger.error("JobQueue not available! Broadcasts will NOT be sent. "
                     "Install with: pip install \"python-telegram-bot[job-queue]\"")

    logger.info("Bot is running...")
    application.run_polling()

if __name__ == '__main__':
    main()
