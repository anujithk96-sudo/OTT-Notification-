import os
import logging
import requests
from bs4 import BeautifulSoup
from pymongo import MongoClient
import telegram
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ലോഗിംഗ് സെറ്റപ്പ്
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# പ്രധാന കോൺഫിഗറേഷൻ (Render-ലെ Environment Variables-ൽ നൽകേണ്ടവ)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
DATABASE_NAME = os.getenv("DATABASE_NAME", "NIKDB")
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "123456789"))
CHANNEL_ID = os.getenv("CHANNEL_ID")

# MongoDB കണക്ഷൻ
client = MongoClient(DATABASE_URL)
db = client[DATABASE_NAME]
subscriptions_collection = db['subscriptions']

# /start കമാൻഡ്
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if user_id != ADMIN_USER_ID:
        await update.message.reply_text("⛔ You are not authorized.")
        return
    
    await update.message.reply_text(
        "🤖 **OTT Notifier Bot is Active & Running!**\n\n"
        "Use `/subscribe <OTT_LINK>` to track serials automatically.",
        parse_mode='Markdown'
    )

# /subscribe കമാൻഡ്
async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if user_id != ADMIN_USER_ID:
        await update.message.reply_text("⛔ You are not authorized.")
        return
        
    if not context.args:
        await update.message.reply_text("⚠️ Please provide a link. Example:\n`/subscribe https://www.zee5.com/...`", parse_mode='Markdown')
        return
    
    url = context.args[0]
    
    existing = subscriptions_collection.find_one({"url": url})
    if existing:
        await update.message.reply_text("ℹ️ This show is already subscribed.")
        return

    subscriptions_collection.insert_one({
        "url": url,
        "status": "active",
        "last_title": ""
    })
    
    await update.message.reply_text("✅ Successfully subscribed! Monitoring started.")

# ഒടിടി വെബ്‌സൈറ്റ് ചെക്ക് ചെയ്ത് ചാനലിലേക്ക് അയക്കുന്ന ഫങ്ഷൻ
async def check_updates(context: ContextTypes.DEFAULT_TYPE) -> None:
    all_subs = subscriptions_collection.find({"status": "active"})
    
    for sub in all_subs:
        url = sub['url']
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            }
            response = requests.get(url, headers=headers, timeout=15)
            
            if response.status_code == 200:
                soup = BeautifulSoup(response.text, 'html.parser')
                
                og_title = soup.find('property', property='og:title')
                page_title = og_title['content'] if og_title and og_title.has_attr('content') else None
                
                if not page_title:
                    title_tag = soup.find('title')
                    page_title = title_tag.text if title_tag else "New Episode"

                if sub.get('last_title') != page_title:
                    message = f"""
⚡ **#First_On_OTT**

🎬 **Serial Name:** {page_title}
🌐 **From OTT:** Zee5 / Hotstar
🔗 **Link:** {url}

🔥 *Thanks For Using OTT Notifier Bot* 🔥
                    """
                    
                    await context.bot.send_message(chat_id=CHANNEL_ID, text=message, parse_mode='Markdown')
                    
                    subscriptions_collection.update_one(
                        {"url": url},
                        {"$set": {"last_title": page_title}}
                    )
                
        except Exception as e:
            logger.error(f"Error checking URL {url}: {e}")

def main() -> None:
    # പുതിയ വേർഷൻ ആപ്ലിക്കേഷൻ ബിൽഡർ സെറ്റപ്പ്
    application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("subscribe", subscribe))

    # Job Queue വഴി പരിശോധന ക്രമീകരിക്കുന്നു (interval: 1800 സെക്കൻഡ് = 30 മിനിറ്റ്)
    job_queue = application.job_queue
    job_queue.run_repeating(check_updates, interval=1800, first=5)

    application.run_polling()

if __name__ == '__main__':
    main()
