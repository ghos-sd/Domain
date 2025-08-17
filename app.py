import os, re, time, asyncio, decimal
from typing import Optional, Dict, Any
from fastapi import FastAPI, HTTPException, Query, Request, Header
from pydantic import BaseModel
import httpx
from httpx import AsyncClient
from playwright.async_api import async_playwright

# ================== الإعدادات ==================
APP_UA = os.getenv(
    "APP_UA",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
)
ALLOWED_TLDS = {".com", ".net"}
SPACESHIP_URL = "https://www.spaceship.com/domain-search/?query={domain}&beast=false&tab=domains"

# حدود السعر (تقدر تغيرها من الـ ENV)
PRICE_LOW_MAX = decimal.Decimal(os.getenv("PRICE_LOW_MAX", "10"))          # ≤10$ تسجيل عادي
PRICE_PREMIUM_MIN = decimal.Decimal(os.getenv("PRICE_PREMIUM_MIN", "20"))  # ≥20$ Premium/معروض للبيع

# كاش + ريت ليمِت
CACHE_TTL = int(os.getenv("CACHE_TTL", str(6 * 3600)))  # 6 ساعات
MAX_CONCURRENCY = int(os.getenv("MAX_CONCURRENCY", "3"))
MIN_INTERVAL = float(os.getenv("MIN_INTERVAL", "1.0"))  # ثانية بين الطلبات
sem = asyncio.Semaphore(MAX_CONCURRENCY)
_last_call_ts = 0.0

# Telegram
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")        # ضيفه في Railway Variables
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")    # اختياري

# ================== نماذج ==================
class CheckResult(BaseModel):
    domain: str
    status: str                 # available / taken / unknown
    tier: Optional[str] = None  # registerable / premium / review
    price: Optional[str] = None # مثال "$8.88/yr"
    source: str                 # spaceship / rdap / fallback

# ================== أدوات ==================
def _now() -> float: return time.time()

def validate_domain(domain: str) -> str:
    d = domain.strip().lower()
    if not re.fullmatch(r"[a-z0-9-]+\.(com|net)", d):
        raise HTTPException(400, "Only .com and .net are supported, invalid domain format.")
    label, tld = d.rsplit(".", 1)
    if label.startswith("-") or label.endswith("-"):
        raise HTTPException(400, "Invalid label: cannot start or end with hyphen.")
    if f".{tld}" not in ALLOWED_TLDS:
        raise HTTPException(400, "TLD not allowed. Only .com and .net.")
    return d

def extract_price_str(text: str) -> Optional[str]:
    m = re.search(r"\$\s*[\d,]+(?:\.\d{1,2})?\s*/?\s*yr?", text, re.I)
    return m.group(0).replace(" ", "") if m else None

def parse_price_value(text: str) -> Optional[decimal.Decimal]:
    m = re.search(r"\$\s*([\d,]+(?:\.\d{1,2})?)", text)
    if not m:
        return None
    try:
        return decimal.Decimal(m.group(1).replace(",", ""))
    except decimal.InvalidOperation:
        return None

# ================== RDAP (Verisign) ==================
RDAP_BASE = {
    ".com": "https://rdap.verisign.com/com/v1/domain/",
    ".net": "https://rdap.verisign.com/net/v1/domain/",
}

async def rdap_check(domain: str) -> Optional[CheckResult]:
    tld = "." + domain.rsplit(".", 1)[1]
    base = RDAP_BASE.get(tld)
    if not base:
        return None
    try:
        async with httpx.AsyncClient(timeout=8.0, headers={"User-Agent": APP_UA}) as c:
            r = await c.get(base + domain)
        if r.status_code == 200:
            return CheckResult(domain=domain, status="taken", source="rdap")
        if r.status_code == 404:
            return CheckResult(domain=domain, status="available", tier="registerable", source="rdap")
        return CheckResult(domain=domain, status="unknown", source="rdap")
    except httpx.HTTPError:
        return CheckResult(domain=domain, status="unknown", source="rdap")

