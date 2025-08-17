# main.py
import os, re, asyncio, time, decimal, json
from typing import Optional, Tuple
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from telegram import Bot, Update
from telegram.ext import Application, MessageHandler, CommandHandler, ContextTypes, filters
from playwright.async_api import async_playwright, Error as PWError

# ------------------- إعدادات عامة -------------------
APP_UA = os.getenv(
    "APP_UA",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
)
SUPPORTED_TLDS = {".com", ".net"}
SPACESHIP_URL = "https://www.spaceship.com/domain-search/?query={q}&beast=false&tab=domains"

# تصنيف الأسعار
PRICE_LOW_MAX = decimal.Decimal(os.getenv("PRICE_LOW_MAX", "10"))   # تسجيل عادي
PRICE_PREMIUM_MIN = decimal.Decimal(os.getenv("PRICE_PREMIUM_MIN", "20"))  # بريميوم مؤكد

# ضبط معدل وكاش بسيط
MIN_INTERVAL = float(os.getenv("MIN_INTERVAL", "0.8"))  # ثانية بين الزيارات
sem = asyncio.Semaphore(int(os.getenv("MAX_CONCURRENCY", "3")))
_last_fetch = 0.0
CACHE_TTL = int(os.getenv("CACHE_TTL", str(6 * 3600)))
_cache: dict[str, tuple[float, dict]] = {}

# ------------------- أدوات مساعدة -------------------
def _now() -> float: return time.time()

def normalize_domain(d: str) -> str: return d.strip().lower()

def validate_domain(d: str) -> str:
    d = normalize_domain(d)
    if not re.fullmatch(r"[a-z0-9-]+\.(com|net)", d):
        raise ValueError("Only .com and .net domains are supported.")
    label, tld = d.rsplit(".", 1)
    if label.startswith("-") or label.endswith("-"):
        raise ValueError("Label cannot start/end with '-'.")
    return d

def extract_price_str(text: str) -> Optional[str]:
    m = re.search(r"\$\s*[\d,]+(?:\.\d{1,2})?\s*/?\s*yr?", text, re.I)
    return m.group(0).replace(" ", "") if m else None

def extract_price_val(text: str) -> Optional[decimal.Decimal]:
    m = re.search(r"\$\s*([\d,]+(?:\.\d{1,2})?)", text)
    if not m: return None
    try:
        return decimal.Decimal(m.group(1).replace(",", ""))
    except decimal.InvalidOperation:
        return None

def classify(price_val: Optional[decimal.Decimal], page_text: str) -> Tuple[str, Optional[str]]:
    """
    يرجع (status, tier)
    status: available / taken / unknown
    tier: registerable / premium / review / None
    """
    tier = None
    status = "unknown"

    # إشارات نصية من الصفحة
    premium_hint = bool(re.search(r"(Premium|Buy\s*now|Aftermarket|Make\s*an\s*offer)", page_text, re.I))
    if re.search(r"(is already registered|is taken|unavailable|not available)", page_text, re.I):
        return "taken", None
    if re.search(r"(is available|Add\sto\s*cart)", page_text, re.I):
        status = "available"

    if price_val is not None:
        if price_val <= PRICE_LOW_MAX:
            tier = "registerable"; status = "available"
        elif price_val >= PRICE_PREMIUM_MIN or premium_hint:
            tier = "premium"; status = "available"
        else:
            tier = "review"; status = "available"
    elif premium_hint:
        tier = "premium"; status = "available"

    return status, tier

def cache_get(key: str) -> Optional[dict]:
    v = _cache.get(key)
    if not v: return None
    ts, data = v
    if _now() - ts > CACHE_TTL:
        _cache.pop(key, None); return None
    return data

def cache_set(key: str, data: dict) -> None:
    _cache[key] = (_now(), data)

# ------------------- سكربينج Spaceship -------------------
async def scrape_spaceship(domain: str) -> dict:
    global _last_fetch
    cached = cache_get(domain)
    if cached: return cached

    async with sem:
        wait = MIN_INTERVAL - (_now() - _last_fetch)
        if wait > 0: await asyncio.sleep(wait)
        _last_fetch = _now()

        url = SPACESHIP_URL.format(q=domain)
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
                ctx = await browser.new_context(user_agent=APP_UA, viewport={"width": 1280, "height": 900})
                page = await ctx.new_page()
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                    # نحاول ننتظر كارد النتيجة، لو ما ظهر نعتمد على النص الكامل
                    try:
                        await page.wait_for_load_state("networkidle", timeout=20000)
                    except PWError:
                        pass
                    body_text = (await page.inner_text("body")).strip()
                    html = await page.content()
                finally:
                    await browser.close()
        except Exception as e:
            # فشل التحميل—نرجع unknown بدل ما نكسر السيرفر
            data = {"domain": domain, "status": "unknown", "tier": None, "price": None, "source": "spaceship", "error": str(e)}
            cache_set(domain, data)
            return data

    # استخراج السعر والتصنيف
    price_str = extract_price_str(html) or extract_price_str(body_text)
    price_val = extract_price_val(html) or extract_price_val(body_text)
    status, tier = classify(price_val, body_text)

    data = {
        "domain": domain,
        "status": status,           # available / taken / unknown
        "tier": tier,               # registerable / premium / review / None
        "price": price_str,         # مثال: "$9.98/yr"
        "source": "spaceship"
    }
    cache_set(domain, data)
    return data

