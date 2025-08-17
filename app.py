import os, re, time, asyncio, decimal
from typing import Optional, Dict, Any
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel
import httpx
from playwright.async_api import async_playwright

# ===== إعدادات عامة =====
APP_UA = os.getenv(
    "APP_UA",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
)

ALLOWED_TLDS = {".com", ".net"}
SPACESHIP_URL = "https://www.spaceship.com/domain-search/?query={domain}&beast=false&tab=domains"

# حدود السعر (يمكن تعديلها من ENV)
PRICE_LOW_MAX = decimal.Decimal(os.getenv("PRICE_LOW_MAX", "10"))   # ≤10 تسجيل عادي
PRICE_PREMIUM_MIN = decimal.Decimal(os.getenv("PRICE_PREMIUM_MIN", "20"))  # ≥20 بريميوم

# كاش بسيط في الذاكرة + ضبط معدل
CACHE_TTL = int(os.getenv("CACHE_TTL", str(6 * 3600)))  # 6 ساعات
_concurrency = int(os.getenv("MAX_CONCURRENCY", "3"))
sem = asyncio.Semaphore(_concurrency)
_last_call_ts = 0.0
MIN_INTERVAL = float(os.getenv("MIN_INTERVAL", "1.0"))  # ثانية بين الطلبات

# ===== نماذج الرد =====
class CheckResult(BaseModel):
    domain: str
    status: str                 # available / taken / unknown
    tier: Optional[str] = None  # registerable / premium / review
    price: Optional[str] = None # "$8.88/yr" إن وُجد
    source: str                 # "spaceship" / "rdap" / "fallback"

# ===== أدوات =====
def _now() -> float:
    return time.time()

def normalize_domain(domain: str) -> str:
    return domain.strip().lower()

def validate_domain(domain: str) -> str:
    d = normalize_domain(domain)
    # شكل مبسّط: label.tld مع السماح بحروف/أرقام/شرطة فقط في اللابل
    if not re.fullmatch(r"[a-z0-9-]+\.(com|net)", d):
        raise HTTPException(400, "Only .com and .net are supported, invalid domain format.")
    label, tld = d.rsplit(".", 1)
    if label.startswith("-") or label.endswith("-"):
        raise HTTPException(400, "Invalid label: cannot start or end with hyphen.")
    tld = "." + tld
    if tld not in ALLOWED_TLDS:
        raise HTTPException(400, "TLD not allowed. Only .com and .net.")
    return d

def parse_price_value(text: str) -> Optional[decimal.Decimal]:
    m = re.search(r"\$\s*([\d,]+(?:\.\d{1,2})?)", text)
    if not m:
        return None
    num = m.group(1).replace(",", "")
    try:
        return decimal.Decimal(num)
    except decimal.InvalidOperation:
        return None

def extract_price_str(text: str) -> Optional[str]:
    m = re.search(r"\$\s*[\d,]+(?:\.\d{1,2})?\s*/?\s*yr?", text, re.I)
    return m.group(0).replace(" ", "") if m else None

# ===== RDAP (verisign) لـ .com/.net =====
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
            return CheckResult(domain=domain, status="taken", tier=None, price=None, source="rdap")
        if r.status_code == 404:
            return CheckResult(domain=domain, status="available", tier="registerable", price=None, source="rdap")
        return CheckResult(domain=domain, status="unknown", tier=None, price=None, source="rdap")
    except httpx.HTTPError:
        return CheckResult(domain=domain, status="unknown", tier=None, price=None, source="rdap")

# ===== Scraper لصفحة Spaceship =====
async def scrape_spaceship(domain: str) -> CheckResult:
    global _last_call_ts
    async with sem:
        # ريتميت بسيط
        now = _now()
        wait = MIN_INTERVAL - (now - _last_call_ts)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_call_ts = _now()

        url = SPACESHIP_URL.format(domain=domain)
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
            context = await browser.new_context(
                user_agent=APP_UA,
                viewport={"width": 1280, "height": 900}
            )
            page = await context.new_page()

            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                # ننتظر هدوء الشبكة عشان السعر يظهر
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception as e:
                await browser.close()
                raise HTTPException(502, f"Spaceship load error: {e}")

            body_text = await page.inner_text("body")
            await browser.close()

    status = "unknown"
    if re.search(rf"{re.escape(domain)}.*?is available", body_text, re.I | re.S):
        status = "available"
    elif re.search(r"(is already registered|is taken|unavailable|not available)", body_text, re.I):
        status = "taken"

    price_str = extract_price_str(body_text)
    price_val = parse_price_value(body_text)

    # إشارات نصّية على بريميوم
    premium_hint = bool(re.search(r"(Premium|Buy\s*now|Make\s*an\s*offer|Aftermarket)", body_text, re.I))

    tier: Optional[str] = None
    if price_val is not None:
        if price_val <= PRICE_LOW_MAX:
            tier = "registerable"
            status = "available"
        elif price_val >= PRICE_PREMIUM_MIN or premium_hint:
            tier = "premium"
            status = "available"
        else:
            tier = "review"

    return CheckResult(domain=domain, status=status, tier=tier, price=price_str, source="spaceship")

# ===== كاش بسيط في الذاكرة =====
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

# ===== تطبيق FastAPI =====
app = FastAPI(title="Domain Availability Checker (.com/.net)")

@app.get("/health")
async def health():
    return {"ok": True}

@app.get("/check", response_model=CheckResult)
async def check(domain: str = Query(..., description="example.com or example.net")):
    d = validate_domain(domain)
    key = d

    cached = cache_get(key)
    if cached:
        return cached

    # نحاول Spaceship أولاً
    try:
        res = await scrape_spaceship(key)
    except HTTPException:
        # لو فشل السكيرابر، جرّب RDAP
        res = await rdap_check(key) or CheckResult(
            domain=key, status="unknown", tier=None, price=None, source="fallback"
        )

    # لو Unknown جرب RDAP لتحسين الثقة
    if res.status == "unknown":
        rd = await rdap_check(key)
        if rd:
            res = rd

    cache_set(key, res)
    return res
