import os
import re
import asyncio
import logging
import hashlib

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

# ==========================================
# LOGGING
# ==========================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)

# ==========================================
# CONFIGURATION
# ==========================================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
DATABASE_NAME = os.getenv("DATABASE_NAME", "NIKDB")
CHANNEL_ID = os.getenv("CHANNEL_ID")

try:
    ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0"))
except ValueError:
    raise ValueError("ADMIN_USER_ID must be a valid Telegram user ID.")

if not TELEGRAM_BOT_TOKEN:
    raise ValueError("Missing TELEGRAM_BOT_TOKEN environment variable.")

if not DATABASE_URL:
    raise ValueError("Missing DATABASE_URL environment variable.")

if not CHANNEL_ID:
    raise ValueError("Missing CHANNEL_ID environment variable.")

if ADMIN_USER_ID == 0:
    raise ValueError("Set ADMIN_USER_ID in Render environment variables.")

# ==========================================
# MONGODB
# ==========================================

mongo_client = MongoClient(
    DATABASE_URL,
    serverSelectionTimeoutMS=10000,
)

db = mongo_client[DATABASE_NAME]

subscriptions_collection = db["subscriptions"]

# ഒരേ URL ഒന്നിലധികം തവണ ചേർക്കുന്നത് തടയുന്നു.
subscriptions_collection.create_index(
    "url",
    unique=True,
)

# ==========================================
# HTTP SESSION
# ==========================================

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    )
}

REQUEST_TIMEOUT = 20

CHECK_INTERVAL = 1800  # 30 minutes


# ==========================================
# ADMIN CHECK
# ==========================================

def is_admin(update: Update) -> bool:
    user = update.effective_user

    return (
        user is not None
        and user.id == ADMIN_USER_ID
    )


# ==========================================
# URL VALIDATION
# ==========================================

def validate_url(url: str) -> bool:
    """
    Accept only HTTP/HTTPS links.
    """

    if not url:
        return False

    if len(url) > 2048:
        return False

    return url.startswith(("https://", "http://"))


# ==========================================
# PAGE INFORMATION
# ==========================================

def get_page_info(url: str) -> dict:
    """
    Fetch public page metadata.

    This reads the publicly available page title and metadata.
    It does not guarantee detection of a new episode.
    """

    response = requests.get(
        url,
        headers=HEADERS,
        timeout=REQUEST_TIMEOUT,
    )

    response.raise_for_status()

    soup = BeautifulSoup(
        response.text,
        "html.parser",
    )

    title = None

    # Open Graph title
    og_title = soup.find(
        "meta",
        attrs={"property": "og:title"},
    )

    if og_title:
        title = og_title.get("content")

    # Twitter title
    if not title:
        twitter_title = soup.find(
            "meta",
            attrs={"name": "twitter:title"},
        )

        if twitter_title:
            title = twitter_title.get("content")

    # Normal HTML title
    if not title:
        title_tag = soup.find("title")

        if title_tag:
            title = title_tag.get_text(strip=True)

    if not title:
        title = url

    # Optional description
    description = ""

    og_description = soup.find(
        "meta",
        attrs={"property": "og:description"},
    )

    if og_description:
        description = og_description.get("content", "")

    # Optional poster image
    poster = ""

    og_image = soup.find(
        "meta",
        attrs={"property": "og:image"},
    )

    if og_image:
        poster = og_image.get("content", "")

    # Hash page metadata to detect changes.
    content = f"{title}|{description}|{poster}"

    page_hash = hashlib.sha256(
        content.encode("utf-8")
    ).hexdigest()

    return {
        "title": title.strip(),
        "description": description.strip(),
        "poster": poster.strip(),
        "hash": page_hash,
    }


# ==========================================
# START COMMAND
# ==========================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    if not is_admin(update):
        await update.effective_message.reply_text(
            "⛔ You are not authorized."
        )
        return

    await update.effective_message.reply_text(
        "🤖 OTT Notifier Bot is running!\n\n"
        "Available commands:\n\n"
        "/subscribe <OTT_LINK>\n"
        "/unsubscribe <OTT_LINK>\n"
        "/list\n"
        "/check\n\n"
        "Monitoring interval: 30 minutes."
    )


# ==========================================
# SUBSCRIBE COMMAND
# ==========================================

async def subscribe(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

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
            "❌ Invalid link. Use an HTTP or HTTPS URL."
        )
        return

    try:
        # Verify the URL before subscribing.
        page_info = await asyncio.to_thread(
            get_page_info,
            url,
        )

        subscriptions_collection.update_one(
            {"url": url},
            {
                "$setOnInsert": {
                    "url": url,
                    "status": "active",
                    "last_hash": page_info["hash"],
                    "last_title": page_info["title"],
                }
            },
            upsert=True,
        )

        await update.effective_message.reply_text(
            "✅ Subscription saved!\n\n"
            f"Page: {page_info['title']}\n"
            f"Link: {url}\n\n"
            "The current page has been saved as the baseline. "
            "Future metadata changes can be reported."
        )

    except requests.RequestException as error:
        logger.warning(
            "Unable to access subscription URL %s: %s",
            url,
            error,
        )

        await update.effective_message.reply_text(
            "❌ Could not access this page.\n"
            "The website may block automated requests or "
            "require JavaScript to display its content."
        )

    except PyMongoError:
        logger.exception("MongoDB subscription error.")

        await update.effective_message.reply_text(
            "❌ Database error. Please try again later."
        )


