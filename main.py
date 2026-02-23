import logging
import os
import json
import requests
from bs4 import BeautifulSoup
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ConversationHandler, ContextTypes
import random
import sqlite3
import uuid
import urllib.parse

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# ---------------- DB Setup ----------------
DB_FILE = "bot_database.db"
conn = sqlite3.connect(DB_FILE, check_same_thread=False)
cursor = conn.cursor()

cursor.execute('''CREATE TABLE IF NOT EXISTS users 
                  (user_id INTEGER PRIMARY KEY, credits INTEGER DEFAULT 0)''')
cursor.execute('''CREATE TABLE IF NOT EXISTS redeem_keys 
                  (key TEXT PRIMARY KEY, credits INTEGER, used INTEGER DEFAULT 0)''')
conn.commit()

# Owner ID
OWNER_ID = 808562734

# ---------------- Dork Presets ----------------
DORK_PRESETS = {
    "Site Targeted Combo": [
        'inurl:login "id="', 'inurl:admin/login', 'inurl:signin "user"',
        'inurl:profile "password"', 'inurl:dashboard ext:php?id=',
        'intitle:"login" intext:"username" intext:"password"',
        'inurl:user.php?id=', 'inurl:member.php?id=',
        'intext:"MySQL Error" inurl:login', 'inurl:auth "username"'
    ],
    "Shopping SQLi": [
        'inurl:cart "product_id="', 'inurl:checkout "order_id="',
        'inurl:add_to_cart ext:php', 'inurl:shop "cat="',
        'inurl:product "id=" intext:"price"', 'inurl:order_details?id='
    ],
    "CC / Payment SQLi": [
        'inurl:payment "card"', 'inurl:billing "cc"',
        'inurl:checkout "credit card"', 'inurl:pay "invoice_id="',
        'inurl:transaction "id="', 'intext:"payment gateway" inurl:id='
    ],
    "Custom Builder": []
}

# ---------------- Helper Functions ----------------
def get_user_credits(user_id):
    cursor.execute("SELECT credits FROM users WHERE user_id=?", (user_id,))
    result = cursor.fetchone()
    return result[0] if result else 0

def deduct_credits(user_id, amount):
    if user_id == OWNER_ID:
        return True
    current = get_user_credits(user_id)
    if current < amount:
        return False
    new = current - amount
    cursor.execute("UPDATE users SET credits=? WHERE user_id=?", (new, user_id))
    conn.commit()
    return True

def get_related_keywords(keyword, max_results=100):
    suggestions = set()
    try:
        url = f"https://suggestqueries.google.com/complete/search?client=firefox&q={keyword}"
        resp = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'})
        data = json.loads(resp.text)
        suggestions.update(data[1])
    except: pass
    return list(suggestions)[:max_results]

def generate_dorks_from_keywords(keywords_list, num=500):
    dorks = set()
    patterns = ["inurl:php?id=", "inurl:login", "inurl:admin", "intext:password", "ext:sql", "filetype:php id="]
    for kw in keywords_list:
        for p in patterns:
            dorks.add(f"{p} {kw}")
            dorks.add(f'"{kw}" {p}')
    return list(dorks)[:num]

def generate_urls_from_dorks(dorks):
    return [f"https://www.google.com/search?q={urllib.parse.quote(dork)}" for dork in dorks]

# ---------------- Commands ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    balance = get_user_credits(user_id)

    if user_id == OWNER_ID:
        welcome_text = "Welcome Owner DIMOND bhai! 🔥\nYou have full access (unlimited credits)."
    else:
        welcome_text = "Welcome DIMOND bhai! 🔥"

    reply_keyboard = [
        ["Keyword Maker"],
        ["Dork Generator"],
        ["Deep Parser"],
        ["SQL Tester"]
    ]
    markup = ReplyKeyboardMarkup(reply_keyboard, resize_keyboard=True)

    await update.message.reply_text(
        f"{welcome_text}\n"
        f"Your balance: {balance} credits\n"
        "Owner commands: /key, /redeem\n"
        "Pick a module to start:",
        reply_markup=markup
    )

async def generate_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != OWNER_ID:
        await update.message.reply_text("Sirf owner use kar sakta hai!")
        return
    if not context.args:
        await update.message.reply_text("Format: /key 50")
        return
    try:
        credits = int(context.args[0])
        key = str(uuid.uuid4())[:12].upper()
        cursor.execute("INSERT INTO redeem_keys (key, credits) VALUES (?, ?)", (key, credits))
        conn.commit()
        await update.message.reply_text(f"Key generated: `{key}` ({credits} credits)\nUse /redeem {key}")
    except:
        await update.message.reply_text("Number daal bhai.")

async def redeem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Format: /redeem KEY123")
        return
    key = context.args[0]
    cursor.execute("SELECT credits, used FROM redeem_keys WHERE key=?", (key,))
    row = cursor.fetchone()
    if not row or row[1] == 1:
        await update.message.reply_text("Invalid ya already used key.")
        return
    credits = row[0]
    user_id = update.effective_user.id
    cursor.execute("UPDATE redeem_keys SET used=1 WHERE key=?", (key,))
    cursor.execute("INSERT OR REPLACE INTO users (user_id, credits) VALUES (?, COALESCE((SELECT credits FROM users WHERE user_id=?)+?, ?))",
                   (user_id, user_id, credits, user_id))
    conn.commit()
    await update.message.reply_text(f"Success! +{credits} credits. New balance: {get_user_credits(user_id)}")

# ---------------- File Handler (chain: keywords → dorks → urls) ----------------
async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc or not doc.file_name.endswith('.txt'):
        await update.message.reply_text("Sirf .txt file forward kar.")
        return

    file = await doc.get_file()
    file_path = f"temp_{doc.file_id}.txt"
    await file.download_to_drive(file_path)

    with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
        lines = [l.strip() for l in f if l.strip()]

    num = len(lines)
    user_id = update.effective_user.id

    file_name_lower = doc.file_name.lower()

    if "keyword" in file_name_lower or "keywords" in file_name_lower:
        if not deduct_credits(user_id, 10):
            await update.message.reply_text(f"10 credits chahiye. Balance: {get_user_credits(user_id)}")
            os.remove(file_path)
            return
        dorks = generate_dorks_from_keywords(lines, num * 2)
        d_file = f"dorks_from_keywords_{num}.txt"
        with open(d_file, 'w') as f:
            f.write("\n".join(dorks))
        await update.message.reply_document(open(d_file, 'rb'), caption=f"{len(dorks)} dorks generated. Forward this for URLs.")
        os.remove(d_file)

    elif "dork" in file_name_lower or "dorks" in file_name_lower:
        if not deduct_credits(user_id, 20):
            await update.message.reply_text(f"20 credits chahiye. Balance: {get_user_credits(user_id)}")
            os.remove(file_path)
            return
        urls = generate_urls_from_dorks(lines)
        u_file = f"parsed_urls_{num:06d}.txt"
        with open(u_file, 'w') as f:
            f.write("\n".join(urls))
        await update.message.reply_document(open(u_file, 'rb'), caption=f"🧲 {len(urls)} results — credits: Free")
        os.remove(u_file)

    else:
        await update.message.reply_text("File keywords ya dorks wali lag nahi rahi.")

    os.remove(file_path)

# ---------------- Main ----------------
def main():
    TOKEN = "8285345901:AAGPls8_BkwpaIUu08XPpfE9Dp4JRlhopVI"   # ←←← YAHAN BOTFATHER SE TOKEN PASTE KAR DE

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("key", generate_key))
    app.add_handler(CommandHandler("redeem", redeem))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_file))

    print("Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()