from fastapi import FastAPI, Request
import httpx
import os

app = FastAPI()

BOT_TOKEN = os.getenv("BOT_TOKEN")  # تأكد أنه مضاف في Railway variables
API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
SPACESHIP_URL = "https://api.spaceship.com/check"

ALLOWED_TLDS = [".com", ".net"]  # فقط المجالات المدعومة

# الصفحة الرئيسية (اختياري)
@app.get("/")
def read_root():
    return {"status": "Bot is running."}

# Webhook Endpoint
@app.post("/telegram-webhook")
async def telegram_webhook(req: Request):
    data = await req.json()
    
    # التأكد من وجود رسالة
    if "message" not in data or "text" not in data["message"]:
        return {"ok": True}
    
    message = data["message"]
    chat_id = message["chat"]["id"]
    text = message["text"].strip().lower()
    
    # تخطي أي نص بدون .com أو .net
    if not (text.endswith(".com") or text.endswith(".net")):
        await send_message(chat_id, "رجاءً أرسل دومين ينتهي بـ .com أو .net فقط.")
        return {"ok": True}
    
    # استعلام Spaceship API
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{SPACESHIP_URL}?domain={text}")
            result = response.json()
        
        status = result.get("status", "unknown")
        price = result.get("price", "N/A")
        source = result.get("source", "N/A")

        if status == "available":
            msg = f"✅ الدومين متاح: {text}\n💰 السعر: {price}\n📦 المصدر: {source}"
        else:
            msg = f"❌ غير متاح: {text}\n📦 الحالة: {status}"
        
        await send_message(chat_id, msg)

    except Exception as e:
        await send_message(chat_id, f"🚫 حصل خطأ أثناء التحقق: {e}")
    
    return {"ok": True}


async def send_message(chat_id, text):
    async with httpx.AsyncClient() as client:
        await client.post(API_URL, json={
            "chat_id": chat_id,
            "text": text
        })
