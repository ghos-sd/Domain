import os
import re
import asyncio
from fastapi import FastAPI, Request, HTTPException
from telegram import Bot, Update
from telegram.ext import Application, MessageHandler, filters
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup

# Read the bot token from environment variables
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable not set")

# Initialize FastAPI app
app = FastAPI(title="Domain Check Bot")

# Initialize Telegram bot and application
bot = Bot(token=BOT_TOKEN)
application = Application.builder().bot(bot).build()

# The supported TLDs and the base URL for the domain search
SUPPORTED_TLDS = {".com", ".net"}
SEARCH_URL_TEMPLATE = "https://www.spaceship.com/domain-search/?query={}&beast=false&tab=domains"

# --- Helper Functions ---

async def get_domain_status(domain: str) -> dict:
    """
    Scrapes Spaceship.com to determine the availability and price of a domain.

    Args:
        domain: The domain name to check (e.g., 'example.com').

    Returns:
        A dictionary with the domain status and price.
    """
    async with async_playwright() as p:
        # Launch a headless browser
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        try:
            # Navigate to the search URL and wait for it to load
            await page.goto(SEARCH_URL_TEMPLATE.format(domain))
            await page.wait_for_selector(".result-card", timeout=10000)
            
            # Get the page's HTML content
            html_content = await page.content()
            soup = BeautifulSoup(html_content, "html.parser")
            
            status_data = {}

            # Check for "already registered" status first
            taken_element = soup.find(lambda tag: "is already registered" in tag.get_text())
            if taken_element:
                status_data["status"] = "taken"
                status_data["price"] = None
                return status_data

            # Find the element containing the price
            price_element = soup.find(lambda tag: tag.get("class") and "price-string" in " ".join(tag.get("class")))
            
            if price_element:
                price_text = price_element.get_text(strip=True)
                
                # Check for premium keywords
                if any(keyword in html_content for keyword in ["Buy now", "Premium", "Aftermarket"]):
                    status_data["status"] = "premium"
                    # Extract the full price for premium domains
                    price_match = re.search(r'\$(\d+[\.,]?\d*)', price_text)
                    status_data["price"] = price_match.group(0) if price_match else None
                    return status_data
                
                # Extract the numeric price value
                price_match = re.search(r'\$(\d+\.?\d*)', price_text)
                if price_match:
                    price_value = float(price_match.group(1))
                    
                    if price_value <= 10.00:
                        status_data["status"] = "normal"
                    elif price_value >= 20.00:
                        status_data["status"] = "premium"
                    else:
                        status_data["status"] = "uncertain"
                    
                    status_data["price"] = price_text
                    return status_data
            
            # If no specific status is found, it's uncertain
            status_data["status"] = "uncertain"
            status_data["price"] = None
            return status_data

        except Exception as e:
            print(f"Error scraping {domain}: {e}")
            return {"status": "error", "price": None}
        finally:
            await browser.close()

# --- Telegram Message Handler ---

async def handle_message(update: Update, context) -> None:
    """
    Handles incoming messages, determines the domain status, and replies.
    """
    message_text = update.message.text.strip().lower()
    
    # Check if the TLD is supported
    if not any(message_text.endswith(tld) for tld in SUPPORTED_TLDS):
        await update.message.reply_text("❌ Only .com and .net domains are supported.")
        return

    await update.message.reply_text("🔎 Checking availability...")

    # Get the domain status using the async function
    status_result = await get_domain_status(message_text)

    # Prepare the reply text based on the status
    status = status_result.get("status")
    price = status_result.get("price")
    reply_text = "An unexpected error occurred." # Default error message

    if status == "normal":
        reply_text = f"✅ {message_text} is available for registration — Price: {price}"
    elif status == "premium":
        reply_text = f"🟡 {message_text} is available but listed for sale — Price: {price}"
    elif status == "taken":
        reply_text = f"❌ {message_text} is already registered."
    elif status == "uncertain":
        reply_text = f"⚪ {message_text} is available but uncertain (needs review)."
    else: # status == "error"
        reply_text = "❌ Could not check the domain. Please try again later."
    
    await update.message.reply_text(reply_text)

# Set up the message handler in the application
application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))

# --- FastAPI Endpoints ---

@app.on_event("startup")
async def startup_event():
    """
    Set the Telegram bot webhook on startup.
    """
    webhook_url = os.getenv("WEBHOOK_URL")
    if not webhook_url:
        print("WEBHOOK_URL environment variable not set. Webhook will not be set.")
        return
    
    await bot.set_webhook(url=f"{webhook_url}/telegram-webhook")
    print(f"Webhook set to {webhook_url}/telegram-webhook")

@app.post("/telegram-webhook")
async def telegram_webhook(request: Request):
    """
    Endpoint for Telegram to send updates to.
    """
    update_json = await request.json()
    update = Update.de_json(update_json, bot)
    await application.process_update(update)
    return {"ok": True}

@app.get("/health")
async def health_check():
    """
    Health check endpoint for the deployment.
    """
    return {"ok": True}

# This is for local development with Uvicorn
if __name__ == "__main__":
    import uvicorn
    # Make sure to set `WEBHOOK_URL` and `BOT_TOKEN` in your environment
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
