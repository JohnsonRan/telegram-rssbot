import os
import logging
from dotenv import load_dotenv
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters
from database import init_db
from handlers import start, add_feed, list_feeds, remove_feed, export_opml, import_opml
from rss_checker import check_updates

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

def main():
    if not TOKEN:
        print("Error: TELEGRAM_BOT_TOKEN not found in .env")
        return

    # Initialize DB (idempotent)
    init_db()

    app = ApplicationBuilder().token(TOKEN).build()

    # Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("add", add_feed))
    app.add_handler(CommandHandler("list", list_feeds))
    app.add_handler(CommandHandler("remove", remove_feed))
    app.add_handler(CommandHandler("export", export_opml))
    app.add_handler(CommandHandler("import", import_opml))

    # Job Queue
    if app.job_queue:
        # Run every 5 minutes (300 seconds)
        app.job_queue.run_repeating(check_updates, interval=300, first=10)
        print("Job queue started.")
    else:
        print("Error: JobQueue not available. Install python-telegram-bot[job-queue]")

    print("Bot started polling...")
    app.run_polling()

if __name__ == "__main__":
    main()