# ================== Scraper: Spaceship ==================
async def scrape_spaceship(domain: str) -> CheckResult:
    global _last_call_ts
    async with sem:
        # تحكم في معدل الطلبات
        wait = MIN_INTERVAL - (_now() - _last_call_ts)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_call_ts = _now()

        url = SPACESHIP_URL.format(domain=domain)
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
            context = await browser.new_context(user_agent=APP_UA, viewport={"width": 1280, "height": 900})
            page = await context.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                await page.wait_for_load_state("networkidle", timeout=15000)
                body_text = await page.inner_text("body")
            except Exception as e:
                await browser.close()
                raise HTTPException(502, f"Spaceship load error: {e}")
            await browser.close()

    status = "unknown"
    if re.search(rf"{re.escape(domain)}.*?is available", body_text, re.I | re.S):
        status = "available"
    elif re.search(r"(is already registered|is taken|unavailable|not available)", body_text, re.I):
        status = "taken"

    price_str = extract_price_str(body_text)
    price_val = parse_price_value(body_text)
    premium_hint = bool(re.search(r"(Premium|Buy\s*now|Make\s*an\s*offer|Aftermarket)", body_text, re.I))

    tier: Optional[str] = None
    if price_val is not None:
        if price_val <= PRICE_LOW_MAX:
            tier, status = "registerable", "available"
        elif price_val >= PRICE_PREMIUM_MIN or premium_hint:
            tier, status = "premium", "available"
        else:
            tier = "review"

    return CheckResult(domain=domain, status=status, tier=tier, price=price_str, source="spaceship")

# ================== كاش بسيط ==================
_CACHE: Dict[str, Dict[str, Any]] = {}

def cache_get(key: str) -> Optional[CheckResult]:
    v = _CACHE.get(key)
    if not v:
        return None
    if _now() - v["t"] > CACHE_TTL:
        _CACHE.pop(key, None)
        return None
    return v["data"]

def cache_set(key: str, data: CheckResult) -> None:
    _CACHE[key] = {"t": _now(), "data": data}

# ================== FastAPI ==================
app = FastAPI(title="Domain Availability Checker (.com/.net)")

@app.get("/")
async def root():
    return {
        "service": "Domain Checker (.com/.net)",
        "endpoints": ["/health", "/check?domain=example.com", "/webhook (POST)"]
    }

@app.get("/health")
async def health():
    return {"ok": True}

@app.get("/check", response_model=CheckResult)
async def check(domain: str = Query(..., description="example.com or example.net")):
    d = validate_domain(domain)
    cached = cache_get(d)
    if cached:
        return cached

    # Spaceship أولاً
    try:
        res = await scrape_spaceship(d)
    except HTTPException:
        res = await rdap_check(d) or CheckResult(domain=d, status="unknown", source="fallback")

    # RDAP لتحسين الثقة عند unknown
    if res.status == "unknown":
        rd = await rdap_check(d)
        if rd:
            res = rd

    cache_set(d, res)
    return res

# ================== Telegram Webhook ==================
def tg_format_reply(d: dict) -> str:
    dom, s, tier, price = d.get("domain"), d.get("status"), d.get("tier"), d.get("price")
    if s == "taken":
        return f"❌ {dom} محجوز."
    if s == "available":
        if tier == "registerable":
            return f"✅ {dom} متاح للتسجيل — السعر: {price or 'N/A'}"
        if tier == "premium":
            return f"🟡 {dom} متاح لكن Premium/معروض للبيع — السعر: {price or 'N/A'}"
        return f"⚪ {dom} متاح غالبًا — السعر: {price or 'N/A'}"
    return f"❓ {dom} الحالة غير معروفة. جرّب لاحقًا."

@app.post("/webhook")
async def telegram_webhook(
    req: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    # تحقق من السر إن كان معيّن
    if WEBHOOK_SECRET and x_telegram_bot_api_secret_token != WEBHOOK_SECRET:
        return {"ok": False, "error": "bad_secret"}

    data = await req.json()
    msg = data.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    text = (msg.get("text") or "").strip()

    if not (TELEGRAM_TOKEN and chat_id):
        return {"ok": True}

    # /start
    if text == "/start":
        help_msg = (
            "أهلاً 👋\n"
            "أرسل دومين .com أو .net مثل:\n"
            "`mybrand.com`\n\n"
            "سأخبرك: ✅ متاح للتسجيل، 🟡 Premium/معروض للبيع، أو ❌ محجوز."
        )
        async with AsyncClient() as c:
            await c.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": help_msg, "parse_mode": "Markdown"},
            )
        return {"ok": True}

    # قبول فقط .com / .net
    if not re.fullmatch(r"[a-z0-9-]+\.(com|net)", text.lower()):
        bad = "أرسل دومين صحيح ينتهي بـ .com أو .net مثل: `example.com`"
        async with AsyncClient() as c:
            await c.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": bad, "parse_mode": "Markdown"},
            )
        return {"ok": True}

    domain = text.lower()

    # استخدم المنطق الداخلي مباشرة
    result = await scrape_spaceship(domain)
    if result.status == "unknown":
        rd = await rdap_check(domain)
        if rd:
            result = rd

    reply = tg_format_reply(result.dict())
    async with AsyncClient() as c:
        await c.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": reply, "parse_mode": "Markdown"},
        )
    return {"ok": True}
