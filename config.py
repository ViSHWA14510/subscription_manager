import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    BOT_TOKEN: str      = os.getenv("BOT_TOKEN", "")
    OWNER_ID: int       = int(os.getenv("OWNER_ID", "0"))
    MONGO_URI: str      = os.getenv("MONGO_URI", "")
    MONGO_DB_NAME: str  = os.getenv("MONGO_DB_NAME", "telegram_sub_bot")

    # ── Payment Config ────────────────────────────────────
    UPI_ID: str         = os.getenv("UPI_ID", "yourname@upi")
    UPI_QR_FILE_ID: str = os.getenv("UPI_QR_FILE_ID", "")   # Telegram file_id of your QR image

    @classmethod
    def validate(cls):
        if not cls.BOT_TOKEN:
            raise ValueError("❌ BOT_TOKEN is not set in .env")
        if not cls.OWNER_ID:
            raise ValueError("❌ OWNER_ID is not set in .env")
        if not cls.MONGO_URI:
            raise ValueError("❌ MONGO_URI is not set in .env")
        if not cls.UPI_ID or cls.UPI_ID == "yourname@upi":
            print("⚠️  WARNING: UPI_ID not set in .env — payments will show placeholder")
        if not cls.UPI_QR_FILE_ID:
            print("⚠️  WARNING: UPI_QR_FILE_ID not set — QR code will not be shown to users")
