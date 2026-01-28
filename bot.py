import os
import sys
import signal
import logging
from dotenv import load_dotenv
from telegram.ext import ApplicationBuilder, CommandHandler

from data_store import store
from handlers import start, add_feed, list_feeds, remove_feed, export_opml, import_opml, nbnhhsh_translate
from rss_checker import check_updates

# Custom filter to exclude api.telegram.org logs
class TelegramAPIFilter(logging.Filter):
    def filter(self, record):
        message = record.getMessage()
        return 'api.telegram.org' not in message

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# Apply filter to httpx logger to exclude api.telegram.org requests
httpx_logger = logging.getLogger('httpx')
httpx_logger.addFilter(TelegramAPIFilter())

load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")


def cleanup_handler(signum, frame):
    """Handle cleanup on shutdown."""
    logger.info("Shutting down bot...")
    try:
        store.close()
    except Exception as e:
        logger.error(f"Error during cleanup: {e}")
    sys.exit(0)


def main():
    if not TOKEN:
        print("Error: TELEGRAM_BOT_TOKEN not found in .env")
        return

    print("Initializing bot...")
    
    # Setup signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, cleanup_handler)
    signal.signal(signal.SIGTERM, cleanup_handler)

    app = ApplicationBuilder().token(TOKEN).build()

    # Command handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("add", add_feed))
    app.add_handler(CommandHandler("list", list_feeds))
    app.add_handler(CommandHandler("remove", remove_feed))
    app.add_handler(CommandHandler("export", export_opml))
    app.add_handler(CommandHandler("import", import_opml))
    app.add_handler(CommandHandler("nbnhhsh", nbnhhsh_translate))

    # Job Queue - Check feeds every 5 minutes
    if app.job_queue:
        app.job_queue.run_repeating(check_updates, interval=300, first=10)
        print("Job queue started - checking feeds every 5 minutes")
    else:
        print("Error: JobQueue not available. Install python-telegram-bot[job-queue]")

    print("Bot started polling...")
    app.run_polling()


if __name__ == "__main__":
    main()