# ==========================================
# UNSUBSCRIBE COMMAND
# ==========================================

async def unsubscribe(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

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
        result = subscriptions_collection.delete_one(
            {"url": url}
        )

        if result.deleted_count:
            await update.effective_message.reply_text(
                "✅ Subscription removed."
            )
        else:
            await update.effective_message.reply_text(
                "ℹ️ This link was not subscribed."
            )

    except PyMongoError:
        logger.exception("MongoDB unsubscribe error.")

        await update.effective_message.reply_text(
            "❌ Database error."
        )


# ==========================================
# LIST COMMAND
# ==========================================

async def list_subscriptions(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    if not is_admin(update):
        await update.effective_message.reply_text(
            "⛔ You are not authorized."
        )
        return

    try:
        subscriptions = list(
            subscriptions_collection.find(
                {"status": "active"},
                {"url": 1, "last_title": 1, "_id": 0},
            ).limit(50)
        )

        if not subscriptions:
            await update.effective_message.reply_text(
                "No active subscriptions."
            )
            return

        lines = ["📺 Active OTT subscriptions:\n"]

        for number, sub in enumerate(
            subscriptions,
            start=1,
        ):
            lines.append(
                f"{number}. {sub.get('last_title', 'OTT page')}\n"
                f"{sub['url']}"
            )

        await update.effective_message.reply_text(
            "\n\n".join(lines)
        )

    except PyMongoError:
        logger.exception("MongoDB list error.")

        await update.effective_message.reply_text(
            "❌ Could not load subscriptions."
        )


# ==========================================
# CHECK SUBSCRIPTIONS
# ==========================================

async def check_updates(
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    logger.info("Starting OTT metadata check.")

    try:
        subscriptions = list(
            subscriptions_collection.find(
                {"status": "active"}
            )
        )

    except PyMongoError:
        logger.exception("Unable to read MongoDB.")
        return

    for sub in subscriptions:

        url = sub.get("url")

        if not url:
            continue

        try:
            page_info = await asyncio.to_thread(
                get_page_info,
                url,
            )

            previous_hash = sub.get("last_hash")
            current_hash = page_info["hash"]

            # First observation only establishes a baseline.
            if not previous_hash:
                subscriptions_collection.update_one(
                    {"_id": sub["_id"]},
                    {
                        "$set": {
                            "last_hash": current_hash,
                            "last_title": page_info["title"],
                        }
                    },
                )
                continue

            # No metadata change.
            if previous_hash == current_hash:
                continue

            # Save the new metadata before sending the message,
            # preventing repeated alerts for the same observation.
            subscriptions_collection.update_one(
                {
                    "_id": sub["_id"],
                    "last_hash": previous_hash,
                },
                {
                    "$set": {
                        "last_hash": current_hash,
                        "last_title": page_info["title"],
                    }
                },
            )

            message = (
                "🔔 OTT PAGE UPDATE DETECTED\n\n"
                f"🎬 Page: {page_info['title']}\n"
                f"🔗 Link: {url}\n\n"
                "ℹ️ The page metadata changed. "
                "This does not necessarily mean a new episode "
                "was released."
            )

            if page_info["description"]:
                description = page_info["description"][:500]

                message += (
                    f"\n\n📝 {description}"
                )

            await context.bot.send_message(
                chat_id=CHANNEL_ID,
                text=message,
                disable_web_page_preview=False,
            )

            logger.info(
                "Page metadata changed: %s",
                url,
            )

        except requests.RequestException as error:
            logger.warning(
                "HTTP error checking %s: %s",
                url,
                error,
            )

        except PyMongoError:
            logger.exception(
                "Database error checking %s",
                url,
            )

        except Exception:
            logger.exception(
                "Unexpected error checking %s",
                url,
            )


# ==========================================
# MANUAL CHECK COMMAND
# ==========================================

async def manual_check(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

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
        "✅ Check completed. Review the channel and Render logs "
        "for any detected changes or errors."
    )


# ==========================================
# ERROR HANDLER
# ==========================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    logger.error(
        "Telegram bot error: %s",
        context.error,
        exc_info=context.error,
    )


# ==========================================
# MAIN
# ==========================================

def main() -> None:

    # Verify MongoDB connectivity.
    try:
        mongo_client.admin.command("ping")
        logger.info("MongoDB connection successful.")

    except PyMongoError as error:
        raise RuntimeError(
            "Could not connect to MongoDB. "
            "Check DATABASE_URL and network access."
        ) from error

    application = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .build()
    )

    # Commands
    application.add_handler(
        CommandHandler("start", start)
    )

    application.add_handler(
        CommandHandler("subscribe", subscribe)
    )

    application.add_handler(
        CommandHandler("unsubscribe", unsubscribe)
    )

    application.add_handler(
        CommandHandler("list", list_subscriptions)
    )

    application.add_handler(
        CommandHandler("check", manual_check)
    )

    application.add_error_handler(error_handler)

    # Automatic monitoring
    application.job_queue.run_repeating(
        check_updates,
        interval=CHECK_INTERVAL,
        first=10,
    )

    logger.info("OTT Notifier Bot started.")

    application.run_polling()


if __name__ == "__main__":
    main()
