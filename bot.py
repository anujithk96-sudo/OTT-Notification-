import os
import asyncio
import logging
import threading
import hashlib

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from pymongo import MongoClient
from pymongo.errors import PyMongoError

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

# ==================================================
# LOGGING
# ==================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)

# ==================================================
# ENVIRONMENT VARIABLES
# ==================================================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
DATABASE_NAME = os.getenv("DATABASE_NAME", "NIKDB")
CHANNEL_ID = os.getenv("CHANNEL_ID")

ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0"))

PORT = int(os.getenv("PORT", "10000"))

CHECK_INTERVAL = 300  # 5 minutes

if not TELEGRAM_BOT_TOKEN:
    raise ValueError("Missing TELEGRAM_BOT_TOKEN")

if not DATABASE_URL:
    raise ValueError("Missing DATABASE_URL")

if not CHANNEL_ID:
    raise ValueError("Missing CHANNEL_ID")

if ADMIN_USER_ID == 0:
    raise ValueError("Missing ADMIN_USER_ID")

# ==================================================
# HTTP HEALTH CHECK
# ==================================================

class HealthHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        if self.path in ("/", "/health"):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"OTT Notifier Bot is running")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass


def start_health_server():
    server = ThreadingHTTPServer(
        ("0.0.0.0", PORT),
        HealthHandler,
    )

    logger.info("Health server listening on port %s", PORT)

    server.serve_forever()


# ==================================================
# MONGODB
# ==================================================

mongo_client = MongoClient(
    DATABASE_URL,
    serverSelectionTimeoutMS=10000,
)

db = mongo_client[DATABASE_NAME]

subscriptions = db["subscriptions"]

subscriptions.create_index("url", unique=True)

# ==================================================
# HTTP REQUESTS
# ==================================================

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    )
}


def validate_url(url):
    try:
        parsed = urlparse(url)

        if parsed.scheme not in ("http", "https"):
            return False

        if not parsed.hostname:
            return False

        host = parsed.hostname.lower()

        allowed_domains = (
            "zee5.com",
            "hotstar.com",
            "jiohotstar.com",
            "sonyliv.com",
            "sunnxt.com",
        )

        return any(
            host == domain or host.endswith("." + domain)
            for domain in allowed_domains
        )

    except Exception:
        return False


def get_page_info(url):
    """
    Read publicly available page metadata.

    Metadata changes are not proof of a new episode.
    """

    response = requests.get(
        url,
        headers=HEADERS,
        timeout=20,
    )

    response.raise_for_status()

    soup = BeautifulSoup(
        response.text,
        "html.parser",
    )

    title = None

    og_title = soup.find(
        "meta",
        attrs={"property": "og:title"},
    )

    if og_title:
        title = og_title.get("content")

    if not title:
        title_tag = soup.find("title")

        if title_tag:
            title = title_tag.get_text(strip=True)

    if not title:
        title = url

    description = ""

    og_description = soup.find(
        "meta",
        attrs={"property": "og:description"},
    )

    if og_description:
        description = og_description.get("content", "")

    # Detect changes in the available page metadata.
    fingerprint = hashlib.sha256(
        f"{title}|{description}".encode("utf-8")
    ).hexdigest()

    return {
        "title": title.strip(),
        "description": description.strip(),
        "fingerprint": fingerprint,
    }


# ==================================================
# ADMIN CHECK
# ==================================================

def is_admin(update):
    user = update.effective_user

    return (
        user is not None
        and user.id == ADMIN_USER_ID
    )


# ==================================================
# START
# ==================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not is_admin(update):
        await update.effective_message.reply_text(
            "⛔ You are not authorized."
        )
        return

    await update.effective_message.reply_text(
        "🤖 OTT Notifier Bot is running!\n\n"
        "Commands:\n"
        "/subscribe <OTT_LINK>\n"
        "/unsubscribe <OTT_LINK>\n"
        "/list\n"
        "/check\n\n"
        "Monitoring interval: 5 minutes."
    )


# ==================================================
# SUBSCRIBE
# ==================================================

async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not is_admin(update):
        await update.effective_message.reply_text(
            "⛔ You are not authorized."
        )
        return

    if not context.args:
        await update.effective_message.reply_text(
            "Please provide an OTT page link.\n\n"
            "Example:\n"
            "/subscribe https://www.zee5.com/"
        )
        return

    url = context.args[0].strip()

    if not validate_url(url):
        await update.effective_message.reply_text(
            "❌ Invalid or unsupported OTT link."
        )
        return

    try:
        # Save the URL even if the OTT website blocks
        # automated requests. Future checks may still fail.
        page_info = await asyncio.to_thread(
            get_page_info,
            url,
        )

        title = page_info["title"]
        fingerprint = page_info["fingerprint"]

    except requests.RequestException as error:
        logger.warning("Could not fetch %s: %s", url, error)

        # Save the URL, but without claiming that
        # the current episode state was successfully read.
        title = url
        fingerprint = None

    try:
        subscriptions.update_one(
            {"url": url},
            {
                "$setOnInsert": {
                    "url": url,
                    "status": "active",
                    "last_title": title,
                    "last_fingerprint": fingerprint,
                    "baseline_pending": fingerprint is None,
                }
            },
            upsert=True,
        )

        await update.effective_message.reply_text(
            "✅ Subscription saved!\n\n"
            f"Page: {title}\n"
            f"Link: {url}\n\n"
            "The bot will check this link every 5 minutes. "
            "New-episode detection depends on the information "
            "available from the website."
        )

    except PyMongoError:
        logger.exception("MongoDB subscription error")

        await update.effective_message.reply_text(
            "❌ Database error while saving subscription."
        )


