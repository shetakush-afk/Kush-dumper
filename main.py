import io
import json
import os
import re
import uuid
import random
import logging
import asyncio
import traceback
import aiohttp
from datetime import datetime, timedelta
from urllib.parse import urlparse
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder, ContextTypes, MessageHandler,
    CommandHandler, CallbackQueryHandler, filters
)

# --- CONFIG ---
TELEGRAM_TOKEN = "7545064228:AAHYqBGcXGJpK1WUp68-uuLZjMjTiPEPb2o"
OXY_ACCOUNTS = [
    ("Pika1_MhRPr", "Pika=1234pika"),
    ("Pookie_rifmR", "Pookie_12345"),
]
OXY_IDX = 0
OWNER_IDS = {7214730073, 8003049490}
DB_FILE = "users_db.json"
PARSER_THREADS = 5
GLOBAL_MAX_CONCURRENT = 10

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger("AlephBot")

# ──────────────────────────────────────────────
#  DATABASE
# ──────────────────────────────────────────────

def load_db():
    if not os.path.exists(DB_FILE):
        return {}
    with open(DB_FILE, 'r') as f:
        return json.load(f)

def save_db(db_data):
    with open(DB_FILE, 'w') as f:
        json.dump(db_data, f, indent=2)

def get_user(user_id):
    uid = str(user_id)
    if uid not in db:
        db[uid] = {"banned": False, "uses": 0, "license_expiry": None}
        save_db(db)
    u = db[uid]
    if "license_expiry" not in u:
        u["license_expiry"] = None
    if "credits" in u:
        del u["credits"]
    return u

def user_has_license(user_id):
    uid = str(user_id)
    if int(uid) in OWNER_IDS:
        return True
    u = get_user(user_id)
    expiry = u.get("license_expiry")
    if not expiry:
        return False
    return datetime.fromisoformat(expiry) > datetime.now()

def get_license_remaining(user_id):
    uid = str(user_id)
    if int(uid) in OWNER_IDS:
        return "Unlimited (Owner)"
    u = get_user(user_id)
    expiry = u.get("license_expiry")
    if not expiry:
        return None
    exp_dt = datetime.fromisoformat(expiry)
    if exp_dt <= datetime.now():
        return None
    delta = exp_dt - datetime.now()
    days = delta.days
    hours, remainder = divmod(delta.seconds, 3600)
    minutes = remainder // 60
    if days > 0:
        return f"{days}d {hours}h {minutes}m"
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"

db = load_db()
active_keys = {}
user_states = {}

LICENSE_DURATIONS = {
    "1h": ("1 Hour", timedelta(hours=1)),
    "6h": ("6 Hours", timedelta(hours=6)),
    "12h": ("12 Hours", timedelta(hours=12)),
    "1d": ("1 Day", timedelta(days=1)),
    "3d": ("3 Days", timedelta(days=3)),
    "7d": ("7 Days", timedelta(days=7)),
    "14d": ("14 Days", timedelta(days=14)),
    "30d": ("30 Days", timedelta(days=30)),
}

# ──────────────────────────────────────────────
#  VISUAL HELPERS
# ──────────────────────────────────────────────

DIV = "─────────────────────────"

def pbar(done, total, width=12):
    filled = int(width * done / total) if total else 0
    bar = "█" * filled + "░" * (width - filled)
    pct = int(100 * done / total) if total else 0
    return f"`[{bar}]` {pct}%"

def esc(text):
    for ch in r'_*[]()~`>#+-=|{}.!':
        text = text.replace(ch, f'\\{ch}')
    return text

def back_kb(target='back_menu'):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️  Back to Menu", callback_data=target)]
    ])

def main_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔤  Keyword Maker", callback_data='mode_kw')],
        [InlineKeyboardButton("🛠  Dork Generator", callback_data='mode_gen')],
        [InlineKeyboardButton("🔎  Deep Parser", callback_data='mode_parse')],
        [InlineKeyboardButton("🔑  My License", callback_data='show_license')],
        [InlineKeyboardButton("❓  Help", callback_data='show_help')],
    ])

COUNT_OPTIONS = [50, 100, 250, 500, 1000]

