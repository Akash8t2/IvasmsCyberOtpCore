import asyncio
import os
import re
import json
from datetime import datetime, timedelta

import httpx
from bs4 import BeautifulSoup
from pyppeteer import launch
from telegram import Bot

# ===================== CONFIG =====================
IVASMS_EMAIL = os.getenv("IVASMS_EMAIL")
IVASMS_PASSWORD = os.getenv("IVASMS_PASSWORD")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_IDS = json.loads(os.getenv("TELEGRAM_CHAT_IDS", "[]"))

LOGIN_URL = "https://www.ivasms.com/login"
BASE_URL = "https://www.ivasms.com"
SMS_ENDPOINT = "/portal/sms/received/getsms"

CHECK_INTERVAL = 5
STATE_FILE = "sent_cache.json"

# IMPORTANT: chrome-for-testing binary path on Heroku
CHROME_PATH = "/app/.chrome-for-testing/chrome-linux64/chrome"

if not IVASMS_EMAIL or not IVASMS_PASSWORD:
    raise RuntimeError("❌ IVASMS_EMAIL / IVASMS_PASSWORD missing")

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_IDS:
    raise RuntimeError("❌ Telegram config missing")

bot = Bot(token=TELEGRAM_BOT_TOKEN)

# ===================== UTILS =====================
def extract_otp(text: str):
    m = re.search(r"\b(\d{4,8})\b", text)
    return m.group(1) if m else None

def load_cache():
    if os.path.exists(STATE_FILE):
        try:
            return set(json.load(open(STATE_FILE)))
        except Exception:
            return set()
    return set()

def save_cache(data):
    with open(STATE_FILE, "w") as f:
        json.dump(list(data), f)

# ===================== OTP MESSAGE =====================
def format_otp_message(raw_sms: str):
    otp = extract_otp(raw_sms)
    return (
        "🔐 *NEW OTP RECEIVED*\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"🔢 *OTP:* `{otp if otp else 'N/A'}`\n\n"
        "📩 *FULL MESSAGE:*\n"
        f"```{raw_sms}```\n\n"
        "⚠️ *Do not share this OTP with anyone*"
    )

# ===================== LOGIN (FIXED SELECTORS + SYSTEM CHROME) =====================
async def login_and_get_cookies():
    print("🔐 Launching Chrome (system chrome-for-testing)...")

    browser = await launch(
        headless=True,
        executablePath=CHROME_PATH,   # <<< CRITICAL FIX
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--single-process",
            "--no-zygote",
        ],
    )

    page = await browser.newPage()
    page.setDefaultTimeout(120000)
    page.setDefaultNavigationTimeout(120000)

    await page.goto(LOGIN_URL, {"waitUntil": "domcontentloaded"})
    await asyncio.sleep(5)

    # Try multiple selectors (page structure safe)
    email_selectors = [
        'input[type="email"]',
        'input[type="text"]',
        'input[placeholder*="Email"]',
    ]

    email_selector = None
    for sel in email_selectors:
        try:
            await page.waitForSelector(sel, {"timeout": 10000})
            email_selector = sel
            break
        except Exception:
            pass

    if not email_selector:
        await page.screenshot({"path": "email_not_found.png"})
        await browser.close()
        raise RuntimeError("❌ Email input not found on login page")

    await page.waitForSelector('input[type="password"]')

    await page.type(email_selector, IVASMS_EMAIL, {"delay": 60})
    await page.type('input[type="password"]', IVASMS_PASSWORD, {"delay": 60})

    # Submit
    try:
        await page.click('button[type="submit"]')
    except Exception:
        await page.keyboard.press("Enter")

    await page.waitForNavigation({"waitUntil": "networkidle2"})

    cookies = await page.cookies()
    await browser.close()

    print("✅ IVASMS Login Successful")
    return cookies

# ===================== FETCH SMS =====================
async def fetch_sms(cookies):
    jar = httpx.Cookies()
    for c in cookies:
        jar.set(c["name"], c["value"], domain=c["domain"])

    async with httpx.AsyncClient(cookies=jar, timeout=30) as client:
        dashboard = await client.get(BASE_URL)
        soup = BeautifulSoup(dashboard.text, "html.parser")

        csrf_meta = soup.find("meta", {"name": "csrf-token"})
        if not csrf_meta:
            print("⚠️ CSRF token not found")
            return []

        csrf = csrf_meta["content"]

        payload = {
            "from": (datetime.utcnow() - timedelta(days=1)).strftime("%m/%d/%Y"),
            "to": datetime.utcnow().strftime("%m/%d/%Y"),
            "_token": csrf,
        }

        r = await client.post(
            BASE_URL + SMS_ENDPOINT,
            data=payload,
            headers={"X-CSRF-TOKEN": csrf},
        )

        soup = BeautifulSoup(r.text, "html.parser")
        messages = []
        for card in soup.find_all("div", class_="card-body"):
            text = card.get_text(" ", strip=True)
            if extract_otp(text):
                messages.append(text)
        return messages

# ===================== SEND TO TELEGRAM GROUP =====================
async def send_to_telegram(raw_sms):
    message = format_otp_message(raw_sms)
    for chat_id in TELEGRAM_CHAT_IDS:
        await bot.send_message(chat_id=chat_id, text=message, parse_mode="Markdown")

# ===================== MAIN LOOP =====================
async def main():
    sent_cache = load_cache()
    cookies = await login_and_get_cookies()

    print("🚀 OTP BOT RUNNING (HEROKU-24 + GROUP MODE)")

    while True:
        try:
            messages = await fetch_sms(cookies)
            for msg in messages:
                if msg not in sent_cache:
                    await send_to_telegram(msg)
                    sent_cache.add(msg)
                    save_cache(sent_cache)
                    print("📩 OTP sent to Telegram group")
        except Exception as e:
            print("❌ Runtime error:", e)

        await asyncio.sleep(CHECK_INTERVAL)

# ===================== START =====================
if __name__ == "__main__":
    asyncio.run(main())
