import os
import logging
import requests
from bs4 import BeautifulSoup
from pymongo import MongoClient
from telegram import Update
from telegram.ext import Updater, CommandHandler, CallbackContext

# ലോഗിംഗ് സെറ്റപ്പ്
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# പ്രധാന കോൺഫിഗറേഷൻ (Render-ലെ Environment Variables-ൽ നൽകേണ്ടവ)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")  # ഡാറ്റാബേസ് യുആർഎൽ
DATABASE_NAME = os.getenv("DATABASE_NAME", "NIKDB")  # ഡാറ്റാബേസ് പേര്
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "123456789"))  # നിങ്ങളുടെ അഡ്മിൻ ഐഡി
CHANNEL_ID = os.getenv("CHANNEL_ID")  # പ്രൈവറ്റ് ചാനൽ ഐഡി അല്ലെങ്കിൽ യൂസർനെയിം

# MongoDB കണക്ഷൻ
client = MongoClient(DATABASE_URL)
db = client[DATABASE_NAME]
subscriptions_collection = db['subscriptions']

# /start കമാൻഡ് - ബോട്ട് വർക്ക് ചെയ്യുന്നുണ്ടോ എന്ന് പരിശോധിക്കാൻ
def start(update: Update, context: CallbackContext) -> None:
    user_id = update.message.from_user.id
    if user_id != ADMIN_USER_ID:
        update.message.reply_text("⛔ You are not authorized.")
        return
    
    update.message.reply_text(
        "🤖 **OTT Notifier Bot is Active & Running!**\n\n"
        "Use `/subscribe <OTT_LINK>` to track serials automatically.",
        parse_mode='Markdown'
    )

# /subscribe കമാൻഡ് വഴി പുതിയ സീരിയൽ ലിങ്ക് ചേർക്കൽ
def subscribe(update: Update, context: CallbackContext) -> None:
    user_id = update.message.from_user.id
    if user_id != ADMIN_USER_ID:
        update.message.reply_text("⛔ You are not authorized.")
        return
        
    if not context.args:
        update.message.reply_text("⚠️ Please provide a link. Example:\n`/subscribe https://www.zee5.com/...`", parse_mode='Markdown')
        return
    
    url = context.args[0]
    
    existing = subscriptions_collection.find_one({"url": url})
    if existing:
        update.message.reply_text("ℹ️ This show is already subscribed.")
        return

    # ഡാറ്റാബേസിൽ സേവ് ചെയ്യുന്നു
    subscriptions_collection.insert_one({
        "url": url,
        "status": "active",
        "last_title": ""
    })
    
    update.message.reply_text("✅ Successfully subscribed! Monitoring started.")

# ഒടിടി വെബ്‌സൈറ്റ് ചെക്ക് ചെയ്ത് യഥാർത്ഥ സീരിയൽ പേരും ലിങ്കും ചാനലിലേക്ക് അയക്കുന്ന ഫങ്ഷൻ
def check_updates(context: CallbackContext) -> None:
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
                
                # ഒടിടി വെബ്‌സൈറ്റിലെ യഥാർത്ഥ പേജ് ടൈറ്റിൽ ഓട്ടോമാറ്റിക്കായി എടുക്കുന്നു
                og_title = soup.find('property', property='og:title')
                page_title = og_title['content'] if og_title and og_title.has_attr('content') else None
                
                if not page_title:
                    title_tag = soup.find('title')
                    page_title = title_tag.text if title_tag else "New Episode"

                # പുതിയ അപ്‌ഡേറ്റ് വന്നിട്ടുണ്ടെങ്കിൽ മാത്രം അയക്കുക
                if sub.get('last_title') != page_title:
                    
                    # സീരിയൽ പേര് ഓട്ടോമാറ്റിക്കായി വരുന്ന മനോഹരമായ ഫോർമാറ്റ്
                    message = f"""
⚡ **#First_On_OTT**

🎬 **Serial Name:** {page_title}
🌐 **From OTT:** Zee5 / Hotstar
🔗 **Link:** {url}

🔥 *Thanks For Using OTT Notifier Bot* 🔥
                    """
                    
                    # ചാനലിലേക്ക് നോട്ടിഫിക്കേഷൻ അയക്കുന്നു
                    context.bot.send_message(chat_id=CHANNEL_ID, text=message, parse_mode='Markdown')
                    
                    # ഡാറ്റാബേസ് അപ്ഡേറ്റ് ചെയ്യുന്നു
                    subscriptions_collection.update_one(
                        {"url": url},
                        {"$set": {"last_title": page_title}}
                    )
                
        except Exception as e:
            logger.error(f"Error checking URL {url}: {e}")

def main() -> None:
    updater = Updater(TELEGRAM_BOT_TOKEN)
    dispatcher = updater.dispatcher

    dispatcher.add_handler(CommandHandler("start", start))
    dispatcher.add_handler(CommandHandler("subscribe", subscribe))

    # Job Queue: first=5 (ബോട്ട് സ്റ്റാർട്ട് ചെയ്ത് 5 സെക്കൻഡിൽ ആദ്യ പരിശോധന)
    # interval=1800 (പിന്നീട് ഓരോ 30 മിനിറ്റ് കൂടുമ്പോൾ ഓട്ടോമാറ്റിക്കായി ചെക്ക് ചെയ്യും)
    job_queue = updater.job_queue
    job_queue.run_repeating(check_updates, interval=1800, first=5)

    updater.start_polling()
    updater.idle()

if __name__ == '__main__':
    main()