def count_kb(prefix):
    rows = []
    row = []
    for c in COUNT_OPTIONS:
        row.append(InlineKeyboardButton(str(c), callback_data=f"{prefix}_{c}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("✏️  Custom Number", callback_data=f"{prefix}_custom")])
    return rows

def extract_brand(site_input):
    site_input = site_input.strip().lower()
    if '/' in site_input or '.' in site_input:
        if not site_input.startswith('http'):
            site_input = 'https://' + site_input
        try:
            host = urlparse(site_input).hostname or site_input
        except Exception:
            host = site_input
        parts = host.replace('www.', '').split('.')
        return parts[0] if parts else site_input
    return site_input

# ──────────────────────────────────────────────
#  OXYLABS ROUND-ROBIN
# ──────────────────────────────────────────────

def get_oxy_auth():
    global OXY_IDX
    user, passwd = OXY_ACCOUNTS[OXY_IDX % len(OXY_ACCOUNTS)]
    OXY_IDX += 1
    return aiohttp.BasicAuth(user, passwd)

OXY_API = "https://realtime.oxylabs.io/v1/queries"

STOP_WORDS = {
    "the", "and", "for", "with", "from", "how", "what", "this", "that", "are",
    "was", "has", "have", "not", "but", "can", "all", "its", "you", "your",
    "will", "been", "would", "could", "should", "also", "more", "about", "than",
    "into", "over", "just", "like", "when", "where", "which", "their", "there",
    "then", "these", "those", "them", "they", "some", "very", "only", "most",
    "such", "each", "other", "between", "after", "before", "during", "while",
    "both", "same", "own", "our", "out", "off", "any", "few", "many", "much",
    "may", "might", "here", "who", "whom", "why", "does", "did", "had", "his",
    "her", "him", "she", "per", "via", "etc", "use", "used", "using", "new",
    "one", "two", "get", "got", "see", "now", "way", "let", "say", "top",
}

# ──────────────────────────────────────────────
#  KEYWORD MAKER — Google Deep Scrape + Web Crawl
# ──────────────────────────────────────────────

SEARCH_DORKS = [
    "{brand}", "{brand} login", "{brand} account", "{brand} password",
    "{brand} admin panel", "{brand} database", "{brand} config",
    "{brand} api", "{brand} dashboard", "{brand} leak",
    "{brand} exploit", "{brand} vulnerability", "{brand} dump",
    "{brand} premium", "{brand} free", "{brand} hack",
    "{brand} settings", "{brand} security", "{brand} backup",
    "{brand} error", "{brand} users", "{brand} email",
    "site:{brand}.com", "inurl:{brand}", "intitle:{brand}",
]

ALPHA = "abcdefghijklmnopqrstuvwxyz"

async def google_search_urls(session, query, sem):
    """Search Google via Oxylabs → return URLs + keyword data from results."""
    async with sem:
        async with global_queue.slot():
            payload = {
                "source": "google_search",
                "query": query,
                "user_agent_type": "desktop_chrome",
                "parse": True,
                "start_page": 1,
                "pages": 3,
                "limit": 30,
            }
            try:
                async with session.post(
                    OXY_API, auth=get_oxy_auth(),
                    json=payload, timeout=aiohttp.ClientTimeout(total=40),
                ) as r:
                    if r.status != 200:
                        return [], set()
                    data = await r.json()
                    urls = []
                    keywords = set()
                    for page in data.get("results", []):
                        content = page.get("content", {})
                        res = content.get("results", {})
                        for item in res.get("organic", []):
                            u = item.get("url")
                            if u:
                                urls.append(u)
                            t = item.get("title", "")
                            if t:
                                keywords.add(t.lower().strip())
                            d = item.get("desc", "")
                            if d:
                                for phrase in re.findall(r'[a-zA-Z0-9]+(?:[\s\-][a-zA-Z0-9]+){1,5}', d.lower()):
                                    keywords.add(phrase.strip())
                        related = res.get("related_searches", {})
                        if isinstance(related, dict):
                            for item in related.get("related_searches", []):
                                q = item.get("query", "")
                                if q:
                                    keywords.add(q.lower().strip())
                        elif isinstance(related, list):
                            for item in related:
                                q = item.get("query", "") if isinstance(item, dict) else str(item)
                                if q:
                                    keywords.add(q.lower().strip())
                        paa = res.get("people_also_ask", [])
                        if isinstance(paa, list):
                            for item in paa:
                                q = item.get("question", "") if isinstance(item, dict) else str(item)
                                if q:
                                    keywords.add(q.lower().strip())
                    return urls, keywords
            except Exception as e:
                logger.error("GoogleSearch err: %s", e)
                return [], set()

async def crawl_page_keywords(session, url, sem):
    """Crawl a URL via Oxylabs universal → extract keywords from HTML content."""
    async with sem:
        async with global_queue.slot():
            payload = {
                "source": "universal",
                "url": url,
                "user_agent_type": "desktop_chrome",
            }
            try:
                async with session.post(
                    OXY_API, auth=get_oxy_auth(),
                    json=payload, timeout=aiohttp.ClientTimeout(total=25),
                ) as r:
                    if r.status != 200:
                        return set()
                    data = await r.json()
                    keywords = set()
                    for page in data.get("results", []):
                        html = page.get("content", "")
                        if not html or not isinstance(html, str):
                            continue
                        for tag in ["title", "h1", "h2", "h3"]:
                            for m in re.findall(rf'<{tag}[^>]*>(.*?)</{tag}>', html, re.IGNORECASE | re.DOTALL):
                                clean = re.sub(r'<[^>]+>', '', m).strip().lower()
                                if clean and len(clean) > 3 and len(clean) < 200:
                                    keywords.add(clean)
                        for m in re.findall(r'<meta[^>]*name=["\'](?:keywords|description)["\'][^>]*content=["\'](.*?)["\']', html, re.IGNORECASE):
                            for kw in re.split(r'[,;|]', m.lower()):
                                kw = kw.strip()
                                if kw and len(kw) > 3 and len(kw) < 200:
                                    keywords.add(kw)
                        text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
                        text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
                        text = re.sub(r'<[^>]+>', ' ', text)
                        text = re.sub(r'\s+', ' ', text).lower()
                        for phrase in re.findall(r'[a-z0-9]+(?:[\s\-][a-z0-9]+){1,4}', text):
                            phrase = phrase.strip()
                            if len(phrase) > 5 and len(phrase) < 150:
                                words = phrase.split()
                                if not all(w in STOP_WORDS for w in words):
                                    keywords.add(phrase)
                    return keywords
            except Exception as e:
                logger.debug("Crawl err %s: %s", url[:60], e)
                return set()

async def generate_keywords(session, brand, max_count, status_msg, sem):
    all_kw = set()
    all_urls = set()
    brand_lower = brand.lower()

    # ── Step 1: Google search with multiple dorks ──
    try:
        await status_msg.edit_text(
            f"🔤 *Keyword Maker — Step 1: Google Search*\n{DIV}\n\n"
            f"   Brand: `{esc(brand)}`\n"
            f"   Searching Google with multiple queries\\.\\.\\.\n\n"
            f"{pbar(0, 3)}",
            parse_mode=ParseMode.MARKDOWN_V2)
    except Exception:
        pass

    queries = [d.format(brand=brand) for d in SEARCH_DORKS]
    for letter in ALPHA:
        queries.append(f"{brand} {letter}")

    tasks = [google_search_urls(session, q, sem) for q in queries]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for r in results:
        if isinstance(r, tuple):
            urls, kws = r
            all_urls.update(urls)
            for kw in kws:
                if brand_lower in kw.lower() and len(kw) > 3:
                    all_kw.add(kw)

    logger.info("KW Step1: %d URLs found, %d keywords from Google for '%s'", len(all_urls), len(all_kw), brand)

    # ── Step 2: Crawl URLs → extract keywords from pages ──
    urls_to_crawl = list(all_urls)[:200]
    if urls_to_crawl:
        try:
            await status_msg.edit_text(
                f"🔤 *Keyword Maker — Step 2: Crawling URLs*\n{DIV}\n\n"
                f"   Brand: `{esc(brand)}`\n"
                f"   URLs found: `{len(all_urls)}`\n"
                f"   Crawling: `{len(urls_to_crawl)}` pages\n"
                f"   Keywords so far: `{len(all_kw)}`\n\n"
                f"{pbar(1, 3)}",
                parse_mode=ParseMode.MARKDOWN_V2)
        except Exception:
            pass

        crawl_done = 0
        batch_size = 10
        for i in range(0, len(urls_to_crawl), batch_size):
            batch = urls_to_crawl[i:i+batch_size]
            crawl_results = await asyncio.gather(
                *[crawl_page_keywords(session, u, sem) for u in batch],
                return_exceptions=True
            )
            for cr in crawl_results:
                if isinstance(cr, set):
                    for kw in cr:
                        if brand_lower in kw.lower():
                            all_kw.add(kw)
            crawl_done += len(batch)
            if crawl_done % 30 == 0:
                try:
                    await status_msg.edit_text(
                        f"🔤 *Keyword Maker — Step 2: Crawling*\n{DIV}\n\n"
                        f"   Crawled: `{crawl_done}/{len(urls_to_crawl)}`\n"
                        f"   Keywords: `{len(all_kw)}`\n\n"
                        f"{pbar(crawl_done, len(urls_to_crawl))}",
                        parse_mode=ParseMode.MARKDOWN_V2)
                except Exception:
                    pass

        logger.info("KW Step2: crawled %d pages, total %d keywords for '%s'", crawl_done, len(all_kw), brand)

    # ── Step 3: Finalize ──
    try:
        await status_msg.edit_text(
            f"🔤 *Keyword Maker — Step 3: Finalizing*\n{DIV}\n\n"
            f"   Brand: `{esc(brand)}`\n"
            f"   Total keywords: `{len(all_kw)}`\n\n"
            f"{pbar(2, 3)}",
            parse_mode=ParseMode.MARKDOWN_V2)
    except Exception:
        pass

    kw_list = list(all_kw)
    random.shuffle(kw_list)
    return kw_list[:max_count]


# ──────────────────────────────────────────────
#  PRESET TEMPLATES
# ──────────────────────────────────────────────

PRESET_COMBO = {
    "key": "combo",
    "label": "Site Targeted Combo",
    "icon": "🎯",
    "desc": "SQLi dorks targeting login/user databases for combo dumping",
    "templates": [
        'inurl:login.php?id= "{kw}"', 'inurl:member.php?id= "{kw}"',
        'inurl:user.php?id= "{kw}"', 'inurl:profile.php?id= "{kw}"',
        'inurl:account.php?id= "{kw}"', 'inurl:index.php?id= "{kw}"',
        'inurl:view.php?id= "{kw}"', 'inurl:detail.php?id= "{kw}"',
        'inurl:page.php?id= "{kw}"', 'inurl:show.php?id= "{kw}"',
        'inurl:content.php?id= "{kw}"', 'inurl:info.php?id= "{kw}"',
        'inurl:main.php?id= "{kw}"', 'inurl:default.php?id= "{kw}"',
        'inurl:admin.php?id= "{kw}"', 'inurl:login.asp?id= "{kw}"',
        'inurl:default.asp?id= "{kw}"', 'inurl:user.aspx?id= "{kw}"',
        'inurl:users.php?id= "{kw}"', 'inurl:customer.php?id= "{kw}"',
        'inurl:signup.php?id= "{kw}"', 'inurl:register.php?id= "{kw}"',
        'inurl:auth.php?id= "{kw}"', 'inurl:accounts.php?id= "{kw}"',
        'inurl:panel.php?id= "{kw}"', 'inurl:dashboard.php?id= "{kw}"',
        'inurl:portal.php?id= "{kw}"', 'inurl:members.php?id= "{kw}"',
        'inurl:subscriber.php?id= "{kw}"', 'inurl:manage.php?id= "{kw}"',
        'inurl:.php?user_id= "{kw}"', 'inurl:.php?uid= "{kw}"',
        'inurl:.php?member_id= "{kw}"', 'inurl:.php?account_id= "{kw}"',
        'inurl:.php?login_id= "{kw}"',
    ],
}

PRESET_SHOPPING = {
    "key": "shopping",
    "label": "Shopping SQLi",
    "icon": "🛒",
    "desc": "SQLi dorks targeting e-commerce sites for order/account dumping",
    "templates": [
        'inurl:product.php?id= "{kw}"', 'inurl:item.php?id= "{kw}"',
        'inurl:shop.php?id= "{kw}"', 'inurl:store.php?id= "{kw}"',
        'inurl:buy.php?id= "{kw}"', 'inurl:cart.php?id= "{kw}"',
        'inurl:order.php?id= "{kw}"', 'inurl:checkout.php?id= "{kw}"',
        'inurl:catalog.php?id= "{kw}"', 'inurl:category.php?id= "{kw}"',
        'inurl:products.php?cat= "{kw}"', 'inurl:goods.php?id= "{kw}"',
        'inurl:productdetail.php?id= "{kw}"', 'inurl:product_detail.php?id= "{kw}"',
        'inurl:product-detail.php?id= "{kw}"', 'inurl:view_product.php?id= "{kw}"',
        'inurl:item_detail.php?id= "{kw}"', 'inurl:shopping.php?id= "{kw}"',
        'inurl:basket.php?id= "{kw}"', 'inurl:invoice.php?id= "{kw}"',
        'inurl:wishlist.php?id= "{kw}"', 'inurl:purchase.php?id= "{kw}"',
        'inurl:listing.php?id= "{kw}"', 'inurl:offer.php?id= "{kw}"',
        'inurl:deal.php?id= "{kw}"', 'inurl:price.php?id= "{kw}"',
        'inurl:.php?product_id= "{kw}"', 'inurl:.php?item_id= "{kw}"',
        'inurl:.php?cat_id= "{kw}"', 'inurl:.php?category_id= "{kw}"',
        'inurl:.php?order_id= "{kw}"', 'inurl:.php?shop_id= "{kw}"',
        'inurl:.php?pid= "{kw}"', 'inurl:.php?prod= "{kw}"',
        'inurl:.php?goods_id= "{kw}"',
    ],
}

PRESET_CC = {
    "key": "cc",
    "label": "CC / Payment SQLi",
    "icon": "💳",
    "desc": "SQLi dorks targeting payment gateways & billing systems",
    "templates": [
        'inurl:payment.php?id= "{kw}"', 'inurl:billing.php?id= "{kw}"',
        'inurl:pay.php?id= "{kw}"', 'inurl:transaction.php?id= "{kw}"',
        'inurl:checkout.php?id= "{kw}"', 'inurl:receipt.php?id= "{kw}"',
        'inurl:donate.php?id= "{kw}"', 'inurl:subscription.php?id= "{kw}"',
        'inurl:gateway.php?id= "{kw}"', 'inurl:process.php?id= "{kw}"',
        'inurl:charge.php?id= "{kw}"', 'inurl:transfer.php?id= "{kw}"',
        'inurl:wallet.php?id= "{kw}"', 'inurl:refund.php?id= "{kw}"',
        'inurl:confirm.php?id= "{kw}"', 'inurl:paymentinfo.php?id= "{kw}"',
        'inurl:payment_detail.php?id= "{kw}"', 'inurl:order_payment.php?id= "{kw}"',
        'inurl:booking.php?id= "{kw}"', 'inurl:reserve.php?id= "{kw}"',
        'inurl:plan.php?id= "{kw}"', 'inurl:invoice.php?id= "{kw}"',
        'inurl:topup.php?id= "{kw}"', 'inurl:recharge.php?id= "{kw}"',
        'inurl:deposit.php?id= "{kw}"', 'inurl:.php?payment_id= "{kw}"',
        'inurl:.php?transaction_id= "{kw}"', 'inurl:.php?billing_id= "{kw}"',
        'inurl:.php?invoice_id= "{kw}"', 'inurl:.php?receipt_id= "{kw}"',
        'inurl:.php?booking_id= "{kw}"', 'inurl:.php?order_id= "{kw}"',
        'inurl:.php?plan_id= "{kw}"', 'inurl:.php?sub_id= "{kw}"',
        'inurl:.php?pay_id= "{kw}"',
    ],
}

PRESETS = {"combo": PRESET_COMBO, "shopping": PRESET_SHOPPING, "cc": PRESET_CC}

# ──────────────────────────────────────────────
#  CUSTOM BUILDER TEMPLATES
# ──────────────────────────────────────────────

DORK_CATEGORIES = {
    "sensitive": {
        "label": "Sensitive Files", "icon": "📄",
        "templates": [
            'filetype:sql "{kw}"', 'filetype:env "{kw}"',
            'filetype:log "{kw}"', 'filetype:cfg "{kw}"',
            'filetype:bak "{kw}"', 'filetype:old "{kw}"',
            'filetype:txt "{kw}" "password"', 'filetype:csv "{kw}" "email"',
            'filetype:xls "{kw}" "password"', 'filetype:conf "{kw}"',
            'filetype:ini "{kw}"', 'extension:yml "{kw}" "password"',
        ],
    },
    "login": {
        "label": "Login Pages", "icon": "🔐",
        "templates": [
            'inurl:admin/login "{kw}"', 'inurl:admin/login.php "{kw}"',
            'inurl:admin/login.asp "{kw}"', 'inurl:user/login "{kw}"',
            'inurl:signin "{kw}"', 'inurl:wp-login.php "{kw}"',
            'intitle:"admin panel" "{kw}"', 'intitle:"login" inurl:admin "{kw}"',
            'intitle:"dashboard" inurl:login "{kw}"', 'inurl:cpanel "{kw}"',
            'inurl:webmail "{kw}"', 'intitle:"sign in" "{kw}"',
        ],
    },
    "dirs": {
        "label": "Exposed Directories", "icon": "📂",
        "templates": [
            'intitle:"index of" "{kw}"', 'intitle:"index of /" "{kw}"',
            'intitle:"index of" "parent directory" "{kw}"',
            'intitle:"Index of" ".git" "{kw}"',
            'intitle:"index of" "backup" "{kw}"', 'intitle:"index of" ".env" "{kw}"',
            'intitle:"index of" "wp-content" "{kw}"',
            'intitle:"index of" "uploads" "{kw}"',
            'intitle:"index of" "config" "{kw}"',
            'intitle:"index of" "database" "{kw}"',
            'intitle:"index of" "private" "{kw}"',
            'intitle:"index of" "secret" "{kw}"',
        ],
    },
    "db": {
        "label": "Database Leaks", "icon": "🗄",
        "templates": [
            'filetype:sql "insert into" "{kw}"', 'filetype:sql "password" "{kw}"',
            'filetype:sql "CREATE TABLE" "{kw}"', 'intext:"DB_PASSWORD" "{kw}"',
            'intext:"DB_HOST" "{kw}"', 'intext:"mysql_connect" "{kw}"',
            'filetype:env "DB_PASSWORD" "{kw}"', 'filetype:env "DATABASE_URL" "{kw}"',
            'intext:"connectionString" filetype:config "{kw}"',
            'filetype:properties "jdbc" "{kw}"',
            'intext:"pg_connect" "{kw}"', 'filetype:sql "phpMyAdmin" "{kw}"',
        ],
    },
    "cloud": {
        "label": "Cloud Storage", "icon": "☁️",
        "templates": [
            'site:s3.amazonaws.com "{kw}"', 'site:blob.core.windows.net "{kw}"',
            'site:storage.googleapis.com "{kw}"', 'site:firebaseio.com "{kw}"',
            'site:digitaloceanspaces.com "{kw}"', 'inurl:s3.amazonaws.com "{kw}"',
            'inurl:storage.cloud.google.com "{kw}"', 'site:drive.google.com "{kw}"',
            'site:docs.google.com "{kw}"', 'site:amazonaws.com filetype:pdf "{kw}"',
            'site:firebasestorage.googleapis.com "{kw}"',
            'inurl:dropbox.com/s/ "{kw}"',
        ],
    },
    "api": {
        "label": "API Keys & Secrets", "icon": "🔑",
        "templates": [
            'extension:json "api_key" "{kw}"', 'extension:json "apikey" "{kw}"',
            'extension:json "secret" "{kw}"', 'filetype:env "API_KEY" "{kw}"',
            'filetype:env "SECRET_KEY" "{kw}"', 'filetype:env "AWS_ACCESS" "{kw}"',
            'intext:"PRIVATE KEY" filetype:key "{kw}"',
            'filetype:pem "PRIVATE" "{kw}"', 'filetype:ppk "{kw}"',
            'intext:"api_secret" "{kw}"', 'filetype:json "client_secret" "{kw}"',
            'filetype:yaml "apiKey" "{kw}"',
        ],
    },
    "all": {"label": "All Types", "icon": "🌐", "templates": []},
}

SITE_TYPES = {
    "any":  {"label": "Any Site",   "icon": "🌐", "prefix": ""},
    "gov":  {"label": ".gov Sites", "icon": "🏛", "prefix": "site:*.gov "},
    "edu":  {"label": ".edu Sites", "icon": "🎓", "prefix": "site:*.edu "},
    "org":  {"label": ".org Sites", "icon": "🏢", "prefix": "site:*.org "},
    "com":  {"label": ".com Sites", "icon": "💼", "prefix": "site:*.com "},
    "mil":  {"label": ".mil Sites", "icon": "🎖", "prefix": "site:*.mil "},
}

PAGE_PARAMS = {
    "inurl":    {"label": "URL Parameters", "icon": "🔗", "extra": [
        'inurl:php?id= "{kw}"', 'inurl:asp?id= "{kw}"', 'inurl:page= "{kw}"',
        'inurl:cat= "{kw}"', 'inurl:item= "{kw}"', 'inurl:view= "{kw}"',
        'inurl:product= "{kw}"', 'inurl:file= "{kw}"', 'inurl:download= "{kw}"',
        'inurl:action= "{kw}"',
    ]},
    "filetype": {"label": "File Types", "icon": "📎", "extra": [
        'filetype:pdf "{kw}"', 'filetype:doc "{kw}"', 'filetype:docx "{kw}"',
        'filetype:ppt "{kw}"', 'filetype:xlsx "{kw}"', 'filetype:xml "{kw}"',
        'filetype:json "{kw}"', 'filetype:txt "{kw}"',
    ]},
    "intitle":  {"label": "Page Titles", "icon": "📰", "extra": [
        'intitle:"{kw}"', 'intitle:"{kw}" "admin"', 'intitle:"{kw}" "login"',
        'intitle:"{kw}" "dashboard"', 'intitle:"{kw}" "config"',
        'intitle:"{kw}" "error"', 'allintitle:"{kw}" password',
    ]},
    "intext":   {"label": "Page Content", "icon": "📝", "extra": [
        'intext:"{kw}"', 'intext:"{kw}" "password"', 'intext:"{kw}" "username"',
        'intext:"{kw}" "secret"', 'allintext:"{kw}" "confidential"',
        'allintext:"{kw}" "internal"',
    ]},
    "none":     {"label": "No Extra Params", "icon": "➖", "extra": []},
}

# ──────────────────────────────────────────────
#  DORK BUILDING
# ──────────────────────────────────────────────

def build_preset_dorks(keywords, preset_key, max_count):
    preset = PRESETS.get(preset_key)
    if not preset:
        return []
    templates = preset["templates"]
    seen = set()
    all_dorks = []
    for kw in keywords:
        for t in templates:
            dork = t.format(kw=kw)
            if dork not in seen:
                seen.add(dork)
                all_dorks.append(dork)
    if len(all_dorks) > max_count:
        random.shuffle(all_dorks)
        all_dorks = all_dorks[:max_count]
    return all_dorks

def build_custom_dorks(keywords, dork_type, site_type, page_param, max_count):
    templates = []
    if dork_type == "all":
        for key, cat in DORK_CATEGORIES.items():
            if key != "all":
                templates.extend(cat["templates"])
    else:
        templates.extend(DORK_CATEGORIES.get(dork_type, {}).get("templates", []))
    pp = PAGE_PARAMS.get(page_param, {})
    templates.extend(pp.get("extra", []))
    if not templates:
        templates = ['intitle:"index of" "{kw}"', 'filetype:sql "password" "{kw}"', 'inurl:php?id= "{kw}"']
    site_prefix = SITE_TYPES.get(site_type, {}).get("prefix", "")
    seen = set()
    all_dorks = []
    for kw in keywords:
        for t in templates:
            dork = site_prefix + t.format(kw=kw)
            if dork not in seen:
                seen.add(dork)
                all_dorks.append(dork)
    if len(all_dorks) > max_count:
        random.shuffle(all_dorks)
        all_dorks = all_dorks[:max_count]
    return all_dorks

# ──────────────────────────────────────────────
#  GLOBAL QUEUE WITH POSITION TRACKING
# ──────────────────────────────────────────────

class GlobalQueue:
    def __init__(self, max_concurrent):
        self._sem = asyncio.Semaphore(max_concurrent)
        self._waiters = []
        self._lock = asyncio.Lock()
        self.active = 0
        self.max = max_concurrent

    async def _add_waiter(self):
        event = asyncio.Event()
        async with self._lock:
            self._waiters.append(event)
            pos = len(self._waiters)
        return event, pos

    async def _remove_waiter(self, event):
        async with self._lock:
            if event in self._waiters:
                self._waiters.remove(event)

    def get_queue_length(self):
        return len(self._waiters)

    def get_position(self, event):
        try:
            return self._waiters.index(event) + 1
        except ValueError:
            return 0

    class _SlotContext:
        def __init__(self, queue):
            self.queue = queue

        async def __aenter__(self):
            await self.queue._sem.acquire()
            async with self.queue._lock:
                self.queue.active += 1
            return self

        async def __aexit__(self, *args):
            async with self.queue._lock:
                self.queue.active -= 1
                if self.queue._waiters:
                    self.queue._waiters[0].set()
            self.queue._sem.release()

    def slot(self):
        return self._SlotContext(self)

global_queue = GlobalQueue(GLOBAL_MAX_CONCURRENT)

# ──────────────────────────────────────────────
#  PER-USER SEMAPHORES
# ──────────────────────────────────────────────

user_semaphores = {}

def get_semaphore(uid):
    if uid not in user_semaphores:
        user_semaphores[uid] = asyncio.Semaphore(PARSER_THREADS)
    return user_semaphores[uid]

# ──────────────────────────────────────────────
#  GOOGLE PARSER (user sem -> global queue)
# ──────────────────────────────────────────────

async def fetch_oxylabs(session, query, sem=None):
    user_sem = sem or asyncio.Semaphore(PARSER_THREADS)
    async with user_sem:
        async with global_queue.slot():
            payload = {
                "source": "google_search", "query": query,
                "user_agent_type": "desktop_chrome", "parse": True,
                "start_page": 1, "pages": 10, "limit": 50,
            }
            logger.info("PARSER [thread] query: %.100s", query)
            try:
                async with session.post(
                    OXY_API, auth=get_oxy_auth(),
                    json=payload, timeout=aiohttp.ClientTimeout(total=90),
                ) as r:
                    body_text = await r.text()
                    if r.status != 200:
                        logger.error("PARSER HTTP %d: %.80s — %.300s", r.status, query, body_text)
                        return []
                    try:
                        data = json.loads(body_text)
                    except json.JSONDecodeError as je:
                        logger.error("PARSER JSON err: %s", je)
                        return []
                    urls = []
                    for page in data.get("results", []):
                        organic = page.get("content", {}).get("results", {}).get("organic", [])
                        for item in organic:
                            u = item.get("url")
                            if u:
                                urls.append(u)
                    logger.info("PARSER got %d URLs: %.80s", len(urls), query)
                    return urls
            except asyncio.TimeoutError:
                logger.error("PARSER timeout: %.100s", query)
                return []
            except aiohttp.ClientError as e:
                logger.error("PARSER net err: %s — %s", query, e)
                return []
            except Exception as e:
                logger.error("PARSER err: %s\n%s", e, traceback.format_exc())
                return []

# ──────────────────────────────────────────────
#  HELPERS
# ──────────────────────────────────────────────

def is_banned(uid):
    return db.get(str(uid), {}).get("banned", False)

# ──────────────────────────────────────────────
#  ANTI-PUBLIC CHECKER (Google-based, real results)
# ──────────────────────────────────────────────

ANTIPUB_THRESHOLDS = {"rare": 50000, "moderate": 500000}
ANTIPUB_CONCURRENCY = 15

async def google_check_keyword(session, keyword, sem):
    async with sem:
        async with global_queue.slot():
            payload = {
                "source": "google_search",
                "query": f'"{keyword}"',
                "user_agent_type": "desktop_chrome",
                "parse": True,
                "start_page": 1, "pages": 1, "limit": 1,
            }
            try:
                async with session.post(
                    OXY_API, auth=get_oxy_auth(),
                    json=payload, timeout=aiohttp.ClientTimeout(total=20),
                ) as r:
                    if r.status != 200:
                        return keyword, -1
                    data = await r.json()
                    for page in data.get("results", []):
                        total = page.get("content", {}).get("results", {}).get("total_results_count", 0)
                        if isinstance(total, str):
                            try:
                                total = int(total.replace(",", "").replace(".", ""))
                            except (ValueError, TypeError):
                                total = 0
                        if not isinstance(total, int):
                            total = 0
                        return keyword, total
                    return keyword, 0
            except Exception as e:
                logger.debug("AntiPub err '%s': %s", keyword[:50], e)
                return keyword, -1

# ──────────────────────────────────────────────
#  STATIC TEXTS
# ──────────────────────────────────────────────

WELCOME = (
    "🐍 *𝓿𝓮𝓷𝓸𝓶 𝓭𝓾𝓶𝓹𝓲𝓷𝓰*\n"
    f"{DIV}\n\n"
    "Welcome\\! Pick a module to get started\\.\n\n"
    "📌 *Full Pipeline:*\n"
    "  1️⃣  🔤 Keyword Maker \\→ deep scrape keywords\n"
    "  2️⃣  🛠 Dork Generator \\→ build dorks\n"
    "  3️⃣  🔎 Deep Parser \\→ get real URLs\n\n"
    "🔑 *License required for all features*\n\n"
    f"{DIV}"
)

HELP = (
    "❓ *How to Use This Bot*\n"
    f"{DIV}\n\n"
    "🔤 *Keyword Maker*  \\(License Required\\)\n"
    "  Google Deep Scrape \\+ Web Crawling\\.\n"
    "  Scrapes Google → crawls URLs → extracts keywords\\.\n"
    "  Single Site or Multi\\-Keyword bulk mode\\.\n\n"
    "🔍 *Anti\\-Public Checker*  \\(License Required\\)\n"
    "  Google checks real result counts\\.\n"
    "  Separates rare vs overused keywords\\.\n\n"
    "🛠 *Dork Generator*  \\(License Required\\)\n"
    "  3 presets \\(Combo, Shopping, CC SQLi\\)\n"
    "  \\+ Custom Builder with full control\\.\n\n"
    "🔎 *Deep Parser*  \\(License Required\\)\n"
    "  Scrapes Google for real URLs\\.\n\n"
    f"{DIV}\n"
    "🔑 *License System:*\n"
    "  Purchase a license key from the owner\\.\n"
    "  Keys are time\\-limited \\(hours/days\\)\\.\n"
    "  Use /redeem `KEY` to activate\\.\n\n"
    f"{DIV}\n"
    "📝 *Commands:*\n"
    "  /start — Main menu\n"
    "  /menu  — Back to menu\n"
    "  /help  — This guide\n"
    "  /license — Check license status\n"
    "  /redeem `KEY` — Activate license\n"
)

# ──────────────────────────────────────────────
#  COMMAND HANDLERS
# ──────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_banned(update.effective_user.id): return
    get_user(update.effective_user.id)
    await update.message.reply_text(WELCOME, reply_markup=main_menu_kb(), parse_mode=ParseMode.MARKDOWN_V2)

async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_banned(update.effective_user.id): return
    await update.message.reply_text(WELCOME, reply_markup=main_menu_kb(), parse_mode=ParseMode.MARKDOWN_V2)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_banned(update.effective_user.id): return
    await update.message.reply_text(HELP, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=back_kb())

async def cmd_license(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_banned(update.effective_user.id): return
    uid = update.effective_user.id
    remaining = get_license_remaining(uid)
    if remaining:
        status_icon = "🟢"
        status_text = "Active"
        expiry_text = f"   Time left: `{esc(remaining)}`\n"
    else:
        status_icon = "🔴"
        status_text = "Inactive"
        expiry_text = "   _No active license_\n"
    ud = get_user(uid)
    text = (
        f"🔑 *License Status*\n{DIV}\n\n"
        f"   Status: {status_icon} *{status_text}*\n"
        f"{expiry_text}"
        f"   Uses: `{ud.get('uses', 0)}`\n\n"
        f"Use /redeem `KEY` to activate a license\\."
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=back_kb())

async def cmd_redeem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_banned(update.effective_user.id): return
    if not context.args:
        await update.message.reply_text(
            "📝 *Usage:* `/redeem YOUR\\-KEY`\n\nExample: `/redeem VENOM\\-A1B2C3D4`",
            parse_mode=ParseMode.MARKDOWN_V2)
        return
    uid = str(update.effective_user.id)
    key = context.args[0].upper()
    if key in active_keys:
        key_info = active_keys.pop(key)
        duration = key_info["duration"]
        duration_label = key_info["label"]
        ud = get_user(uid)

        current_expiry = ud.get("license_expiry")
        if current_expiry:
            exp_dt = datetime.fromisoformat(current_expiry)
            if exp_dt > datetime.now():
                new_expiry = exp_dt + duration
            else:
                new_expiry = datetime.now() + duration
        else:
            new_expiry = datetime.now() + duration

        ud["license_expiry"] = new_expiry.isoformat()
        db[uid] = ud
        save_db(db)
        remaining = get_license_remaining(int(uid))
        await update.message.reply_text(
            f"✅ *License Activated\\!*\n{DIV}\n\n"
            f"   Duration: `{esc(duration_label)}`\n"
            f"   Expires: `{esc(new_expiry.strftime('%Y\\-%m\\-%d %H:%M'))}`\n"
            f"   Total remaining: `{esc(remaining or 'N/A')}`",
            parse_mode=ParseMode.MARKDOWN_V2)
    else:
        await update.message.reply_text("❌ *Invalid or expired key\\.*", parse_mode=ParseMode.MARKDOWN_V2)

async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in OWNER_IDS: return
    if not context.args:
        await update.message.reply_text("Usage: /ban USER\\_ID", parse_mode=ParseMode.MARKDOWN_V2); return
    tid = str(context.args[0])
    ud = db.get(tid, {"banned": False, "uses": 0, "license_expiry": None})
    ud["banned"] = True
    ud["license_expiry"] = None
    db[tid] = ud
    save_db(db)
    await update.message.reply_text(f"🚫 User `{tid}` banned & license revoked\\.", parse_mode=ParseMode.MARKDOWN_V2)

async def cmd_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in OWNER_IDS: return
    if not context.args:
        valid_durations = ", ".join(LICENSE_DURATIONS.keys())
        await update.message.reply_text(
            f"Usage: /key DURATION\n\n"
            f"Valid durations: {valid_durations}\n\n"
            f"Examples:\n"
            f"  /key 1h   (1 hour)\n"
            f"  /key 7d   (7 days)\n"
            f"  /key 30d  (30 days)")
        return
    dur_key = context.args[0].lower()
    if dur_key not in LICENSE_DURATIONS:
        valid_durations = ", ".join(LICENSE_DURATIONS.keys())
        await update.message.reply_text(f"Invalid duration. Valid: {valid_durations}")
        return
    label, duration = LICENSE_DURATIONS[dur_key]
    key = f"VENOM-{uuid.uuid4().hex[:8].upper()}"
    active_keys[key] = {"duration": duration, "label": label}
    await update.message.reply_text(
        f"🔑 *New License Key*\n{DIV}\n\n"
        f"   Key: `{key}`\n"
        f"   Duration: `{esc(label)}`\n\n"
        f"Share this key with the buyer\\.\n"
        f"They activate with: `/redeem {key}`",
        parse_mode=ParseMode.MARKDOWN_V2)

async def cmd_revoke(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in OWNER_IDS: return
    if not context.args:
        await update.message.reply_text("Usage: /revoke USER\\_ID", parse_mode=ParseMode.MARKDOWN_V2)
        return
    tid = str(context.args[0])
    if tid in db:
        db[tid]["license_expiry"] = None
        save_db(db)
        await update.message.reply_text(f"🔓 License revoked for user `{tid}`\\.", parse_mode=ParseMode.MARKDOWN_V2)
    else:
        await update.message.reply_text(f"User `{tid}` not found\\.", parse_mode=ParseMode.MARKDOWN_V2)


# ──────────────────────────────────────────────
#  BUTTON HANDLER
# ──────────────────────────────────────────────

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if is_banned(uid): return
    await q.answer()
    data = q.data

    if data == 'back_menu':
        user_states.pop(uid, None)
        await q.edit_message_text(WELCOME, reply_markup=main_menu_kb(), parse_mode=ParseMode.MARKDOWN_V2)
        return
    if data == '_noop':
        return
    if data == 'show_license':
        remaining = get_license_remaining(uid)
        if remaining:
            status_icon = "🟢"
            status_text = "Active"
            expiry_text = f"   Time left: `{esc(remaining)}`\n"
        else:
            status_icon = "🔴"
            status_text = "Inactive"
            expiry_text = "   _No active license_\n"
        ud = get_user(uid)
        text = (
            f"🔑 *License Status*\n{DIV}\n\n"
            f"   Status: {status_icon} *{status_text}*\n"
            f"{expiry_text}"
            f"   Uses: `{ud.get('uses', 0)}`\n\n"
            f"Use /redeem `KEY` to activate a license\\."
        )
        await q.edit_message_text(text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=back_kb())
        return
    if data == 'show_help':
        await q.edit_message_text(HELP, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=back_kb())
        return

    # ══════════════════════════════════════════
    #  KEYWORD MAKER
    # ══════════════════════════════════════════
    if data == 'mode_kw':
        if not user_has_license(uid):
            await q.edit_message_text(
                f"🔴 *License Required*\n{DIV}\n\n"
                f"Keyword Maker requires an active license\\.\n\n"
                f"Contact the owner to purchase a license key,\n"
                f"then activate with /redeem `KEY`\\.",
                parse_mode=ParseMode.MARKDOWN_V2, reply_markup=back_kb())
            return
        user_states[uid] = {"mode": "KEYWORD", "step": "choose_kw_type"}
        remaining = get_license_remaining(uid)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔤  Single Site", callback_data='kw_single')],
            [InlineKeyboardButton("📦  Multi-Keyword (Bulk)", callback_data='kw_multi')],
            [InlineKeyboardButton("🔍  Anti-Public Checker", callback_data='kw_antipub')],
            [InlineKeyboardButton("⬅️  Back to Menu", callback_data='back_menu')],
        ])
        text = (
            f"🔤 *Keyword Maker*\n{DIV}\n\n"
            f"🌐 *Google Deep Scrape* \\+ Web Crawling\\.\n"
            f"🟢 License: `{esc(remaining or '')}`\n\n"
            f"Choose a mode:\n\n"
            f"🔤 *Single Site*\n"
            f"   _Enter one brand → scrape Google \\+ crawl pages_\n\n"
            f"📦 *Multi\\-Keyword \\(Bulk\\)*\n"
            f"   _Enter multiple brands at once, merge results_\n\n"
            f"🔍 *Anti\\-Public Checker*\n"
            f"   _Check keywords rarity, keep only UHQ ones_"
        )
        await q.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN_V2)
        return

    if data == 'kw_single':
        user_states[uid] = {"mode": "KEYWORD", "step": "count", "kw_type": "single"}
        rows = count_kb("kwcount")
        rows.append([InlineKeyboardButton("⬅️  Back", callback_data='mode_kw')])
        text = (
            f"🔤 *Single Site — Choose Count*\n{DIV}\n\n"
            f"📊 *How many keywords* to generate?\n\n"
            f"Pick a preset or type a custom number\\."
        )
        await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.MARKDOWN_V2)
        return

    if data == 'kw_multi':
        user_states[uid] = {"mode": "KEYWORD", "step": "count", "kw_type": "multi"}
        rows = count_kb("kwcount")
        rows.append([InlineKeyboardButton("⬅️  Back", callback_data='mode_kw')])
        text = (
            f"📦 *Multi\\-Keyword — Choose Count*\n{DIV}\n\n"
            f"📊 *How many keywords per brand?*\n\n"
            f"Pick a preset or type a custom number\\.\n"
            f"_Total output \\= count × number of brands_"
        )
        await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.MARKDOWN_V2)
        return

    if data == 'kw_antipub':
        user_states[uid] = {"mode": "KEYWORD", "step": "antipub_input", "kw_type": "antipub"}
        bk = InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️  Back", callback_data='mode_kw')],
        ])
        text = (
            f"🔍 *Anti\\-Public Checker*\n{DIV}\n\n"
            f"Checks each keyword on *Google* for real\n"
            f"result count to find the *rare UHQ* ones\\.\n\n"
            f"📊 *Output \\(3 files\\):*\n"
            f"   🟢 Anti\\-Public \\(<50K results\\)\n"
            f"   🟡 Semi\\-Public \\(50K\\-500K\\)\n"
            f"   🔴 Public \\(>500K results\\)\n\n"
            f"📝 *How to send:*\n"
            f"• Paste keywords below \\(one per line\\)\n"
            f"• Or upload a `.txt` file\n\n"
            f"⚡ `{ANTIPUB_CONCURRENCY}` concurrent checks"
        )
        await q.edit_message_text(text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=bk)
        return

    if data.startswith('kwcount_'):
        val = data.replace('kwcount_', '')
        st = user_states.get(uid, {})
        kw_type = st.get("kw_type", "single")
        if val == 'custom':
            st.update({"step": "custom_count"})
            user_states[uid] = st
            back_target = f"kw_{kw_type}"
            bk = InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️  Back", callback_data=back_target)],
            ])
            await q.edit_message_text(
                f"🔤 *Custom Count*\n{DIV}\n\n"
                f"Type a number below \\(e\\.g\\. `1500`\\):\n",
                parse_mode=ParseMode.MARKDOWN_V2, reply_markup=bk)
            return
        max_count = int(val)
        if kw_type == "multi":
            st.update({"step": "multi_input", "max_count": max_count})
            user_states[uid] = st
            bk = InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️  Change Count", callback_data='kw_multi')],
            ])
            await q.edit_message_text(
                f"📦 *Multi\\-Keyword — Enter Brands*\n{DIV}\n\n"
                f"   Keywords per brand: `{max_count}`\n\n"
                f"✏️ Send me *multiple brands/keywords*\n"
                f"\\(one per line, or upload a \\.txt file\\)\n\n"
                f"💡 _Example:_\n"
                f"`netflix`\n`spotify`\n`amazon`\n`disney`\n`hulu`\n",
                parse_mode=ParseMode.MARKDOWN_V2, reply_markup=bk)
            return
        else:
            st.update({"step": "site_input", "max_count": max_count})
            user_states[uid] = st
            bk = InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️  Change Count", callback_data='kw_single')],
            ])
            await q.edit_message_text(
                f"🔤 *Single Site — Enter Site*\n{DIV}\n\n"
                f"   Keywords to generate: `{max_count}`\n\n"
                f"✏️ Now send me a *site name or URL*\n\n"
                f"💡 _Examples:_\n"
                f"`netflix.com`\n`spotify`\n`amazon.com`\n",
                parse_mode=ParseMode.MARKDOWN_V2, reply_markup=bk)
            return

    # ── Mode: Parser ──
    if data == 'mode_parse':
        user_states[uid] = {"mode": "PARSER"}
        has_lic = user_has_license(uid)
        remaining = get_license_remaining(uid)
        if has_lic:
            lic_line = f"🟢 License: *Active* \\(`{esc(remaining or '')}`\\)"
        else:
            lic_line = "🔴 License: *Inactive* — /redeem a key first"
        text = (
            f"🔎 *Deep Parser*\n{DIV}\n\n"
            f"Send me your *dorks* and I'll scrape\n"
            f"Google results using `{PARSER_THREADS}` threads\\.\n\n"
            f"📝 *How to send:*\n"
            f"• Type dorks below \\(one per line\\)\n"
            f"• Or upload a `.txt` file\n\n"
            f"💡 _Example:_\n"
            f"`inurl:php?id=`\n"
            f"`filetype:sql password`\n\n"
            f"{lic_line}\n"
            f"⚡ Threads: `{PARSER_THREADS}` concurrent"
        )
        await q.edit_message_text(text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=back_kb())
        return

    # ══════════════════════════════════════════
    #  DORK GENERATOR MENU
    # ══════════════════════════════════════════
    if data == 'mode_gen':
        if not user_has_license(uid):
            await q.edit_message_text(
                f"🔴 *License Required*\n{DIV}\n\n"
                f"Dork Generator requires an active license\\.\n\n"
                f"Contact the owner to purchase a license key,\n"
                f"then activate with /redeem `KEY`\\.",
                parse_mode=ParseMode.MARKDOWN_V2, reply_markup=back_kb())
            return
        user_states[uid] = {"mode": "GENERATOR", "step": "choose_type"}
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🎯  Site Targeted Combo", callback_data='preset_combo')],
            [InlineKeyboardButton("🛒  Shopping SQLi", callback_data='preset_shopping')],
            [InlineKeyboardButton("💳  CC / Payment SQLi", callback_data='preset_cc')],
            [InlineKeyboardButton(f"{DIV}", callback_data='_noop')],
            [InlineKeyboardButton("🛠  Custom Builder", callback_data='custom_start')],
            [InlineKeyboardButton("⬅️  Back to Menu", callback_data='back_menu')],
        ])
        text = (
            f"🛠 *Dork Generator*\n{DIV}\n\n"
            f"Choose a *quick preset* or build custom:\n\n"
            f"🎯 *Site Targeted Combo*\n"
            f"   _SQLi dorks for login/user DB dumping_\n\n"
            f"🛒 *Shopping SQLi*\n"
            f"   _SQLi dorks for e\\-commerce/order dumping_\n\n"
            f"💳 *CC / Payment SQLi*\n"
            f"   _SQLi dorks for payment/billing systems_\n\n"
            f"{DIV}\n"
            f"🛠 *Custom Builder*\n"
            f"   _Full control over dork type, site, params_"
        )
        await q.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN_V2)
        return

    # ── Presets — pick count ──
    if data.startswith('preset_'):
        preset_key = data.replace('preset_', '')
        preset = PRESETS.get(preset_key)
        if not preset: return
        user_states[uid] = {
            "mode": "GENERATOR", "step": "preset_count",
            "gen_type": "preset", "preset": preset_key,
        }
        rows = count_kb(f"pcount_{preset_key}")
        rows.append([InlineKeyboardButton("⬅️  Back", callback_data='mode_gen')])
        text = (
            f"{preset['icon']} *{esc(preset['label'])}*\n{DIV}\n\n"
            f"_{esc(preset['desc'])}_\n\n"
            f"⚙️ *Auto\\-configured:*\n"
            f"   Templates: `{len(preset['templates'])}` patterns\n\n"
            f"📊 *How many dorks* to generate?\n"
        )
        await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.MARKDOWN_V2)
        return

    # ── Preset count -> custom or value ──
    if data.startswith('pcount_'):
        parts = data.split('_')
        preset_key = parts[1]
        val = parts[2]
        if val == 'custom':
            st = user_states.get(uid, {})
            st.update({"step": "custom_count", "count_next": "preset_keywords"})
            user_states[uid] = st
            bk = InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️  Back", callback_data=f"preset_{preset_key}")],
            ])
            await q.edit_message_text(
                f"🛠 *Custom Count*\n{DIV}\n\nType a number below \\(e\\.g\\. `2000`\\):\n",
                parse_mode=ParseMode.MARKDOWN_V2, reply_markup=bk)
            return
        max_count = int(val)
        preset = PRESETS.get(preset_key, {})
        st = user_states.get(uid, {})
        st.update({"step": "keywords", "max_count": max_count})
        user_states[uid] = st
        bk = InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️  Change Count", callback_data=f"preset_{preset_key}")],
            [InlineKeyboardButton("⬅️  Back to Generator", callback_data='mode_gen')],
        ])
        text = (
            f"{preset.get('icon','')} *{esc(preset.get('label',''))}*\n{DIV}\n\n"
            f"   Templates: `{len(preset.get('templates',[]))}` patterns\n"
            f"   Max dorks: `{max_count}`\n\n"
            f"✏️ Now send me your *keywords*\n"
            f"\\(one per line, or upload a \\.txt file\\)"
        )
        await q.edit_message_text(text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=bk)
        return

    # ══════════════════════════════════════════
    #  CUSTOM BUILDER
    # ══════════════════════════════════════════
    if data == 'custom_start':
        user_states[uid] = {"mode": "GENERATOR", "step": "cust_dtype", "gen_type": "custom"}
        rows = []
        for key, cat in DORK_CATEGORIES.items():
            rows.append([InlineKeyboardButton(f"{cat['icon']}  {cat['label']}", callback_data=f"cdtype_{key}")])
        rows.append([InlineKeyboardButton("⬅️  Back", callback_data='mode_gen')])
        text = f"🛠 *Custom Builder — Step 1/4*\n{DIV}\n\nChoose the *type of dorks*:\n"
        await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.MARKDOWN_V2)
        return

    if data.startswith("cdtype_"):
        dtype = data.replace("cdtype_", "")
        st = user_states.get(uid, {}); st.update({"step": "cust_site", "dork_type": dtype}); user_states[uid] = st
        cat = DORK_CATEGORIES.get(dtype, {})
        rows = []
        for key, site in SITE_TYPES.items():
            rows.append([InlineKeyboardButton(f"{site['icon']}  {site['label']}", callback_data=f"csite_{key}")])
        rows.append([InlineKeyboardButton("⬅️  Back", callback_data='custom_start')])
        text = (
            f"🛠 *Custom Builder — Step 2/4*\n{DIV}\n\n"
            f"   Dork type: {cat.get('icon','')} *{esc(cat.get('label',''))}*\n\nChoose *site scope*:\n"
        )
        await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.MARKDOWN_V2)
        return

    if data.startswith("csite_"):
        stype = data.replace("csite_", "")
        st = user_states.get(uid, {}); st.update({"step": "cust_param", "site_type": stype}); user_states[uid] = st
        dtype = st.get("dork_type", "all"); cat = DORK_CATEGORIES.get(dtype, {}); site = SITE_TYPES.get(stype, {})
        rows = []
        for key, pp in PAGE_PARAMS.items():
            rows.append([InlineKeyboardButton(f"{pp['icon']}  {pp['label']}", callback_data=f"cparam_{key}")])
        rows.append([InlineKeyboardButton("⬅️  Back", callback_data=f"cdtype_{dtype}")])
        text = (
            f"🛠 *Custom Builder — Step 3/4*\n{DIV}\n\n"
            f"   Dork type: {cat.get('icon','')} *{esc(cat.get('label',''))}*\n"
            f"   Site scope: {site.get('icon','')} *{esc(site.get('label',''))}*\n\nChoose *parameters*:\n"
        )
        await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.MARKDOWN_V2)
        return

    if data.startswith("cparam_"):
        pparam = data.replace("cparam_", "")
        st = user_states.get(uid, {}); st.update({"step": "cust_count", "page_param": pparam}); user_states[uid] = st
        dtype = st.get("dork_type", "all"); stype = st.get("site_type", "any")
        cat = DORK_CATEGORIES.get(dtype, {}); site = SITE_TYPES.get(stype, {}); pp = PAGE_PARAMS.get(pparam, {})
        rows = count_kb("ccount")
        rows.append([InlineKeyboardButton("⬅️  Back", callback_data=f"csite_{stype}")])
        text = (
            f"🛠 *Custom Builder — Step 4/4*\n{DIV}\n\n"
            f"   Dork type: {cat.get('icon','')} *{esc(cat.get('label',''))}*\n"
            f"   Site scope: {site.get('icon','')} *{esc(site.get('label',''))}*\n"
            f"   Parameters: {pp.get('icon','')} *{esc(pp.get('label',''))}*\n\n"
            f"📊 *How many dorks* to generate?\n"
        )
        await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.MARKDOWN_V2)
        return

    if data.startswith("ccount_"):
        val = data.replace("ccount_", "")
        st = user_states.get(uid, {})
        if val == 'custom':
            st.update({"step": "custom_count", "count_next": "custom_keywords"}); user_states[uid] = st
            pparam = st.get("page_param", "none")
            bk = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️  Back", callback_data=f"cparam_{pparam}")]])
            await q.edit_message_text(
                f"🛠 *Custom Count*\n{DIV}\n\nType a number below \\(e\\.g\\. `2000`\\):\n",
                parse_mode=ParseMode.MARKDOWN_V2, reply_markup=bk)
            return
        max_count = int(val)
        st.update({"step": "keywords", "max_count": max_count}); user_states[uid] = st
        dtype = st.get("dork_type", "all"); stype = st.get("site_type", "any"); pparam = st.get("page_param", "none")
        cat = DORK_CATEGORIES.get(dtype, {}); site = SITE_TYPES.get(stype, {}); pp = PAGE_PARAMS.get(pparam, {})
        bk = InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️  Change Count", callback_data=f"cparam_{pparam}")],
            [InlineKeyboardButton("⬅️  Start Over", callback_data='mode_gen')],
        ])
        text = (
            f"🛠 *Custom Builder — Send Keywords*\n{DIV}\n\n"
            f"   Dork type: {cat.get('icon','')} *{esc(cat.get('label',''))}*\n"
            f"   Site scope: {site.get('icon','')} *{esc(site.get('label',''))}*\n"
            f"   Parameters: {pp.get('icon','')} *{esc(pp.get('label',''))}*\n"
            f"   Max dorks: `{max_count}`\n\n"
            f"✏️ Send *keywords* \\(text or \\.txt file\\)"
        )
        await q.edit_message_text(text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=bk)
        return