# ==================================================
# UNSUBSCRIBE
# ==================================================

async def unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not is_admin(update):
        await update.effective_message.reply_text(
            "⛔ You are not authorized."
        )
        return

    if not context.args:
        await update.effective_message.reply_text(
            "Usage:\n/unsubscribe <OTT_LINK>"
        )
        return

    url = context.args[0].strip()

    try:
        result = subscriptions.delete_one({"url": url})

        if result.deleted_count:
            await update.effective_message.reply_text(
                "✅ Subscription removed."
            )
        else:
            await update.effective_message.reply_text(
                "ℹ️ Subscription not found."
            )

    except PyMongoError:
        logger.exception("MongoDB unsubscribe error")

        await update.effective_message.reply_text(
            "❌ Database error."
        )


# ==================================================
# LIST
# ==================================================

async def list_subscriptions(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not is_admin(update):
        await update.effective_message.reply_text(
            "⛔ You are not authorized."
        )
        return

    try:
        items = list(
            subscriptions.find(
                {"status": "active"},
                {"url": 1, "last_title": 1, "_id": 0},
            ).limit(50)
        )

        if not items:
            await update.effective_message.reply_text(
                "No active subscriptions."
            )
            return

        lines = ["📺 Active subscriptions:\n"]

        for number, item in enumerate(items, 1):
            lines.append(
                f"{number}. {item.get('last_title') or 'OTT page'}\n"
                f"{item['url']}"
            )

        await update.effective_message.reply_text(
            "\n\n".join(lines)
        )

    except PyMongoError:
        logger.exception("MongoDB list error")

        await update.effective_message.reply_text(
            "❌ Could not load subscriptions."
        )


# ==================================================
# CHECK UPDATES
# ==================================================

async def check_updates(context: ContextTypes.DEFAULT_TYPE):

    logger.info("Starting OTT metadata check.")

    try:
        items = list(
            subscriptions.find({"status": "active"})
        )

    except PyMongoError:
        logger.exception("Cannot read subscriptions from MongoDB.")
        return

    for item in items:

        url = item.get("url")

        if not url:
            continue

        try:
            page_info = await asyncio.to_thread(
                get_page_info,
                url,
            )

            current_fingerprint = page_info["fingerprint"]
            previous_fingerprint = item.get("last_fingerprint")

            # If the initial page could not be read, establish
            # a baseline now. Do not send a false alert.
            if item.get("baseline_pending") or not previous_fingerprint:

                subscriptions.update_one(
                    {"_id": item["_id"]},
                    {
                        "$set": {
                            "last_fingerprint": current_fingerprint,
                            "last_title": page_info["title"],
                            "baseline_pending": False,
                        }
                    },
                )

                logger.info("Baseline established for %s", url)
                continue

            # No visible metadata change.
            if current_fingerprint == previous_fingerprint:
                continue

            # Update stored metadata.
            subscriptions.update_one(
                {
                    "_id": item["_id"],
                    "last_fingerprint": previous_fingerprint,
                },
                {
                    "$set": {
                        "last_fingerprint": current_fingerprint,
                        "last_title": page_info["title"],
                    }
                },
            )

            message = (
                "🔔 OTT PAGE CHANGE DETECTED\n\n"
                f"🎬 Page: {page_info['title']}\n"
                f"🔗 Link: {url}\n\n"
                "⚠️ Page information changed. This does not "
                "necessarily mean a new episode was released."
            )

            if page_info["description"]:
                message += (
                    "\n\n📝 "
                    + page_info["description"][:500]
                )

            await context.bot.send_message(
                chat_id=CHANNEL_ID,
                text=message,
            )

            logger.info("Metadata change reported for %s", url)

        except requests.RequestException as error:
            logger.warning("Could not check %s: %s", url, error)

        except PyMongoError:
            logger.exception("MongoDB error checking %s", url)

        except Exception:
            logger.exception("Unexpected error checking %s", url)


# ==================================================
# MANUAL CHECK
# ==================================================

async def manual_check(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not is_admin(update):
        await update.effective_message.reply_text(
            "⛔ You are not authorized."
        )
        return

    await update.effective_message.reply_text(
        "🔍 Checking subscribed OTT pages..."
    )

    await check_updates(context)

    await update.effective_message.reply_text(
        "✅ Check completed. Check Render logs for errors."
    )


# ==================================================
# ERROR HANDLER
# ==================================================

async def error_handler(update, context):

    logger.error(
        "Telegram application error",
        exc_info=context.error,
    )


# ==================================================
# MAIN
# ==================================================

def main():

    # Start HTTP health endpoint for Render Web Service.
    threading.Thread(
        target=start_health_server,
        daemon=True,
    ).start()

    try:
        mongo_client.admin.command("ping")
        logger.info("MongoDB connection successful.")

    except PyMongoError:
        logger.exception("MongoDB connection failed.")
        raise

    application = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("subscribe", subscribe))
    application.add_handler(CommandHandler("unsubscribe", unsubscribe))
    application.add_handler(CommandHandler("list", list_subscriptions))
    application.add_handler(CommandHandler("check", manual_check))

    application.add_error_handler(error_handler)

    application.job_queue.run_repeating(
        check_updates,
        interval=CHECK_INTERVAL,
        first=10,
    )

    logger.info("OTT Notifier Bot started.")

    application.run_polling()


if __name__ == "__main__":
    main()