# ------------------- FastAPI + Telegram -------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # بدون سلاش في النهاية

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is required")

bot = Bot(BOT_TOKEN)
application = Application.builder().bot(bot).build()

app = FastAPI(title="Spaceship Domain Checker (no API)")

# أوامر البوت
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (
        "Hi! Send me a domain like `brand.com` or `name.net`.\n"
        "I support only .com and .net and I’ll reply with:\n"
        "• status (available/taken)\n"
        "• tier (registerable/premium/review)\n"
        "• price if visible\n"
    )
    await update.message.reply_text(txt, parse_mode="Markdown")

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message.text.strip()
    # خُذ أول دومين شكله صحيح من الرسالة
    m = re.search(r"\b([a-z0-9-]+\.(?:com|net))\b", msg, re.I)
    if not m:
        await update.message.reply_text("❌ Please send a .com or .net domain, e.g. `example.com`.", parse_mode="Markdown")
        return
    domain = validate_domain(m.group(1))
    await update.message.reply_text("🔎 Checking…")
    data = await scrape_spaceship(domain)

    status = data["status"]
    tier = data.get("tier")
    price = data.get("price")

    if status == "taken":
        await update.message.reply_text(f"❌ {domain} is already registered.")
        return

    if status == "available":
        pieces = ["✅ Available"]
        if tier == "registerable": pieces.append("registerable")
        elif tier == "premium": pieces.append("premium/aftermarket")
        elif tier == "review": pieces.append("needs review")
        info = ", ".join(pieces)
        if price: info += f" — Price: {price}"
        await update.message.reply_text(f"{domain}: {info}")
        return

    # unknown
    await update.message.reply_text(f"⚪ {domain}: status unknown (try again).")

application.add_handler(CommandHandler("start", cmd_start))
application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), on_text))

# نقاط FastAPI
@app.on_event("startup")
async def on_startup():
    if WEBHOOK_URL:
        # ثبت الـ webhook تلقائياً
        resp = await bot.set_webhook(url=f"{WEBHOOK_URL}/telegram-webhook", allowed_updates=["message"])
        print("setWebhook:", resp.to_dict() if hasattr(resp, "to_dict") else resp)
    else:
        print("WEBHOOK_URL not set — webhook will NOT be configured automatically.")

@app.get("/health")
async def health(): return {"ok": True}

class TGResult(BaseModel):
    ok: bool = True

@app.post("/telegram-webhook", response_model=TGResult)
async def telegram_webhook(req: Request):
    """
    مهم: مهما حصل، رجّع 200 بسرعة عشان تيليجرام ما يكرر الطلبات ويعمل 502.
    """
    try:
        data = await req.json()
    except Exception:
        return TGResult(ok=True)

    try:
        update = Update.de_json(data, bot)
        # تعامل مع الحالات اللي ما فيها message
        if update.message:
            await application.process_update(update)
    except Exception as e:
        # سجّل الخطأ وواصل
        print("Webhook error:", repr(e))
    return TGResult(ok=True)

# للتجربة محلياً: uvicorn main:app --reload            "👋 أهلاً! أرسل لي اسم دومين من نوع .com أو .net مثل:\n"
            "  example.com\n\n"
            "سأرد عليك بحالة الدومين (متاح أو محجوز)."
        )
        await tg_send_message(chat_id, help_text)
        return {"ok": True}

    # قبول الدومين مباشرة من الرسالة
    candidate = text.lower()
    if not any(candidate.endswith(tld) for tld in SUPPORTED_TLDS):
        await tg_send_message(chat_id, "❌ Only .com and .net are supported.")
        return {"ok": True}

    # فاليديشن وفحص RDAP
    try:
        domain = validate_domain(candidate)
    except HTTPException as e:
        await tg_send_message(chat_id, f"❌ {e.detail}")
        return {"ok": True}

    await tg_send_message(chat_id, "🔎 Checking…")
    res = await rdap_check(domain)
    status = res["status"]

    if status == "available":
        msg = f"✅ `{domain}` is AVAILABLE for registration."
    elif status == "taken":
        msg = f"❌ `{domain}` is already registered."
    else:
        msg = f"⚪ Couldn't determine the status of `{domain}`. Try again."

    await tg_send_message(chat_id, msg)
    return {"ok": True}

# ========= ضبط الويبهوك عند الإقلاع =========
@app.on_event("startup")
async def on_startup():
    if not WEBHOOK_URL:
        print("WEBHOOK_URL not set. Skipping webhook setup.")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook"
    payload = {
        "url": f"{WEBHOOK_URL}/{WEBHOOK_SECRET}",
        "drop_pending_updates": True,
        "allowed_updates": ["message"],
    }
    async with httpx.AsyncClient(timeout=10.0) as c:
        r = await c.post(url, json=payload)
        try:
            print("setWebhook:", r.json())
        except Exception:
            print("setWebhook status:", r.status_code)

# ========= تشغيل محلي =========
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, reload=True)
