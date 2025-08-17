import os
import httpx
from fastapi import FastAPI, Request
from pydantic import BaseModel
from typing import Optional

app = FastAPI()

BOT_TOKEN = os.getenv("BOT_TOKEN")
BASE_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

# موديل الرد
class DomainCheckResult(BaseModel):
    domain: str
    status: str
    tier: Optional[str]
    price: Optional[str]
    source: str

# دالة التحقق من الدومين عبر spaceship
async def check_domain(domain: str) -> DomainCheckResult:
    try:
        async with httpx.AsyncClient() as client:
            res = await client.get(f"https://api.spaceship.com/check?domain={domain}")
            data = res.json()
            return DomainCheckResult(
                domain=domain,
                status=data.get("status", "unknown"),
                tier=data.get("tier", None),
                price=data.get("price", None),
                source="spaceship"
            )
    except Exception:
        return DomainCheckResult(
            domain=domain,
            status="error",
            tier=None,
            price=None,
            source="spaceship"
        )

# استقبال Webhook من Telegram
@app.post("/telegram-webhook")
async def telegram_webhook(req: Request):
    data = await req.json()
    message = data.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    text = message.get("text", "")

    if not chat_id or not text:
        return {"ok": True}

    if text.startswith("/start"):
        reply = "أرسل لي أي دومين مثل: `test.com` وسأتحقق من توفره ✅"
    elif "." in text:
        result = await check_domain(text)
        reply = f"""🔎 {result.domain}
الحالة: {result.status}
المستوى: {result.tier or '-'}
السعر: {result.price or '-'}
المصدر: {result.source}"""
    else:
        reply = "من فضلك أرسل اسم دومين صحيح مثل `example.com`"

    # الرد
    await send_message(chat_id, reply)
    return {"ok": True}

# دالة إرسال رسالة
async def send_message(chat_id: int, text: str):
    async with httpx.AsyncClient() as client:
        await client.post(
            f"{BASE_URL}/sendMessage",
            json={"chat_id": chat_id, "text": text}
        )

# فحص جاهزية
@app.get("/")
async def root():
    return {"status": "ok"}
