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
    if not re.fullmatch(r"[a-z0-9
