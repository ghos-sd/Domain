import os
import re
import asyncio
from typing import Optional, Dict
from fastapi import FastAPI, Request, HTTPException
import httpx

# ========= إعدادات البيئة =========
BOT_TOKEN = os.getenv("BOT_TOKEN")  # توكن البوت من BotFather
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # عنوانك العام https://.... (بدون سلاش أخير)
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "telegram-webhook")  # جزء URL سري
PORT = int(os.getenv("PORT", "8000"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is required")

# ========= FastAPI =========
app = FastAPI(title="Domain Checker Bot (.com/.net)")

# ========= إعدادات عامة =========
SUPPORTED_TLDS = {".com", ".net"}
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"

RDAP_BASE = {
    ".com": "https://rdap.verisign.com/com/v1/domain/",
    ".net": "https://rdap.verisign.com/net/v1/domain/",
}

# ========= أدوات مساعدة =========
def normalize_domain(s: str) -> str:
    return s.strip().lower()

def validate_domain(s: str) -> str:
    s = normalize_domain(s)
    # label.tld — أحرف/أرقام/شرطة فقط
    if not re.fullmatch(r"[a-z0-9-]+\.(com|net)", s):
        raise HTTPException(status_code=400, detail="Only .com and .net are supported.")
    label, tld = s.rsplit(".", 1)
    if label.startswith("-") or label.endswith("-"):
        raise HTTPException(status_code=400, detail="Invalid domain label.")
    return s

async def rdap_check(domain: str) -> Dict[str, str]:
    tld = "." + domain.rsplit(".", 1)[1]
    base = RDAP_BASE.get(tld)
    if not base:
        # لن يحدث بسبب الفاليديشن
        return {"domain": domain, "status": "unknown", "source": "rdap"}

    try:
        async with httpx.AsyncClient(timeout=10.0, headers={"User-Agent": UA}) as c:
            r = await c.get(base + domain)
        if r.status_code == 200:
            return {"domain": domain, "status": "taken", "source": "rdap"}
        elif r.status_code == 404:
            return {"domain": domain, "status": "available", "source": "rdap"}
        else:
            return {"domain": domain, "status": "unknown", "source": "rdap"}
    except httpx.HTTPError:
        return {"domain": domain, "status": "unknown", "source": "rdap"}

async def tg_send_message(chat_id: int, text: str) -> None:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    async with httpx.AsyncClient(timeout=10.0) as c:
        await c.post(url, json=payload)

# ========= نقاط API عامة =========
@app.get("/")
async def root():
    return {"ok": True, "service": "Domain Checker Bot", "webhook": f"/{WEBHOOK_SECRET}"}

@app.get("/health")
async def health():
    return {"ok": True}

# ========= Webhook (Telegram) =========
@app.post(f"/{{secret_path}}")
async def telegram_webhook(request: Request, secret_path: str):
    """
    تيليجرام هيرسل الـ Update هنا.
    المسار لازم يطابق WEBHOOK_SECRET.
    """
    if secret_path != WEBHOOK_SECRET:
        # رفض أي مسار غير صحيح
        raise HTTPException(status_code=403, detail="Forbidden")

    update = await request.json()
    message = update.get("message") or update.get("edited_message")
    if not message:
        return {"ok": True}  # تجاهل أنواع تحديثات أخرى

    chat = message.get("chat", {})
    chat_id = chat.get("id")
    text = (message.get("text") or "").strip()

    if not chat_id or not text:
        return {"ok": True}

    # معالجة الأمر /start
    if text.startswith("/start"):
        help_text = (
            "👋 أهلاً! أرسل لي اسم دومين من نوع .com أو .net مثل:\n"
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