# ──────────────────────────────────────────────
#  CORE PROCESSING
# ──────────────────────────────────────────────

async def process_input(update: Update, context: ContextTypes.DEFAULT_TYPE, lines: list):
    uid = update.effective_user.id
    uid_s = str(uid)
    st = user_states.get(uid, {})
    mode = st.get("mode")
    step = st.get("step")
    logger.info("process: user=%s mode=%s step=%s items=%d", uid_s, mode, step, len(lines))

    if not mode:
        await update.message.reply_text(
            "⚠️ *No mode selected\\!*\n\nTap /start first\\.",
            parse_mode=ParseMode.MARKDOWN_V2)
        return

    # ── CUSTOM COUNT INPUT ──
    if step == "custom_count":
        raw = lines[0].strip()
        try:
            num = int(raw)
            if num < 1 or num > 50000:
                await update.message.reply_text("⚠️ Number must be between 1 and 50000\\.", parse_mode=ParseMode.MARKDOWN_V2)
                return
        except ValueError:
            await update.message.reply_text("⚠️ Please send a *valid number*\\.", parse_mode=ParseMode.MARKDOWN_V2)
            return

        count_next = st.get("count_next", "")
        st["max_count"] = num

        if mode == "KEYWORD":
            kw_type = st.get("kw_type", "single")
            if kw_type == "multi":
                st["step"] = "multi_input"
                user_states[uid] = st
                await update.message.reply_text(
                    f"📦 *Multi\\-Keyword — Enter Brands*\n{DIV}\n\n"
                    f"   Keywords per brand: `{num}`\n\n"
                    f"✏️ Send *multiple brands/keywords*\n"
                    f"\\(one per line, or upload a \\.txt file\\)\n",
                    parse_mode=ParseMode.MARKDOWN_V2)
                return
            else:
                st["step"] = "site_input"
                user_states[uid] = st
                await update.message.reply_text(
                    f"🔤 *Keyword Maker — Enter Site*\n{DIV}\n\n"
                    f"   Keywords to generate: `{num}`\n\n"
                    f"✏️ Send me a *site name or URL*\n\n"
                    f"💡 _Examples:_ `netflix.com`, `spotify`, `amazon`\n",
                    parse_mode=ParseMode.MARKDOWN_V2)
                return

        if count_next == "preset_keywords":
            st["step"] = "keywords"
            user_states[uid] = st
            preset_key = st.get("preset", "combo")
            preset = PRESETS.get(preset_key, {})
            await update.message.reply_text(
                f"{preset.get('icon','')} *{esc(preset.get('label',''))}*\n{DIV}\n\n"
                f"   Max dorks: `{num}`\n\n"
                f"✏️ Send *keywords* \\(text or \\.txt file\\)\n",
                parse_mode=ParseMode.MARKDOWN_V2)
            return

        if count_next == "custom_keywords":
            st["step"] = "keywords"
            user_states[uid] = st
            await update.message.reply_text(
                f"🛠 *Custom Builder*\n{DIV}\n\n"
                f"   Max dorks: `{num}`\n\n"
                f"✏️ Send *keywords* \\(text or \\.txt file\\)\n",
                parse_mode=ParseMode.MARKDOWN_V2)
            return
        return

    # ── KEYWORD MAKER — single site input ──
    if mode == "KEYWORD" and step == "site_input":
        site_raw = lines[0].strip()
        brand = extract_brand(site_raw)
        max_count = st.get("max_count", 500)
        logger.info("KW: brand='%s' max=%d", brand, max_count)

        status = await update.message.reply_text(
            f"🔤 *Keyword Maker — Starting*\n{DIV}\n\n"
            f"   Site: `{esc(brand)}`\n"
            f"   Target: `{max_count}` keywords\n\n{pbar(0, 1)}\n\n"
            f"⏳ Scraping Google \\+ AI \\+ expanding\\.\\.\\.",
            parse_mode=ParseMode.MARKDOWN_V2)

        kw_sem = get_semaphore(uid)
        async with aiohttp.ClientSession() as session:
            keywords = await generate_keywords(session, brand, max_count, status, kw_sem)

        out = io.BytesIO("\n".join(keywords).encode())
        out.name = f"keywords_{brand}_{len(keywords)}.txt"

        ud = get_user(uid_s)
        ud["uses"] = ud.get("uses", 0) + 1
        db[uid_s] = ud; save_db(db)

        await status.edit_text(
            f"✅ *Keywords Generated\\!*\n{DIV}\n\n"
            f"   Site: `{esc(brand)}`\n"
            f"   Generated: `{len(keywords)}`\n"
            f"   Requested: `{max_count}`\n\n{pbar(1, 1)}\n\n"
            f"📄 File attached below ⬇️",
            parse_mode=ParseMode.MARKDOWN_V2)

        await update.message.reply_document(
            document=out,
            caption=f"🔤 {len(keywords)} UHQ keywords for {brand}")
        return

    # ── KEYWORD MAKER — multi-keyword bulk input ──
    if mode == "KEYWORD" and step == "multi_input":
        brands = []
        for line in lines:
            b = extract_brand(line.strip())
            if b and len(b) > 1:
                brands.append(b)
        brands = list(dict.fromkeys(brands))

        if not brands:
            await update.message.reply_text(
                "⚠️ *No valid brands found\\!*\n\nSend brand names, one per line\\.",
                parse_mode=ParseMode.MARKDOWN_V2)
            return

        if len(brands) > 50:
            await update.message.reply_text(
                "⚠️ *Too many brands\\!* Maximum is 50 at once\\.",
                parse_mode=ParseMode.MARKDOWN_V2)
            return

        max_per_brand = st.get("max_count", 500)
        total_target = max_per_brand * len(brands)
        brands_display = ", ".join(brands[:5])
        if len(brands) > 5:
            brands_display += f" \\+{len(brands) - 5} more"

        status = await update.message.reply_text(
            f"📦 *Multi\\-Keyword — Starting*\n{DIV}\n\n"
            f"   Brands: `{len(brands)}`\n"
            f"   Per brand: `{max_per_brand}` keywords\n"
            f"   Total target: `{total_target}`\n\n"
            f"   Processing: `{esc(brands_display)}`\n\n"
            f"{pbar(0, len(brands))}\n\n"
            f"⏳ This may take a while\\.\\.\\.",
            parse_mode=ParseMode.MARKDOWN_V2)

        all_keywords = set()
        kw_sem = get_semaphore(uid)
        per_brand_counts = {}

        async with aiohttp.ClientSession() as session:
            for i, brand in enumerate(brands):
                try:
                    await status.edit_text(
                        f"📦 *Multi\\-Keyword — Processing*\n{DIV}\n\n"
                        f"   Brands: `{i + 1}/{len(brands)}`\n"
                        f"   Current: `{esc(brand)}`\n"
                        f"   Total keywords: `{len(all_keywords)}`\n\n"
                        f"{pbar(i, len(brands))}\n\n"
                        f"⏳ 🤖 AI \\+ Google \\+ algorithmic\\.\\.\\.",
                        parse_mode=ParseMode.MARKDOWN_V2)
                except Exception:
                    pass

                brand_kw = await generate_keywords(session, brand, max_per_brand, status, kw_sem)
                before = len(all_keywords)
                all_keywords.update(brand_kw)
                added = len(all_keywords) - before
                per_brand_counts[brand] = added
                logger.info("MULTI-KW: brand='%s' generated=%d unique_added=%d total=%d",
                            brand, len(brand_kw), added, len(all_keywords))

        kw_list = list(all_keywords)
        random.shuffle(kw_list)

        out = io.BytesIO("\n".join(kw_list).encode())
        out.name = f"bulk_keywords_{len(brands)}brands_{len(kw_list)}.txt"

        ud = get_user(uid_s)
        ud["uses"] = ud.get("uses", 0) + 1
        db[uid_s] = ud; save_db(db)

        top_brands = sorted(per_brand_counts.items(), key=lambda x: x[1], reverse=True)[:5]
        breakdown = "\n".join([f"   `{esc(b)}`: {c}" for b, c in top_brands])
        if len(brands) > 5:
            breakdown += f"\n   _\\.\\.\\. and {len(brands) - 5} more_"

        await status.edit_text(
            f"✅ *Multi\\-Keyword — Done\\!*\n{DIV}\n\n"
            f"   Brands processed: `{len(brands)}`\n"
            f"   Total keywords: `{len(kw_list)}`\n"
            f"   Per brand target: `{max_per_brand}`\n\n"
            f"📊 *Breakdown:*\n{breakdown}\n\n"
            f"{pbar(1, 1)}\n\n📄 File below ⬇️",
            parse_mode=ParseMode.MARKDOWN_V2)

        await update.message.reply_document(
            document=out,
            caption=f"📦 {len(kw_list)} UHQ keywords from {len(brands)} brands")
        return

    # ── ANTI-PUBLIC CHECKER (Google-based, real result counts) ──
    if mode == "KEYWORD" and step == "antipub_input":
        input_keywords = [l.strip() for l in lines if l.strip() and len(l.strip()) > 2]
        input_keywords = list(dict.fromkeys(input_keywords))

        if len(input_keywords) < 5:
            await update.message.reply_text(
                "⚠️ *Too few keywords\\!*\n\nSend at least 5 keywords to check\\.",
                parse_mode=ParseMode.MARKDOWN_V2)
            return

        if len(input_keywords) > 10000:
            await update.message.reply_text(
                "⚠️ *Too many keywords\\!* Maximum is 10000 at once\\.",
                parse_mode=ParseMode.MARKDOWN_V2)
            return

        status = await update.message.reply_text(
            f"🔍 *Anti\\-Public Checker — Starting*\n{DIV}\n\n"
            f"   Keywords: `{len(input_keywords)}`\n"
            f"   ⚡ {ANTIPUB_CONCURRENCY} concurrent Google checks\n\n"
            f"{pbar(0, len(input_keywords))}\n\n"
            f"⏳ Checking each keyword against Google\\.\\.\\.",
            parse_mode=ParseMode.MARKDOWN_V2)

        antipub_sem = asyncio.Semaphore(ANTIPUB_CONCURRENCY)
        anti_public = []
        semi_public = []
        public = []
        errors = 0
        done_count = 0
        lock = asyncio.Lock()
        total = len(input_keywords)

        async with aiohttp.ClientSession() as session:
            async def _check(kw):
                nonlocal done_count, errors
                keyword, count = await google_check_keyword(session, kw, antipub_sem)
                async with lock:
                    if count < 0:
                        errors += 1
                        semi_public.append(keyword)
                    elif count <= ANTIPUB_THRESHOLDS["rare"]:
                        anti_public.append(keyword)
                    elif count <= ANTIPUB_THRESHOLDS["moderate"]:
                        semi_public.append(keyword)
                    else:
                        public.append(keyword)
                    done_count += 1
                    d = done_count
                if d % 15 == 0 or d == total:
                    try:
                        await status.edit_text(
                            f"🔍 *Anti\\-Public — Google Checking*\n{DIV}\n\n"
                            f"   Checked: `{d}/{total}`\n"
                            f"   🟢 Anti\\-Public: `{len(anti_public)}`\n"
                            f"   🟡 Semi\\-Public: `{len(semi_public)}`\n"
                            f"   🔴 Public: `{len(public)}`\n\n"
                            f"{pbar(d, total)}\n\n"
                            f"⚡ {ANTIPUB_CONCURRENCY} concurrent\\.\\.\\.",
                            parse_mode=ParseMode.MARKDOWN_V2)
                    except Exception:
                        pass

            for i in range(0, total, ANTIPUB_CONCURRENCY):
                batch = input_keywords[i:i+ANTIPUB_CONCURRENCY]
                await asyncio.gather(*[_check(kw) for kw in batch], return_exceptions=True)

        ud = get_user(uid_s)
        ud["uses"] = ud.get("uses", 0) + 1
        db[uid_s] = ud; save_db(db)

        ap_pct = int(100 * len(anti_public) / total) if total else 0
        sp_pct = int(100 * len(semi_public) / total) if total else 0
        pb_pct = int(100 * len(public) / total) if total else 0
        err_note = f"\n   ⚠️ Errors \\(→ semi\\): `{errors}`" if errors else ""

        summary = (
            f"✅ *Anti\\-Public Check — Done\\!*\n{DIV}\n\n"
            f"   Total checked: `{total}`{err_note}\n\n"
            f"   🟢 Anti\\-Public \\(<50K results\\): `{len(anti_public)}` \\({ap_pct}%\\)\n"
            f"   🟡 Semi\\-Public \\(50K\\-500K\\): `{len(semi_public)}` \\({sp_pct}%\\)\n"
            f"   🔴 Public \\(>500K results\\): `{len(public)}` \\({pb_pct}%\\)\n\n"
            f"{pbar(1, 1)}\n\n📄 Files below ⬇️"
        )
        await status.edit_text(summary, parse_mode=ParseMode.MARKDOWN_V2)

        if anti_public:
            f_ap = io.BytesIO("\n".join(anti_public).encode())
            f_ap.name = f"anti_public_{len(anti_public)}.txt"
            await update.message.reply_document(document=f_ap,
                caption=f"🟢 {len(anti_public)} Anti-Public (rare/UHQ) keywords")

        if semi_public:
            f_sp = io.BytesIO("\n".join(semi_public).encode())
            f_sp.name = f"semi_public_{len(semi_public)}.txt"
            await update.message.reply_document(document=f_sp,
                caption=f"🟡 {len(semi_public)} Semi-Public keywords")

        if public:
            f_pb = io.BytesIO("\n".join(public).encode())
            f_pb.name = f"public_{len(public)}.txt"
            await update.message.reply_document(document=f_pb,
                caption=f"🔴 {len(public)} Public (overused) keywords")
        return

    # ── GENERATOR ──
    if mode == "GENERATOR":
        if step != "keywords":
            await update.message.reply_text(
                "⚠️ *Please complete the setup first\\!*\n\nUse the buttons above\\.",
                parse_mode=ParseMode.MARKDOWN_V2)
            return

        gen_type = st.get("gen_type", "custom")
        max_count = st.get("max_count", 500)

        if gen_type == "preset":
            preset_key = st.get("preset", "combo")
            preset = PRESETS.get(preset_key, {})
            label = preset.get("label", "Preset"); icon = preset.get("icon", "🛠")
            status = await update.message.reply_text(
                f"{icon} *Generating {esc(label)}\\.\\.\\.*\n{DIV}\n\n"
                f"   Keywords: `{len(lines)}`\n   Max dorks: `{max_count}`\n\n{pbar(0, 1)}",
                parse_mode=ParseMode.MARKDOWN_V2)
            dorks = build_preset_dorks(lines, preset_key, max_count)
        else:
            dtype = st.get("dork_type", "all"); stype = st.get("site_type", "any"); pparam = st.get("page_param", "none")
            cat = DORK_CATEGORIES.get(dtype, {}); site = SITE_TYPES.get(stype, {}); pp = PAGE_PARAMS.get(pparam, {})
            icon = "🛠"; label = "Custom Dorks"
            status = await update.message.reply_text(
                f"🛠 *Generating\\.\\.\\.*\n{DIV}\n\n"
                f"   Type: {cat.get('icon','')} {esc(cat.get('label',''))}\n"
                f"   Scope: {site.get('icon','')} {esc(site.get('label',''))}\n"
                f"   Keywords: `{len(lines)}` | Max: `{max_count}`\n\n{pbar(0, 1)}",
                parse_mode=ParseMode.MARKDOWN_V2)
            dorks = build_custom_dorks(lines, dtype, stype, pparam, max_count)

        out = io.BytesIO("\n".join(dorks).encode())
        out.name = f"dorks_{gen_type}_{len(dorks)}.txt"
        ud = get_user(uid_s); ud["uses"] = ud.get("uses", 0) + 1; db[uid_s] = ud; save_db(db)

        await status.edit_text(
            f"✅ *{esc(label)} — Done\\!*\n{DIV}\n\n"
            f"   Keywords: `{len(lines)}`\n   Dorks: `{len(dorks)}`\n   Limit: `{max_count}`\n\n"
            f"{pbar(1, 1)}\n\n📄 File below ⬇️",
            parse_mode=ParseMode.MARKDOWN_V2)
        await update.message.reply_document(document=out, caption=f"{icon} {len(dorks)} dorks from {len(lines)} keywords")
        return

    # ── PARSER (license required) ──
    if mode == "PARSER":
        if not user_has_license(uid):
            remaining = get_license_remaining(uid)
            await update.message.reply_text(
                f"🔴 *License Required*\n{DIV}\n\n"
                f"Deep Parser requires an active license\\.\n\n"
                f"Contact the owner to purchase a license key,\n"
                f"then activate it with /redeem `KEY`\\.",
                parse_mode=ParseMode.MARKDOWN_V2)
            return

        remaining = get_license_remaining(uid)
        icon = "🔎"
        label = "Deep Parser"
        item_w = "dorks"

        queue_len = global_queue.get_queue_length()
        active = global_queue.active
        queue_note = ""
        if active >= global_queue.max:
            queue_pos = queue_len + 1
            queue_note = (
                f"\n\n🚦 *Queue Status:*\n"
                f"   Active: `{active}/{global_queue.max}` slots\n"
                f"   Your position: *\\#{queue_pos}*\n"
                f"   _Your task will start automatically\\._"
            )

        status = await update.message.reply_text(
            f"{icon} *{esc(label)} — {'Queued' if queue_note else 'Running'}*\n{DIV}\n\n"
            f"   Items: `{len(lines)}` {item_w}\n"
            f"   🟢 License: `{esc(remaining or '')}`\n"
            f"   ⚡ Threads: `{PARSER_THREADS}` concurrent"
            f"{queue_note}\n\n"
            f"{pbar(0, len(lines))}\n\n⏳ Please wait\\.\\.\\.",
            parse_mode=ParseMode.MARKDOWN_V2)

        results = []; errors = 0; done_count = 0; lock = asyncio.Lock()
        sem = get_semaphore(uid)

        try:
            async with aiohttp.ClientSession() as session:
                started = False
                async def _pw(dork):
                    nonlocal done_count, errors, started
                    try:
                        urls = await fetch_oxylabs(session, dork, sem)
                        if isinstance(urls, list):
                            async with lock: results.extend(urls)
                        else:
                            async with lock: errors += 1
                    except Exception as e:
                        logger.error("PW err: %s", e)
                        async with lock: errors += 1
                    async with lock:
                        if not started:
                            started = True
                        done_count += 1
                        d = done_count
                    if d % 2 == 0 or d == len(lines):
                        qi = ""
                        a = global_queue.active
                        if a >= global_queue.max and d < len(lines):
                            qi = f"\n   🚦 Queue: `{a}/{global_queue.max}` slots active\n"
                        try:
                            await status.edit_text(
                                f"{icon} *{esc(label)} — Running*\n{DIV}\n\n"
                                f"   Done: `{d}/{len(lines)}` {item_w}\n   Found: `{len(results)}` URLs\n"
                                f"   ⚡ Threads: `{PARSER_THREADS}`{qi}\n\n{pbar(d, len(lines))}\n\n⏳ Please wait\\.\\.\\.",
                                parse_mode=ParseMode.MARKDOWN_V2)
                        except Exception: pass
                await asyncio.gather(*[_pw(d) for d in lines], return_exceptions=True)
        except Exception as e:
            logger.error("Session err: %s\n%s", e, traceback.format_exc())
            await status.edit_text(f"❌ *Error*\n{DIV}\n\n`{esc(str(e))}`", parse_mode=ParseMode.MARKDOWN_V2)
            return

        ud = get_user(uid_s)
        ud["uses"] = ud.get("uses", 0) + 1
        db[uid_s] = ud; save_db(db)

        raw_count = len(results)
        final = list(set(results))
        logger.info("PARSER done: user=%s raw=%d unique=%d", uid_s, raw_count, len(final))
        err_note = f"\n   ⚠️ Errors: `{errors}`\n" if errors else ""

        if not final:
            await status.edit_text(
                f"{icon} *{esc(label)} — Complete*\n{DIV}\n\n"
                f"   Scanned: `{len(lines)}` {item_w}\n   Results: `0`{err_note}\n\n"
                f"{pbar(1, 1)}", parse_mode=ParseMode.MARKDOWN_V2)
        else:
            f_out = io.BytesIO("\n".join(final).encode())
            f_out.name = f"parsed_urls_{datetime.now().strftime('%H%M%S')}.txt"
            await status.edit_text(
                f"{icon} *{esc(label)} — Complete*\n{DIV}\n\n"
                f"   Scanned: `{len(lines)}` {item_w}\n"
                f"   Raw URLs: `{raw_count}`\n"
                f"   Unique URLs: `{len(final)}`{err_note}\n\n"
                f"{pbar(1, 1)}\n\n📄 File contains all `{len(final)}` unique URLs ⬇️", parse_mode=ParseMode.MARKDOWN_V2)
            await update.message.reply_document(document=f_out, caption=f"{icon} {len(final)} unique URLs (from {raw_count} raw)")
        return

    await update.message.reply_text(
        "⚠️ *Unknown mode\\!*\n\nTap /start to begin\\.",
        parse_mode=ParseMode.MARKDOWN_V2)

