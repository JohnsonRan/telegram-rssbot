import os
import sys
import signal
import logging
from dotenv import load_dotenv
from telegram.ext import ApplicationBuilder, CommandHandler

from data_store import store
from handlers import start, add_feed, list_feeds, no_as_a_service, remove_feed, export_opml, import_opml
from rss_checker import check_updates
from sticker_converter import sticker
from utils import FEED_REQUEST_TIMEOUT

logger = logging.getLogger(__name__)

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

    # Build application with lifecycle hooks
    async def post_init(application):
        """Initialize resources after application starts."""
        import httpx
        # HTTP client for RSS feed fetching with longer timeout and redirects
        application.bot_data['rss_http_client'] = httpx.AsyncClient(
            timeout=FEED_REQUEST_TIMEOUT,
            follow_redirects=True
        )
        application.bot_data['no_http_client'] = httpx.AsyncClient(
            timeout=5.0,
            follow_redirects=True
        )
        logger.info("RSS HTTP client initialized")
    
    async def post_shutdown(application):
        """Clean up resources before application stops."""
        rss_http_client = application.bot_data.get('rss_http_client')
        if rss_http_client:
            await rss_http_client.aclose()
            logger.info("RSS HTTP client closed")
        no_http_client = application.bot_data.get('no_http_client')
        if no_http_client:
            await no_http_client.aclose()
            logger.info("No-as-a-service HTTP client closed")
    
    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # Command handlers (protected ones are wrapped with @require_admin in handlers.py)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("add", add_feed))
    app.add_handler(CommandHandler("list", list_feeds))
    app.add_handler(CommandHandler("remove", remove_feed))
    app.add_handler(CommandHandler("export", export_opml))
    app.add_handler(CommandHandler("import", import_opml))
    app.add_handler(CommandHandler("no", no_as_a_service))
    app.add_handler(CommandHandler("sticker", sticker))

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

