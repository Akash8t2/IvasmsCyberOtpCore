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

CHROME_PATH = "/app/.chrome-for-testing/chrome-linux64/chrome"

bot = Bot(token=TELEGRAM_BOT_TOKEN)

# ===================== UTILS =====================
def extract_otp(text):
    m = re.search(r"\b(\d{4,8})\b", text)
    return m.group(1) if m else None

def load_cache():
    if os.path.exists(STATE_FILE):
        try:
            return set(json.load(open(STATE_FILE)))
        except:
            return set()
    return set()

def save_cache(data):
    with open(STATE_FILE, "w") as f:
        json.dump(list(data), f)

# ===================== OTP MESSAGE =====================
def format_otp_message(raw_sms):
    otp = extract_otp(raw_sms)
    return (
        "🔐 *NEW OTP RECEIVED*\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"🔢 *OTP:* `{otp if otp else 'N/A'}`\n\n"
        "📩 *FULL MESSAGE:*\n"
        f"```{raw_sms}```\n\n"
        "⚠️ *Do not share this OTP with anyone*"
    )

# ===================== CLOUDFLARE SAFE LOGIN =====================
async def login_and_get_cookies(max_retry=3):
    print("🔐 Launching Chrome (Cloudflare-tolerant mode)...")

    for attempt in range(1, max_retry + 1):
        print(f"🔄 Login attempt {attempt}/{max_retry}")

        browser = await launch(
            headless=True,
            executablePath=CHROME_PATH,
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

        try:
            await page.goto(LOGIN_URL, {"waitUntil": "domcontentloaded"})
            await asyncio.sleep(12)  # Cloudflare wait

            email_input = None
            password_input = None
            active_context = page

            # 1️⃣ main page
            email_input = await page.querySelector(
                'input[type="email"], input[name="email"], input[type="text"]'
            )
            password_input = await page.querySelector('input[type="password"]')

            # 2️⃣ iframe scan
            if not email_input:
                for frame in page.frames:
                    try:
                        email_input = await frame.querySelector(
                            'input[type="email"], input[name="email"], input[type="text"]'
                        )
                        password_input = await frame.querySelector('input[type="password"]')
                        if email_input and password_input:
                            active_context = frame
                            break
                    except:
                        continue

            # 3️⃣ still not found → Cloudflare
            if not email_input or not password_input:
                await page.screenshot({"path": f"cloudflare_block_{attempt}.png"})
                print("⚠️ Login form not found (Cloudflare challenge)")
                await browser.close()
                await asyncio.sleep(10)
                continue

            # 4️⃣ type credentials
            await email_input.type(IVASMS_EMAIL, {"delay": 80})
            await password_input.type(IVASMS_PASSWORD, {"delay": 80})

            # submit
            try:
                btn = await active_context.querySelector('button[type="submit"]')
                if btn:
                    await btn.click()
                else:
                    await page.keyboard.press("Enter")
            except:
                await page.keyboard.press("Enter")

            await asyncio.sleep(10)

            cookies = await page.cookies()
            await browser.close()

            if cookies:
                print("✅ Login cookies captured")
                return cookies

        except Exception as e:
            print("❌ Login error:", e)

        await browser.close()
        await asyncio.sleep(10)

    print("🚫 Login failed after retries (Cloudflare block)")
    return None

# ===================== FETCH SMS =====================
async def fetch_sms(cookies):
    if not cookies:
        return []

    jar = httpx.Cookies()
    for c in cookies:
        jar.set(c["name"], c["value"], domain=c["domain"])

    async with httpx.AsyncClient(cookies=jar, timeout=30) as client:
        dash = await client.get(BASE_URL)
        soup = BeautifulSoup(dash.text, "html.parser")

        csrf_meta = soup.find("meta", {"name": "csrf-token"})
        if not csrf_meta:
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
        msgs = []

        for card in soup.find_all("div", class_="card-body"):
            text = card.get_text(" ", strip=True)
            if extract_otp(text):
                msgs.append(text)

        return msgs

# ===================== TELEGRAM =====================
async def send_to_telegram(raw_sms):
    msg = format_otp_message(raw_sms)
    for chat_id in TELEGRAM_CHAT_IDS:
        await bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")

# ===================== MAIN LOOP =====================
async def main():
    sent_cache = load_cache()

    cookies = await login_and_get_cookies()
    if not cookies:
        print("🚫 Cannot login due to Cloudflare. Sleeping.")
        while True:
            await asyncio.sleep(60)

    print("🚀 OTP BOT RUNNING (STABLE MODE)")

    while True:
        try:
            messages = await fetch_sms(cookies)
            for msg in messages:
                if msg not in sent_cache:
                    await send_to_telegram(msg)
                    sent_cache.add(msg)
                    save_cache(sent_cache)
                    print("📩 OTP sent")
        except Exception as e:
            print("❌ Runtime error:", e)

        await asyncio.sleep(CHECK_INTERVAL)

# ===================== START =====================
if __name__ == "__main__":
    asyncio.run(main())