# ──────────────────────────────────────────────
#  INPUT HANDLERS
# ──────────────────────────────────────────────

async def file_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_banned(update.effective_user.id): return
    uid = update.effective_user.id
    st = user_states.get(uid, {})
    if not st.get("mode"):
        await update.message.reply_text("⚠️ *No mode selected\\!* Tap /start first\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return
    try:
        tg_file = await context.bot.get_file(update.message.document.file_id)
        raw = await tg_file.download_as_bytearray()
        lines = [l.strip() for l in raw.decode("utf-8").splitlines() if l.strip()]
        if not lines:
            await update.message.reply_text("⚠️ *Empty file\\!*", parse_mode=ParseMode.MARKDOWN_V2)
            return
        await process_input(update, context, lines)
    except Exception as e:
        logger.error("file err: %s\n%s", e, traceback.format_exc())
        await update.message.reply_text(f"❌ *File Error:* `{esc(str(e))}`", parse_mode=ParseMode.MARKDOWN_V2)

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_banned(update.effective_user.id): return
    if not update.message or not update.message.text: return
    uid = update.effective_user.id
    st = user_states.get(uid, {})
    if not st.get("mode"): return
    text = update.message.text.strip()
    if text.startswith("/"): return
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines: return
    await process_input(update, context, lines)

# ──────────────────────────────────────────────
#  MAIN
# ──────────────────────────────────────────────

if __name__ == "__main__":
    logger.info("Starting bot...")
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).concurrent_updates(True).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("license", cmd_license))
    app.add_handler(CommandHandler("redeem", cmd_redeem))
    app.add_handler(CommandHandler("key", cmd_key))
    app.add_handler(CommandHandler("ban", cmd_ban))
    app.add_handler(CommandHandler("revoke", cmd_revoke))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.Document.FileExtension("txt"), file_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    logger.info("Bot ready — polling...")
    app.run_polling()
